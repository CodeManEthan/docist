"""The session CSRF check on state-changing browser requests."""
import pytest

import app as flask_app_module

TOKEN = "test-csrf-token"
SESSION_EXPIRED = "Session expired — reload the page and try again."


@pytest.fixture
def armed(csrf_on):
    """CSRF on and a known token in the session."""
    with csrf_on.session_transaction() as sess:
        sess["csrf"] = TOKEN
    return csrf_on


class TestRefused:
    def test_post_without_token_is_json_400(self, armed):
        response = armed.post("/upload")
        assert response.status_code == 400
        assert response.get_json() == {"error": SESSION_EXPIRED}

    def test_fetch_accept_any_gets_json(self, armed):
        response = armed.post("/upload", headers={"Accept": "*/*"})
        assert response.status_code == 400
        assert response.get_json()["error"] == SESSION_EXPIRED

    def test_form_navigation_gets_the_message_page(self, armed):
        response = armed.post("/logout", headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        assert response.status_code == 400
        assert response.mimetype == "text/html"
        body = response.get_data(as_text=True)
        assert "Session expired" in body and "<html" in body

    def test_wrong_token_is_refused(self, armed):
        response = armed.post("/logout", headers={"X-CSRF-Token": "nope"})
        assert response.status_code == 400

    def test_no_session_token_is_refused(self, csrf_on):
        response = csrf_on.post("/logout", data={"csrf_token": ""})
        assert response.status_code == 400

    def test_header_wins_over_form_field(self, armed):
        response = armed.post(
            "/logout", data={"csrf_token": TOKEN}, headers={"X-CSRF-Token": "nope"},
        )
        assert response.status_code == 400


class TestAccepted:
    def test_form_field(self, armed):
        assert armed.post("/logout", data={"csrf_token": TOKEN}).status_code == 302

    def test_header(self, armed):
        assert armed.post("/logout", headers={"X-CSRF-Token": TOKEN}).status_code == 302

    def test_tool_post_with_header_reaches_the_route(self, armed):
        response = armed.post("/upload", headers={"X-CSRF-Token": TOKEN})
        assert response.status_code == 400
        assert response.get_json()["error"] == "No files provided"

    def test_api_blueprint_is_exempt(self, armed):
        app = flask_app_module.app
        prev = app.config["API_ANONYMOUS"]
        app.config["API_ANONYMOUS"] = True
        try:
            response = armed.post("/api/v1/pages/extract")
        finally:
            app.config["API_ANONYMOUS"] = prev
        assert response.status_code == 400
        assert response.get_json()["error"] != SESSION_EXPIRED

    def test_get_is_untouched(self, csrf_on):
        assert csrf_on.get("/").status_code == 200
        assert csrf_on.get("/healthz").status_code == 200

    def test_disabled_check_lets_everything_through(self, client):
        assert client.post("/logout").status_code == 302


def test_csrf_token_is_a_template_global_and_stable(client):
    app = flask_app_module.app
    with app.test_request_context():
        first = app.jinja_env.globals["csrf_token"]()
        assert first and app.jinja_env.globals["csrf_token"]() == first
