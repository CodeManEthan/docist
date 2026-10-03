"""Integration tests: the refactored page and its static asset are served.

These exercise the real Flask app via its test_client, confirming the page
references the extracted /static/app.js, that the asset is served non-empty,
and that /formats still advertises PDF support.
"""
import json
import os
import re
import sys

import pytest

# Make the project root importable so `import app` works regardless of cwd.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import app as app_module  # noqa: E402
from utils import csrf  # noqa: E402


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


TEMPLATES = os.path.join(PROJECT_ROOT, "templates")
NAV_RE = re.compile(r'<nav class="tool-nav">(.*?)</nav>', re.S)


def _tool_navs():
    """Each template with a tool nav, and that nav's markup."""
    navs = {}
    for name in sorted(os.listdir(TEMPLATES)):
        if not name.endswith(".html"):
            continue
        with open(os.path.join(TEMPLATES, name), encoding="utf-8") as fh:
            src = fh.read()
        found = NAV_RE.findall(src)
        if found or "tool-nav" in src:
            assert len(found) == 1, name
            navs[name] = found[0]
    return navs


def test_every_tool_nav_links_to_the_packet_builder():
    # The nav has no shared partial: each template carries its own. A new
    # template with a tool nav fails here until it links to /packet.
    navs = _tool_navs()
    assert "packet.html" in navs and "index.html" in navs
    for name, nav in navs.items():
        links = re.findall(r'<a href="(/[^"]*)"([^>]*)>([^<]*)</a>', nav)
        tools = [href for href, _, _ in links]
        assert tools.count("/packet") == 1, name
        # Beside Merge, as the packet builder sits beside the merge page.
        assert tools.index("/packet") == tools.index("/") + 1, name
        attrs = dict((href, (a, text)) for href, a, text in links)
        assert attrs["/packet"][1] == "Packet Builder", name
        active = [href for href, a, _ in links if 'class="active"' in a]
        if name == "packet.html":
            assert active == ["/packet"], name
        else:
            assert "/packet" not in active, name


def test_served_pages_link_to_the_packet_builder(client):
    for path in ("/", "/packet", "/convert", "/pages", "/print", "/export",
                 "/security", "/api", "/login", "/signup", "/forgot"):
        body = client.get(path).get_data(as_text=True)
        nav = NAV_RE.search(body)
        assert nav, path
        assert 'href="/packet"' in nav.group(1), path
    packet_nav = NAV_RE.search(client.get("/packet").get_data(as_text=True)).group(1)
    assert '<a href="/packet" class="active">Packet Builder</a>' in packet_nav
    assert packet_nav.count('class="active"') == 1


def test_packet_fixture_csrf_text_matches_the_server():
    # The page tells the CSRF refusal from other code-less 400s by the
    # error text, which packet-api.json pins (B2 deviation 7). If the
    # server's message changes, this fails before the page stops seeing it.
    fixture = os.path.join(PROJECT_ROOT, "tests", "fixtures", "packet-api.json")
    with open(fixture, encoding="utf-8") as fh:
        shared = json.load(fh)["shared"]["csrf"]
    assert shared["body"]["error"] == csrf._MESSAGE
    assert csrf._MESSAGE.startswith("Session expired")
