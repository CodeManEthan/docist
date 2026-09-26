"""Daily limits: who is counted where, what counts, and the 429 at the wall.

The ``metered`` fixture sets anon 2 / free 3 / paid unlimited.
"""
from datetime import timedelta

import pytest

import app as flask_app_module
from models import Usage, db
from utils import metering

app = flask_app_module.app


@pytest.fixture
def pdf(tmp_path, builders):
    return builders.pdf(tmp_path / "doc.pdf", pages=1)


def merge(client, pdf):
    """One cheap, successful metered operation (a one-file merge)."""
    return client.post(
        "/upload",
        data={"files[]": (open(pdf, "rb"), "doc.pdf")},
        content_type="multipart/form-data",
    )


def usage_rows():
    with app.app_context():
        return {(u.subject[:2], u.count) for u in db.session.execute(db.select(Usage)).scalars()}


class TestAnonymous:
    def test_third_op_is_429_with_nudge(self, client, metered, pdf):
        assert merge(client, pdf).status_code == 200
        assert merge(client, pdf).status_code == 200
        response = merge(client, pdf)
        assert response.status_code == 429
        body = response.get_json()
        assert body["code"] == "daily_limit"
        assert body["limit"] == 2 and body["used"] == 2
        assert body["signup_url"] == "/signup"
        assert "/signup" in body["error"] and "3 a day" in body["error"]
        assert 1 <= int(response.headers["Retry-After"]) <= 24 * 3600

    def test_refused_op_is_not_counted(self, client, metered, pdf):
        merge(client, pdf)
        merge(client, pdf)
        merge(client, pdf)
        assert usage_rows() == {("ip", 2)}

    def test_failed_op_is_not_counted(self, client, metered, pdf):
        assert client.post("/upload").status_code == 400
        assert client.post("/upload").status_code == 400
        assert merge(client, pdf).status_code == 200
        assert usage_rows() == {("ip", 1)}

    def test_usage_headers(self, client, metered, pdf):
        response = merge(client, pdf)
        assert response.headers["X-Docist-Usage-Limit"] == "2"
        assert response.headers["X-Docist-Usage-Remaining"] == "1"
        response = client.post("/upload")  # failed: shows, does not spend
        assert response.headers["X-Docist-Usage-Remaining"] == "1"

    def test_preview_and_gets_are_not_metered(self, client, metered):
        response = client.post("/preview/thumbs")
        assert "X-Docist-Usage-Limit" not in response.headers
        assert "X-Docist-Usage-Limit" not in client.get("/").headers
        assert usage_rows() == set()

    def test_metering_off_counts_nothing(self, client, pdf):
        assert "X-Docist-Usage-Limit" not in merge(client, pdf).headers
        assert usage_rows() == set()

    def test_day_rollover(self, client, metered, pdf, monkeypatch):
        merge(client, pdf)
        merge(client, pdf)
        assert merge(client, pdf).status_code == 429
        tomorrow = metering.today() + timedelta(days=1)
        monkeypatch.setattr(metering, "today", lambda: tomorrow)
        assert merge(client, pdf).status_code == 200


class TestAccounts:
    def test_unverified_shares_the_anonymous_subject(
            self, client, metered, pdf, make_user, login):
        assert merge(client, pdf).status_code == 200  # anonymous
        login(client, make_user(verified=False))
        assert merge(client, pdf).status_code == 200
        response = merge(client, pdf)
        assert response.status_code == 429
        assert "Verify your email" in response.get_json()["error"]
        assert usage_rows() == {("ip", 2)}

    def test_verified_free_has_its_own_subject_and_limit(
            self, client, metered, pdf, make_user, login):
        merge(client, pdf)
        merge(client, pdf)
        assert merge(client, pdf).status_code == 429  # anonymous allowance spent
        login(client, make_user())
        for _ in range(3):
            assert merge(client, pdf).status_code == 200
        response = merge(client, pdf)
        assert response.status_code == 429
        assert response.get_json()["limit"] == 3
        assert "resets at midnight UTC" in response.get_json()["error"]
        assert usage_rows() == {("ip", 2), ("u:", 3)}

    def test_paid_is_unlimited(self, client, metered, pdf, make_user, login):
        login(client, make_user(plan="monthly"))
        for _ in range(5):
            response = merge(client, pdf)
            assert response.status_code == 200
        assert response.headers["X-Docist-Usage-Limit"] == "unlimited"
        assert response.headers["X-Docist-Usage-Remaining"] == "unlimited"

    def test_paid_limit_applies_when_configured(
            self, client, metered, pdf, make_user, login):
        metered(paid=1)
        login(client, make_user(plan="lifetime"))
        assert merge(client, pdf).status_code == 200
        assert merge(client, pdf).status_code == 429


class TestSummary:
    def test_usage_summary_tiers(self, client, metered, make_user, login):
        with app.test_request_context():
            summary = metering.usage_summary(None)
            assert summary == {"used": 0, "limit": 2, "remaining": 2, "tier": "anon"}
            with app.app_context():
                paid = make_user(email="p@x.io", plan="monthly")
                unverified = make_user(email="u@x.io", verified=False)
            assert metering.usage_summary(paid) == {
                "used": 0, "limit": None, "remaining": None, "tier": "paid",
            }
            assert metering.usage_summary(unverified)["tier"] == "unverified"
            assert metering.resolve(unverified)[0].startswith("ip:")
            assert metering.resolve(paid)[0] == f"u:{paid.id}"
