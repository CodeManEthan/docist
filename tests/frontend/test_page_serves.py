"""Integration tests: the refactored page and its static asset are served.

These exercise the real Flask app via its test_client, confirming the page
references the extracted /static/app.js, that the asset is served non-empty,
and that /formats still advertises PDF support.
"""
import os
import sys

import pytest

# Make the project root importable so `import app` works regardless of cwd.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import app as app_module  # noqa: E402


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def test_index_serves_and_references_app_js(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "/static/app.js" in body
    # The big inline script should be gone after extraction.
    assert "function displayFiles" not in body


def test_static_app_js_served_non_empty(client):
    resp = client.get("/static/app.js")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert body.strip() != ""
    # A pure function we extracted should be present in the served asset.
    assert "formatFileSize" in body


def test_formats_returns_json_with_pdf(client):
    resp = client.get("/formats")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, dict)
    assert "extensions" in data
    assert ".pdf" in data["extensions"]


def test_index_anonymous_shows_sign_in_and_csrf_meta(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Sign in" in body
    assert 'href="/login"' in body
    assert '<meta name="csrf-token" content="' in body
    assert "/static/csrf.js" in body
    # The old shared-password sign-out link is gone.
    assert 'href="/logout"' not in body


def test_every_tool_page_includes_csrf_head(client):
    for path in ("/", "/convert", "/pages", "/print", "/export", "/security", "/api"):
        body = client.get(path).get_data(as_text=True)
        assert '<meta name="csrf-token"' in body, path
        assert "/static/csrf.js" in body, path


def test_static_csrf_js_served(client):
    resp = client.get("/static/csrf.js")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "withCsrf" in body
    assert "X-CSRF-Token" in body
