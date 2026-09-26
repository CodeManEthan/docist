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
