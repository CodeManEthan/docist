"""The /account page: login wall, password, sessions, API keys, usage.

Uses the shared ``client`` (in-memory SQLite, CSRF and metering off),
``make_user`` and ``login`` fixtures from ``tests/backend/conftest.py``.
"""
import hashlib
import re

import pytest

import app as flask_app_module
from models import ApiKey, User, db
from routes.account import MAX_KEYS

PASSWORD = "pw-12345678"
NEW_PASSWORD = "brand-new-pass-99"


def _db(fn):
    with flask_app_module.app.app_context():
        return fn()


def _keys(user_id):
    return _db(lambda: db.session.execute(
        db.select(ApiKey).where(ApiKey.user_id == user_id)
        .order_by(ApiKey.id)).scalars().all())


def _raw_key(page):
    match = re.search(rb"dk_[A-Za-z0-9_\-]{20,}", page)
    return match.group(0).decode() if match else None


@pytest.fixture
def signed_in(client, make_user, login):
    user = make_user()
    login(client, user)
    return user


class TestLoginWall:
    def test_anonymous_redirected_to_login(self, client):
        response = client.get("/account")
        assert response.status_code == 302
        assert response.headers["Location"] == "/login?next=/account"

    @pytest.mark.parametrize("path", [
        "/account/password", "/account/keys", "/account/signout-all",
        "/account/keys/1/revoke",
    ])
    def test_anonymous_posts_redirected(self, client, path):
        response = client.post(path)
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login")

    def test_page_shows_email_plan_and_usage(self, client, signed_in):
        page = client.get("/account").data
        assert b"a@x.io" in page
        assert b"free" in page
        assert re.search(rb"0 / \d+", page)

    def test_paid_plan_shows_no_limit(self, client, make_user, login):
        user = make_user(email="p@x.io", plan="lifetime")
        login(client, user)
        page = client.get("/account").data
        assert b"lifetime" in page
        assert b"no daily limit" in page

    def test_unverified_sees_resend_state(self, client, make_user, login):
        user = make_user(verified=False)
        login(client, user)
        page = client.get("/account").data
        assert b"not verified" in page


class TestPassword:
    def _change(self, client, current=PASSWORD, new=NEW_PASSWORD, confirm=None):
        return client.post("/account/password", data={
            "current": current, "password": new,
            "confirm": new if confirm is None else confirm,
        })

    def test_wrong_current_rejected(self, client, signed_in):
        response = self._change(client, current="not-my-password")
        assert response.status_code == 302
        user = _db(lambda: db.session.get(User, signed_in.id))
        assert user.check_password(PASSWORD)
        assert b"current password is wrong" in client.get("/account").data

    def test_mismatch_rejected(self, client, signed_in):
        self._change(client, confirm="other-other-other")
        assert _db(lambda: db.session.get(User, signed_in.id)).check_password(PASSWORD)

    def test_change_signs_out_other_sessions_only(self, client, signed_in, login):
        other = flask_app_module.app.test_client()
        login(other, signed_in)
        assert other.get("/account").status_code == 200

        response = self._change(client)
        assert response.status_code == 302
        user = _db(lambda: db.session.get(User, signed_in.id))
        assert user.check_password(NEW_PASSWORD)
        assert client.get("/account").status_code == 200
        assert other.get("/account").status_code == 302

    def test_change_keeps_api_keys(self, client, signed_in):
        client.post("/account/keys", data={"label": "k"})
        self._change(client)
        assert all(k.revoked_at is None for k in _keys(signed_in.id))


class TestSignoutEverywhere:
    def test_other_sessions_dropped_this_one_kept(self, client, signed_in, login):
        other = flask_app_module.app.test_client()
        login(other, signed_in)
        response = client.post("/account/signout-all")
        assert response.status_code == 302
        assert client.get("/account").status_code == 200
        assert other.get("/account").status_code == 302


class TestApiKeys:
    def test_create_shows_raw_once_and_stores_hash(self, client, signed_in):
        response = client.post("/account/keys", data={"label": "laptop"})
        assert response.status_code == 200
        raw = _raw_key(response.data)
        assert raw and raw.startswith("dk_")
        assert b"won't be shown again" in response.data

        (key,) = _keys(signed_in.id)
        assert key.key_hash == hashlib.sha256(raw.encode()).hexdigest()
        assert key.prefix == raw[:12]
        assert key.label == "laptop"
        assert raw not in {key.key_hash, key.prefix}

        again = client.get("/account").data
        assert raw.encode() not in again
        assert key.prefix.encode() in again

    def test_created_key_authenticates(self, client, signed_in):
        raw = _raw_key(client.post("/account/keys", data={"label": "x"}).data)
        found = _db(lambda: ApiKey.lookup(raw))
        assert found is not None

    def test_unverified_cannot_create(self, client, make_user, login):
        user = make_user(verified=False)
        login(client, user)
        response = client.post("/account/keys", data={"label": "x"})
        assert response.status_code == 302
        assert _keys(user.id) == []
        assert b"Verify your email before creating API keys" in client.get("/account").data

    def test_five_key_cap(self, client, signed_in):
        for i in range(MAX_KEYS):
            assert client.post("/account/keys", data={"label": f"k{i}"}).status_code == 200
        response = client.post("/account/keys", data={"label": "one-too-many"})
        assert response.status_code == 302
        assert len(_keys(signed_in.id)) == MAX_KEYS

    def test_revoked_keys_free_a_slot(self, client, signed_in):
        for i in range(MAX_KEYS):
            client.post("/account/keys", data={"label": f"k{i}"})
        first = _keys(signed_in.id)[0]
        client.post(f"/account/keys/{first.id}/revoke")
        assert client.post("/account/keys", data={"label": "new"}).status_code == 200

    def test_revoke_own(self, client, signed_in):
        raw = _raw_key(client.post("/account/keys", data={"label": "x"}).data)
        (key,) = _keys(signed_in.id)
        response = client.post(f"/account/keys/{key.id}/revoke")
        assert response.status_code == 302
        assert _keys(signed_in.id)[0].revoked_at is not None
        assert _db(lambda: ApiKey.lookup(raw)) is None

    def test_revoke_someone_elses_is_404(self, client, make_user, login):
        owner = make_user(email="owner@x.io")
        login(client, owner)
        client.post("/account/keys", data={"label": "theirs"})
        (key,) = _keys(owner.id)

        intruder = make_user(email="intruder@x.io")
        login(client, intruder)
        assert client.post(f"/account/keys/{key.id}/revoke").status_code == 404
        assert _keys(owner.id)[0].revoked_at is None

    def test_revoke_missing_is_404(self, client, signed_in):
        assert client.post("/account/keys/9999/revoke").status_code == 404
