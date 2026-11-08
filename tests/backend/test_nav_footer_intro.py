"""Every page's nav and footer, the intro strip on /, and the sample files.

The nav and the footer are shared partials (templates/_nav.html,
templates/_footer.html); these tests read the served pages, so a template
that drops either include, or marks the wrong link active, fails here.
"""
import io
import re

import pytest
from pypdf import PdfReader

import app as flask_app_module
from models import EmailToken, User, db

NAV_RE = re.compile(r'<nav class="tool-nav">(.*?)</nav>', re.S)
FOOTER_RE = re.compile(r'<footer class="site-footer">(.*?)</footer>', re.S)

# The eight tool pages and the nav link each marks active.
TOOL_PAGES = {
    "/": "/", "/packet": "/packet", "/convert": "/convert", "/pages": "/pages",
    "/print": "/print", "/export": "/export", "/security": "/security", "/api": "/api",
}


def _token(user, purpose):
    app = flask_app_module.app
    with app.app_context():
        raw = EmailToken.issue(db.session.get(User, user.id), purpose)
        db.session.commit()
    return raw


@pytest.fixture
def every_page(client, make_user, login):
    """{template: served body} for all fifteen page templates."""
    pages = {}
    for path in TOOL_PAGES:
        resp = client.get(path)
        assert resp.status_code == 200, path
        pages[path] = resp.get_data(as_text=True)
    for name in ("login", "signup", "forgot"):
        resp = client.get(f"/{name}")
        assert resp.status_code == 200, name
        pages[name] = resp.get_data(as_text=True)

    user = make_user(verified=False)
    reset = client.get(f"/reset/{_token(user, 'reset')}")
    verify = client.get(f"/verify/{_token(user, 'verify')}")
    dead = client.get("/reset/no-such-token")              # message.html
    assert (reset.status_code, verify.status_code, dead.status_code) == (200, 200, 400)
    pages["reset"] = reset.get_data(as_text=True)
    pages["verify"] = verify.get_data(as_text=True)
    pages["message"] = dead.get_data(as_text=True)
    assert "Choose a new password</h1>" in pages["reset"]
    assert "Verify your email</h1>" in pages["verify"]
    assert "Link expired</h1>" in pages["message"]

    login(client, user)
    resp = client.get("/account")
    assert resp.status_code == 200
    pages["account"] = resp.get_data(as_text=True)
    assert len(pages) == 15
    return pages


def _nav(body):
    found = NAV_RE.findall(body)
    assert len(found) == 1
    return found[0]


def _active_links(nav):
    return re.findall(r'<a href="([^"]+)" class="active">', nav)


def test_every_page_has_the_footer_links(every_page):
    for page, body in every_page.items():
        footers = FOOTER_RE.findall(body)
        assert len(footers) == 1, page
        footer = footers[0]
        assert footer.startswith("Docist by Ethan · "), page
        assert '<a href="https://github.com/CodeManEthan/docist">Source on GitHub</a>' in footer, page
        assert '<a href="https://codemanethan.github.io">Portfolio</a>' in footer, page


def test_tool_pages_mark_exactly_their_own_nav_link(every_page):
    for path, active in TOOL_PAGES.items():
        nav = _nav(every_page[path])
        assert _active_links(nav) == [active], path
        assert nav.count('class="active"') == 1, path


def test_other_pages_mark_no_nav_link(every_page):
    others = [page for page in every_page if page not in TOOL_PAGES]
    assert len(others) == 7
    for page in others:
        nav = _nav(every_page[page])
        assert 'class="active"' not in nav, page
        # The same eight tool links as everywhere else.
        assert len(re.findall(r'<a href="/[a-z]*">', nav)) >= 8, page


def test_nav_lists_the_eight_tools_in_order(every_page):
    nav = _nav(every_page["login"])
    hrefs = re.findall(r'<a href="([^"]+)"', nav)
    assert hrefs[:8] == list(TOOL_PAGES)
    assert '<a href="/security">Watermark &amp; Security</a>' in nav


def test_index_has_the_intro_strip_and_sample_button(client):
    body = client.get("/").get_data(as_text=True)
    strip = body[body.index('<section class="intro-strip"'):body.index("</section>")]
    assert body.index('class="intro-strip"') < body.index("<h1>")
    assert ("Docist merges, converts, OCRs, stamps and protects documents in your "
            "browser: seven tools and a REST API.") in strip
    assert '<button type="button" class="intro-btn" id="sampleBtn">Try with sample files</button>' in strip
    # Only / has the strip.
    assert 'class="intro-strip"' not in client.get("/convert").get_data(as_text=True)


@pytest.mark.parametrize("seconds, words", [
    (24 * 3600, "24 hours"),
    (3600, "1 hour"),
    (2 * 3600 + 1800, "2 hours"),
    (1800, "30 minutes"),
    (60, "1 minute"),
])
def test_strip_duration_follows_output_max_age(client, monkeypatch, seconds, words):
    app = flask_app_module.app
    monkeypatch.setitem(app.config, "OUTPUT_MAX_AGE", seconds)
    monkeypatch.setitem(app.config, "LIMIT_ANON", 7)
    body = client.get("/").get_data(as_text=True)
    assert ("No account needed: 7 operations a day (one operation is one finished "
            "tool run). Uploads to the tools are deleted after processing; results "
            f"are kept {words} for download, for your session only.") in body


def test_strip_limit_is_the_anonymous_limit_the_nav_shows(client, monkeypatch):
    app = flask_app_module.app
    monkeypatch.setitem(app.config, "LIMIT_ANON", 4)
    body = client.get("/").get_data(as_text=True)
    assert "No account needed: 4 operations a day" in body
    assert '<span class="nav-usage">4 of 4 left today</span>' in body


@pytest.mark.parametrize("name, magic", [
    ("sample-report.pdf", b"%PDF-"),
    ("sample-note.md", b"# "),
    ("sample-chart.png", b"\x89PNG\r\n\x1a\n"),
])
def test_sample_files_are_served(client, name, magic):
    resp = client.get(f"/static/samples/{name}")
    try:
        assert resp.status_code == 200
        data = resp.get_data()
    finally:
        resp.close()
    assert data.startswith(magic)
    if name.endswith(".pdf"):
        assert len(PdfReader(io.BytesIO(data)).pages) == 2
