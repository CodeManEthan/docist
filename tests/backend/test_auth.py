"""Sign up, sign in/out, verification, password reset, throttles, rate limit.

Uses the shared ``client`` (in-memory SQLite, CSRF and metering off),
``make_user`` and ``login`` fixtures from ``tests/backend/conftest.py``.
Outgoing mail is captured by patching ``utils.mailer.send``.
"""
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import app as flask_app_module
from models import ApiKey, EmailToken, User, db
from routes import auth as auth_routes
from utils.ratelimit import RateLimiter

PASSWORD = "pw-12345678"
NEW_PASSWORD = "brand-new-pass-99"


@pytest.fixture(autouse=True)
def _console_mail(monkeypatch):
    for name in ("DOCIST_EMAIL_BACKEND", "DOCIST_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def outbox(monkeypatch):
    """Capture every email the routes send as (to, subject, text)."""
    sent = []

    def fake_send(to, subject, text):
        sent.append((to, subject, text))
        return True

    monkeypatch.setattr(auth_routes.mailer, "send", fake_send)
    # /forgot mails from a thread; run it inline so tests can read the outbox
    # right after the request. TestForgotTiming covers the real thread.
    monkeypatch.setattr(auth_routes, "_send_detached", fake_send)
    return sent


@pytest.fixture
def throttled():
    """Force the auth throttles on, starting from empty windows."""
    app = flask_app_module.app
    auth_routes.reset_throttles()
    app.config["AUTH_THROTTLE_FORCE"] = True
    try:
        yield
    finally:
        app.config.pop("AUTH_THROTTLE_FORCE", None)
        auth_routes.reset_throttles()


def _db(fn):
    with flask_app_module.app.app_context():
        return fn()


def _user(email):
    return _db(lambda: db.session.execute(
        db.select(User).where(User.email == email)).scalar_one_or_none())


def _token_from(outbox, kind):
    to, subject, text = outbox[-1]
    match = re.search(rf"/{kind}/([A-Za-z0-9_\-]+)", text)
    assert match, text
    return match.group(1)


def _signed_in_as(client):
    with client.session_transaction() as sess:
        return sess.get("uid")


def _post_login(client, email="a@x.io", password=PASSWORD, next_target="/"):
    return client.post("/login", data={
        "email": email, "password": password, "next": next_target,
    })


def _signup(client, email="new@x.io", password=PASSWORD, confirm=None):
    return client.post("/signup", data={
        "email": email, "password": password,
        "confirm": password if confirm is None else confirm,
    })


# --------------------------------------------------------------------------
# Sign up
# --------------------------------------------------------------------------
class TestSignup:
    def test_page_renders(self, client):
        response = client.get("/signup")
        assert response.status_code == 200
        assert b'name="confirm"' in response.data

    def test_creates_user_logs_in_and_sends_verify(self, client, outbox):
        response = _signup(client, email="  New@X.io ")
        assert response.status_code == 302
        assert response.headers["Location"] == "/account"
        user = _user("new@x.io")
        assert user is not None
        assert user.verified_at is None
        assert user.password_hash != PASSWORD
        assert _signed_in_as(client) == user.id
        assert outbox and outbox[0][0] == "new@x.io"
        assert "/verify/" in outbox[0][2]

    def test_console_backend_flash_says_guest_limits(self, client, outbox):
        _signup(client)
        page = client.get("/account").data
        assert b"Email isn&#39;t set up on this instance yet" in page

    def test_duplicate_email_says_sign_in(self, client, make_user, outbox):
        make_user(email="a@x.io")
        response = _signup(client, email="A@x.io")
        assert response.status_code == 400
        assert b"already exists" in response.data
        assert b"Sign in instead" in response.data
        assert _signed_in_as(client) is None

    def test_short_password_rejected(self, client, outbox):
        response = _signup(client, password="short")
        assert response.status_code == 400
        assert _user("new@x.io") is None

    def test_mismatched_confirm_rejected(self, client, outbox):
        response = _signup(client, confirm="something-else")
        assert response.status_code == 400
        assert _user("new@x.io") is None

    def test_bad_email_rejected(self, client, outbox):
        assert _signup(client, email="not-an-email").status_code == 400

    def test_send_failure_does_not_fail_signup(self, client, monkeypatch):
        monkeypatch.setattr(auth_routes.mailer, "send", lambda *a: False)
        assert _signup(client).status_code == 302
        assert _user("new@x.io") is not None


# --------------------------------------------------------------------------
# Sign in and out
# --------------------------------------------------------------------------
class TestLogin:
    def test_page_renders_email_and_password(self, client):
        response = client.get("/login")
        assert response.status_code == 200
        assert b'type="email"' in response.data
        assert b'href="/signup"' in response.data
        assert b'href="/forgot"' in response.data

    def test_right_password_signs_in(self, client, make_user):
        user = make_user()
        response = _post_login(client, email="A@X.io ", next_target="/pages")
        assert response.status_code == 302
        assert response.headers["Location"] == "/pages"
        assert _signed_in_as(client) == user.id

    def test_wrong_password(self, client, make_user):
        make_user()
        response = _post_login(client, password="nope-nope-nope")
        assert response.status_code == 401
        assert b"Wrong email or password." in response.data
        assert _signed_in_as(client) is None

    def test_unknown_email_same_message(self, client, monkeypatch):
        checked = []
        real = auth_routes.check_password_hash
        monkeypatch.setattr(auth_routes, "check_password_hash",
                            lambda h, p: checked.append(h) or real(h, p))
        response = _post_login(client, email="nobody@x.io")
        assert response.status_code == 401
        assert b"Wrong email or password." in response.data
        assert checked == [auth_routes._DUMMY_HASH]  # timing equalized

    def test_disabled_user_same_message(self, client, make_user):
        user = make_user()

        def disable():
            db.session.get(User, user.id).disabled = True
            db.session.commit()
        _db(disable)
        response = _post_login(client)
        assert response.status_code == 401
        assert b"Wrong email or password." in response.data
        assert _signed_in_as(client) is None

    @pytest.mark.parametrize("target", [
        "//evil.com", "/\\evil.com", "https://evil.com", "\\\\evil.com",
        "/\t/evil.com", "evil.com",
    ])
    def test_next_open_redirect_refused(self, client, make_user, target):
        make_user()
        response = _post_login(client, next_target=target)
        assert response.status_code == 302
        assert response.headers["Location"] == "/"

    def test_safe_next_keeps_local_paths(self):
        assert auth_routes._safe_next("/account?tab=keys") == "/account?tab=keys"
        assert auth_routes._safe_next("/") == "/"
        assert auth_routes._safe_next(None) == "/"

    def test_forms_carry_csrf_token_and_pass_the_check(self, client, make_user, csrf_on):
        make_user()
        page = client.get("/login").data.decode()
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        assert _post_login(client).status_code == 400  # no token
        response = client.post("/login", data={
            "email": "a@x.io", "password": PASSWORD, "csrf_token": token,
        })
        assert response.status_code == 302

    def test_logout_is_post_only(self, client, make_user, login):
        user = make_user()
        login(client, user)
        assert client.get("/logout").status_code == 405
        response = client.post("/logout")
        assert response.status_code == 302
        assert response.headers["Location"] == "/"
        assert _signed_in_as(client) is None

    def test_logout_keeps_csrf_token(self, client, make_user, login):
        user = make_user()
        login(client, user)
        with client.session_transaction() as sess:
            sess["csrf"] = "keep-me"
        client.post("/logout")
        with client.session_transaction() as sess:
            assert sess.get("csrf") == "keep-me"


# --------------------------------------------------------------------------
# Email verification
# --------------------------------------------------------------------------
class TestVerify:
    def test_link_verifies(self, client, outbox):
        _signup(client)
        token = _token_from(outbox, "verify")
        response = client.post(f"/verify/{token}")
        assert response.status_code == 200
        assert b"Email verified" in response.data
        assert _user("new@x.io").verified_at is not None

    def test_reused_link_refused(self, client, outbox):
        _signup(client)
        token = _token_from(outbox, "verify")
        client.post(f"/verify/{token}")
        for method in (client.get, client.post):
            response = method(f"/verify/{token}")
            assert response.status_code == 400
            assert b"expired or has already been used" in response.data

    def test_get_shows_a_confirm_page_and_changes_nothing(self, client, outbox):
        """launch-hardening §12: a scanner that follows the link verifies nothing."""
        _signup(client)
        token = _token_from(outbox, "verify")
        for _ in range(2):
            response = client.get(f"/verify/{token}")
            assert response.status_code == 200
            page = response.data.decode()
            assert "Confirm that you signed up for Docist with new@x.io." in page
            assert "Verify my email" in page
            assert "If you didn't sign up, close this page and nothing happens." in page
            assert f'action="/verify/{token}"' in page
            assert 'name="csrf_token"' in page
        assert _user("new@x.io").verified_at is None
        tokens = _db(lambda: [t.used_at for t in
                              db.session.execute(db.select(EmailToken)).scalars()])
        assert tokens and all(used is None for used in tokens)

    def test_post_needs_the_csrf_token(self, client, outbox, csrf_on):
        client.get("/signup")
        page = client.get("/signup").data.decode()
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        client.post("/signup", data={"email": "new@x.io", "password": PASSWORD,
                                     "confirm": PASSWORD, "csrf_token": csrf})
        token = _token_from(outbox, "verify")
        assert client.post(f"/verify/{token}").status_code == 400
        assert _user("new@x.io").verified_at is None
        # A fresh browser gets its CSRF token from the GET, as on reset.
        fresh = flask_app_module.app.test_client()
        page = fresh.get(f"/verify/{token}").data.decode()
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        response = fresh.post(f"/verify/{token}", data={"csrf_token": csrf})
        assert response.status_code == 200
        assert _user("new@x.io").verified_at is not None

    def test_the_critics_sequence_now_fails_at_the_api_key(self, client, outbox):
        """The registrant keeps their session; a scanner GETs the link; the
        registrant asks for an API key and is refused (critic, major 1)."""
        _signup(client, email="victim@example.com")
        token = _token_from(outbox, "verify")
        scanner = flask_app_module.app.test_client()
        assert scanner.get(f"/verify/{token}").status_code == 200
        response = client.post("/account/keys", data={"label": "k"})
        assert response.status_code == 302
        assert _user("victim@example.com").verified_at is None
        keys = _db(lambda: db.session.execute(db.select(ApiKey)).scalars().all())
        assert keys == []

    def test_expired_link_refused(self, client, outbox):
        _signup(client)
        token = _token_from(outbox, "verify")

        def expire():
            for tok in db.session.execute(db.select(EmailToken)).scalars():
                tok.expires_at = datetime(2000, 1, 1)
            db.session.commit()
        _db(expire)
        for method in (client.get, client.post):
            response = method(f"/verify/{token}")
            assert response.status_code == 400
        assert _user("new@x.io").verified_at is None

    def test_garbage_token_refused(self, client):
        assert client.get("/verify/not-a-real-token").status_code == 400

    def test_resend_needs_login(self, client):
        response = client.post("/verify/resend")
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login")

    def test_resend_sends_a_new_link(self, client, make_user, login, outbox):
        user = make_user(verified=False)
        login(client, user)
        response = client.post("/verify/resend")
        assert response.status_code == 302
        assert len(outbox) == 1
        assert client.get(f"/verify/{_token_from(outbox, 'verify')}").status_code == 200

    def test_resend_throttled(self, client, make_user, login, outbox, throttled):
        user = make_user(verified=False)
        login(client, user)
        for _ in range(5):
            assert client.post("/verify/resend").status_code == 302
        response = client.post("/verify/resend")
        assert response.status_code == 429
        assert b"Too many attempts" in response.data


# --------------------------------------------------------------------------
# Forgotten password and reset
# --------------------------------------------------------------------------
class TestForgotAndReset:
    def test_forgot_same_answer_for_known_and_unknown(self, client, make_user, outbox):
        make_user()
        known = client.post("/forgot", data={"email": "a@x.io"})
        unknown = client.post("/forgot", data={"email": "ghost@x.io"})
        assert known.status_code == unknown.status_code == 200
        assert known.data == unknown.data
        assert b"If an account exists for that address" in known.data
        assert [m[0] for m in outbox] == ["a@x.io"]

    def test_reset_flow(self, client, make_user, outbox):
        user = make_user(verified=False)
        epoch_before = user.session_epoch

        def add_key():
            raw, _ = ApiKey.issue(db.session.get(User, user.id), "old")
            db.session.commit()
        _db(add_key)

        client.post("/forgot", data={"email": "a@x.io"})
        token = _token_from(outbox, "reset")
        page = client.get(f"/reset/{token}")
        assert page.status_code == 200
        assert b'name="confirm"' in page.data

        response = client.post(f"/reset/{token}", data={
            "password": NEW_PASSWORD, "confirm": NEW_PASSWORD,
        })
        assert response.status_code == 302
        assert response.headers["Location"] == "/account"

        after = _user("a@x.io")
        assert after.check_password(NEW_PASSWORD)
        assert after.verified_at is not None
        assert after.session_epoch == epoch_before + 1
        assert _signed_in_as(client) == user.id
        live = _db(lambda: db.session.execute(
            db.select(ApiKey).where(ApiKey.revoked_at.is_(None))).scalars().all())
        assert live == []
        assert b"revoked" in client.get("/account").data

        # One use only.
        assert client.get(f"/reset/{token}").status_code == 400

    def test_reset_signs_out_other_sessions(self, client, make_user, login, outbox):
        user = make_user()
        other = flask_app_module.app.test_client()
        login(other, user)
        assert other.get("/account").status_code == 200
        client.post("/forgot", data={"email": "a@x.io"})
        token = _token_from(outbox, "reset")
        client.post(f"/reset/{token}", data={
            "password": NEW_PASSWORD, "confirm": NEW_PASSWORD,
        })
        assert other.get("/account").status_code == 302

    def test_reset_typo_keeps_the_link(self, client, make_user, outbox):
        make_user()
        client.post("/forgot", data={"email": "a@x.io"})
        token = _token_from(outbox, "reset")
        bad = client.post(f"/reset/{token}", data={
            "password": NEW_PASSWORD, "confirm": "different-one",
        })
        assert bad.status_code == 400
        assert client.get(f"/reset/{token}").status_code == 200

    def test_expired_reset_refused(self, client, make_user, outbox):
        make_user()
        client.post("/forgot", data={"email": "a@x.io"})
        token = _token_from(outbox, "reset")

        def expire():
            for tok in db.session.execute(db.select(EmailToken)).scalars():
                tok.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
            db.session.commit()
        _db(expire)
        assert client.get(f"/reset/{token}").status_code == 400
        response = client.post(f"/reset/{token}", data={
            "password": NEW_PASSWORD, "confirm": NEW_PASSWORD,
        })
        assert response.status_code == 400
        assert _user("a@x.io").check_password(PASSWORD)

    def test_forgot_per_email_throttle(self, client, make_user, outbox, throttled):
        make_user()
        for _ in range(3):
            assert client.post("/forgot", data={"email": "a@x.io"}).status_code == 200
        response = client.post("/forgot", data={"email": "A@x.io"})
        assert response.status_code == 429
        assert b"Too many attempts" in response.data
        assert len(outbox) == 3


    def test_reset_page_refuses_a_verify_token(self, client, make_user, outbox):
        """The reset GET peeks with purpose 'reset', so a verify link is dead there."""
        user = make_user(verified=False)
        raw = _db(lambda: _issue(user.id, "verify"))
        assert client.get(f"/reset/{raw}").status_code == 400
        assert client.post(f"/reset/{raw}", data={
            "password": NEW_PASSWORD, "confirm": "typo-typo-typo",
        }).status_code == 400


def _issue(user_id, purpose):
    raw = EmailToken.issue(db.session.get(User, user_id), purpose)
    db.session.commit()
    return raw


class TestForgotTiming:
    """/forgot must not wait on the mail server only when the account exists."""

    def test_forgot_does_not_block_on_a_slow_sender(self, client, make_user, monkeypatch):
        make_user()
        sent = []
        release = threading.Event()

        def slow_send(to, subject, text):
            release.wait(5)  # a mail server that hangs until the test lets go
            sent.append(to)
            return True

        monkeypatch.setattr(auth_routes.mailer, "send", slow_send)
        timings = {}
        for email in ("a@x.io", "ghost@x.io"):
            start = time.monotonic()
            response = client.post("/forgot", data={"email": email})
            timings[email] = time.monotonic() - start
            assert response.status_code == 200
        # Both answered while the known account's mail was still stuck.
        assert sent == []
        assert timings["a@x.io"] < 1.0
        release.set()
        for thread in threading.enumerate():
            if thread.name == "docist-mail":
                thread.join(5)
        assert sent == ["a@x.io"]


# --------------------------------------------------------------------------
# Login and signup throttles
# --------------------------------------------------------------------------
class TestAuthThrottle:
    def test_login_per_email_throttle(self, client, make_user, throttled):
        make_user()
        for _ in range(5):
            assert _post_login(client, password="wrong-wrong").status_code == 401
        response = _post_login(client)  # even the right password
        assert response.status_code == 429
        assert b"Too many attempts. Try again in a few minutes." in response.data

    def test_login_per_ip_throttle(self, client, throttled):
        for i in range(10):
            assert _post_login(client, email=f"u{i}@x.io").status_code == 401
        assert _post_login(client, email="fresh@x.io").status_code == 429

    def test_signup_per_ip_throttle(self, client, outbox, throttled):
        for i in range(5):
            _signup(client, email=f"s{i}@x.io")
            client.post("/logout")
        assert _signup(client, email="s9@x.io").status_code == 429

    def test_throttles_off_under_test_by_default(self, client, make_user):
        make_user()
        for _ in range(8):
            assert _post_login(client, password="wrong-wrong").status_code == 401


# --------------------------------------------------------------------------
# Rate limiting (forced on under test with a tiny window)
# --------------------------------------------------------------------------
class TestRateLimit:
    @pytest.fixture
    def limited(self, client):
        app = flask_app_module.app
        prev_limiter = app.limiter
        app.limiter = RateLimiter(max_requests=2, window=3600)
        app.config["RATE_LIMIT_FORCE"] = True
        try:
            yield client
        finally:
            app.limiter = prev_limiter
            app.config.pop("RATE_LIMIT_FORCE", None)

    def test_third_post_in_window_is_429(self, limited):
        assert limited.post("/upload").status_code == 400  # counted, not blocked
        assert limited.post("/upload").status_code == 400
        response = limited.post("/upload")
        assert response.status_code == 429
        assert "error" in response.get_json()
        assert int(response.headers["Retry-After"]) >= 1

    def test_gets_are_never_limited(self, limited):
        for _ in range(5):
            assert limited.get("/").status_code == 200
