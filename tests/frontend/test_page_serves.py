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


def test_packet_page_serves_with_its_assets(client):
    resp = client.get("/packet")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    for asset in ("/static/packet-model.js", "/static/packet.js", "/static/packet.css"):
        assert asset in body
    assert '<meta name="csrf-token"' in body
    assert "/static/csrf.js" in body
    assert 'href="/login"' in body          # the account nav
    # SortableJS is pinned with SRI (design §3.4).
    assert "sortablejs@1.15.7/Sortable.min.js" in body
    assert 'integrity="sha384-' in body
    # It loads async, so a hung CDN never holds the deferred page scripts.
    sortable_tag = body[body.index("<script", body.index("sortablejs") - 200):body.index("</script>", body.index("sortablejs"))]
    assert " async" in sortable_tag and " defer" not in sortable_tag
    # Anonymous visitors have a daily limit, so the page knows what is left.
    assert 'data-ops-left="' in body
    assert 'data-ttl-minutes="' in body


def test_packet_static_assets_served(client):
    for path, needle in (("/static/packet-model.js", "pagePlan"),
                         ("/static/packet.js", "stageWithRelief"),
                         ("/static/packet.css", ".pk-card")):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert needle in resp.get_data(as_text=True), path


def test_packet_page_is_not_linked_yet(client):
    # [B4] adds the nav link and the merge-page link; until then /packet is
    # reachable by URL only.
    for path in ("/", "/convert", "/pages", "/print", "/export", "/security", "/api"):
        assert 'href="/packet"' not in client.get(path).get_data(as_text=True), path
