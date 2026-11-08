"""The /api docs page: every /api/v1/ route documented, curl examples built
from the origin the page was requested at, the status list, the health line,
and a first paragraph that follows DOCIST_API_ANONYMOUS."""
import html
import re

import pytest

import app as flask_app_module
from routes import api
from routes.errors import SERVER_ERROR_MESSAGE

APP = flask_app_module.app
ORIGIN = "https://docs.example"
CLAIM = "Every one-file tool, scriptable. The Packet Builder is browser-only."


def text(fragment):
    """``fragment`` without tags, entities decoded, whitespace collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def first_paragraph(body):
    match = re.search(r'<p class="subtitle">(.*?)</p>', body, re.S)
    assert match, "no first paragraph"
    return text(match.group(1))


def section(body, element_id):
    match = re.search(rf'<div class="api-note" id="{element_id}">(.*?)</div>', body, re.S)
    assert match, element_id
    return match.group(1)


@pytest.fixture
def config(client):
    """Set app config keys for one test and put them back after."""
    saved = {}

    def set_(**values):
        for key, value in values.items():
            saved.setdefault(key, APP.config[key])
            APP.config[key] = value

    yield set_
    APP.config.update(saved)


def page(client):
    response = client.get("/api", base_url=ORIGIN)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_curl_examples_use_the_request_origin(client):
    body = page(client)
    curls = [html.unescape(line).strip() for line in body.splitlines()
             if html.unescape(line).strip().startswith(("curl ", '<pre class="api-curl">curl '))]
    assert len(curls) >= 17
    for line in curls:
        assert f"{ORIGIN}/api/v1/" in line, line
    assert "localhost" not in body


def test_every_api_v1_route_has_its_own_section(client):
    body = page(client)
    rules = [rule for rule in APP.url_map.iter_rules() if rule.rule.startswith("/api/v1/")]
    assert len(rules) >= 17
    for rule in rules:
        verb = "POST" if "POST" in rule.methods else "GET"
        assert f'<span class="verb">{verb}</span>{rule.rule}</h2>' in body, rule.rule


def test_the_claim_names_the_packet_builder(client):
    assert first_paragraph(page(client)).startswith(CLAIM)


def test_one_status_list_with_every_status(client):
    errors = section(page(client), "errors")
    items = [text(item) for item in re.findall(r"<li>(.*?)</li>", errors, re.S)]
    assert [item.split()[0] for item in items] == ["400", "401", "413", "429", "500"]
    server = items[-1]
    assert f'{{"error": "{SERVER_ERROR_MESSAGE}", "code": "server_error"}}' in server
    assert '"code": "upload_too_large"' in items[2]
    assert '"code": "daily_limit"' in items[3] and "Retry-After" in items[3]


def test_the_500_line_matches_what_the_api_answers(client, config, builders, tmp_path,
                                                  monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("internal")

    monkeypatch.setattr(api, "run_in_job", boom)
    config(API_ANONYMOUS=True)
    pdf = builders.pdf(tmp_path / "a.pdf", pages=1, marker="One")
    with open(pdf, "rb") as handle:
        answer = client.post("/api/v1/pages/rotate",
                             data={"file": (handle, "a.pdf"), "angle": "90"},
                             content_type="multipart/form-data")
    assert answer.status_code == 500
    shown = '{"error": "%s", "code": "server_error"}' % answer.get_json()["error"]
    assert answer.get_json()["code"] == "server_error"
    assert shown in text(section(page(client), "errors"))


def test_health_line_names_every_healthz_field(client):
    body = page(client)
    match = re.search(r"<li><strong>Health:</strong>(.*?)</li>", body, re.S)
    assert match
    line = text(match.group(1))
    for field in client.get("/healthz").get_json():
        assert f'"{field}"' in line, field
    assert '"commit"' in line and "DOCIST_COMMIT" in line


def test_keyed_only_server(client, config):
    config(API_ANONYMOUS=False, LIMIT_ANON=7)
    body = page(client)
    first = first_paragraph(body)
    assert "every POST needs an API key" in first
    assert 'href="/account"' in re.search(r'<p class="subtitle">(.*?)</p>', body, re.S).group(1)
    assert "guest limit" not in first and "keyless" not in first
    errors = text(section(body, "errors"))
    assert "API key required. Create one at /account." in errors


def test_keyless_server_names_both_ways(client, config):
    config(API_ANONYMOUS=True, LIMIT_ANON=7)
    body = page(client)
    first = first_paragraph(body)
    assert "API key from your account page" in first
    assert "with no key at all" in first
    assert "guest limit of 7 operations a day" in first
    assert "every POST needs an API key" not in first
    errors = text(section(body, "errors"))
    assert "API key required" not in errors
    assert "Invalid or revoked API key." in errors
