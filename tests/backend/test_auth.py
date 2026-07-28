"""Auth gate, rate limiting, and health endpoint integration tests."""
import pytest

import app as flask_app_module
from utils.ratelimit import RateLimiter

PASSWORD = "hunter2-test-password"


@pytest.fixture
def gated(client):
    """The standard test client with the login gate switched on."""
    app = flask_app_module.app
    prev = app.config["ACCESS_PASSWORD"]
    app.config["ACCESS_PASSWORD"] = PASSWORD
    try:
        yield client
    finally:
        app.config["ACCESS_PASSWORD"] = prev


def login(client, password=PASSWORD, next_target="/"):
    return client.post(
        "/login", data={"password": password, "next": next_target}
    )


# --------------------------------------------------------------------------
# Gate off (default local setup)
# --------------------------------------------------------------------------
class TestGateOff:
    def test_pages_open_without_login(self, client):
        assert client.get("/").status_code == 200

    def test_login_page_redirects_home(self, client):
        response = client.get("/login")
        assert response.status_code == 302
        assert response.headers["Location"] == "/"

    def test_no_signout_link_when_gate_off(self, client):
        assert b"Sign out" not in client.get("/").data


# --------------------------------------------------------------------------
# Gate on
# --------------------------------------------------------------------------
class TestGateOn:
    def test_page_get_redirects_to_login(self, gated):
        response = gated.get("/")
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login")

    def test_api_post_gets_json_401(self, gated):
        response = gated.post("/upload")
        assert response.status_code == 401
        assert "error" in response.get_json()

    def test_login_page_is_reachable(self, gated):
        response = gated.get("/login")
        assert response.status_code == 200
        assert b"password" in response.data.lower()

    def test_wrong_password_rejected(self, gated):
        response = login(gated, password="nope")
        assert response.status_code == 401
        assert b"Wrong password" in response.data

    def test_right_password_opens_the_app(self, gated):
        response = login(gated, next_target="/pages")
        assert response.status_code == 302
        assert response.headers["Location"] == "/pages"
        assert gated.get("/").status_code == 200

    def test_signout_link_shown_when_authed(self, gated):
        login(gated)
        assert b"Sign out" in gated.get("/").data

    def test_logout_closes_the_session(self, gated):
        login(gated)
        assert gated.get("/").status_code == 200
        gated.get("/logout")
        assert gated.get("/").status_code == 302

    def test_next_open_redirect_is_neutralised(self, gated):
        response = login(gated, next_target="//evil.example.com/phish")
        assert response.status_code == 302
        assert response.headers["Location"] == "/"

    def test_healthz_is_exempt(self, gated):
        response = gated.get("/healthz")
        assert response.status_code == 200
        assert response.get_json() == {"status": "ok"}

    def test_static_assets_are_exempt(self, gated):
        assert gated.get("/static/style.css").status_code == 200

    def test_every_tool_page_is_gated(self, gated):
        for path in ("/", "/convert", "/pages", "/print", "/export", "/security"):
            response = gated.get(path)
            assert response.status_code == 302, path
            assert response.headers["Location"].startswith("/login"), path


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
