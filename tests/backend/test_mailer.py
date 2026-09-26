"""utils.mailer: backend selection, the three senders, never-raise."""
import json
import logging

import pytest

import app as flask_app_module
from utils import mailer

_ENV = (
    "DOCIST_EMAIL_BACKEND", "DOCIST_BASE_URL", "DOCIST_EMAIL_FROM",
    "DOCIST_SMTP_HOST", "DOCIST_SMTP_PORT", "DOCIST_SMTP_USER",
    "DOCIST_SMTP_PASSWORD", "DOCIST_SMTP_TLS", "DOCIST_RESEND_API_KEY",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls = []
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


class TestConsole:
    def test_default_backend_logs_and_succeeds(self, caplog):
        assert mailer.backend() == "console"
        assert mailer.configured() is False
        with caplog.at_level(logging.WARNING, logger="utils.mailer"):
            assert mailer.send("a@x.io", "Hello there", "the link: http://x/verify/abc")
        assert "Hello there" in caplog.text
        assert "http://x/verify/abc" in caplog.text

    def test_console_link_falls_back_to_request_root(self):
        with flask_app_module.app.test_request_context("/", base_url="http://dev.local:5010/"):
            assert mailer.absolute_url("/verify/t") == "http://dev.local:5010/verify/t"

    def test_base_url_wins_over_host(self, clean_env):
        clean_env.setenv("DOCIST_BASE_URL", "https://docist.example/")
        with flask_app_module.app.test_request_context("/", base_url="http://evil.example/"):
            assert mailer.absolute_url("/reset/t") == "https://docist.example/reset/t"


class TestSmtp:
    def test_starttls_login_send(self, clean_env, fake_smtp):
        clean_env.setenv("DOCIST_EMAIL_BACKEND", "smtp")
        clean_env.setenv("DOCIST_BASE_URL", "https://docist.example")
        clean_env.setenv("DOCIST_SMTP_HOST", "smtp.example")
        clean_env.setenv("DOCIST_SMTP_USER", "u")
        clean_env.setenv("DOCIST_SMTP_PASSWORD", "p")
        clean_env.setenv("DOCIST_EMAIL_FROM", "Docist <hi@docist.example>")
        assert mailer.configured() is True
        assert mailer.send("a@x.io", "Subj", "Body text") is True
        (smtp,) = fake_smtp.instances
        assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.example", 587, 10)
        assert smtp.calls[:2] == ["starttls", ("login", "u", "p")]
        (msg,) = smtp.sent
        assert msg["To"] == "a@x.io"
        assert msg["From"] == "Docist <hi@docist.example>"
        assert msg["Subject"] == "Subj"
        assert "Body text" in msg.get_content()

    def test_no_tls_no_login(self, clean_env, fake_smtp):
        clean_env.setenv("DOCIST_EMAIL_BACKEND", "smtp")
        clean_env.setenv("DOCIST_BASE_URL", "https://docist.example")
        clean_env.setenv("DOCIST_SMTP_HOST", "smtp.example")
        clean_env.setenv("DOCIST_SMTP_PORT", "2525")
        clean_env.setenv("DOCIST_SMTP_TLS", "none")
        assert mailer.send("a@x.io", "S", "B") is True
        (smtp,) = fake_smtp.instances
        assert smtp.port == 2525
        assert "starttls" not in smtp.calls
        assert not any(isinstance(c, tuple) for c in smtp.calls)

    def test_ssl_uses_smtp_ssl(self, clean_env, monkeypatch):
        used = []

        class FakeSSL(FakeSMTP):
            def __init__(self, *a, **kw):
                used.append("ssl")
                super().__init__(*a, **kw)

        monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSSL)
        monkeypatch.setattr(mailer.smtplib, "SMTP", None)  # must not be used
        clean_env.setenv("DOCIST_EMAIL_BACKEND", "smtp")
        clean_env.setenv("DOCIST_BASE_URL", "https://docist.example")
        clean_env.setenv("DOCIST_SMTP_HOST", "smtp.example")
        clean_env.setenv("DOCIST_SMTP_TLS", "ssl")
        assert mailer.send("a@x.io", "S", "B") is True
        assert used == ["ssl"]


class TestResend:
    def test_posts_json_with_bearer(self, clean_env, monkeypatch):
        captured = {}

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            captured["body"] = json.loads(req.data)
            captured["timeout"] = timeout
            return Resp()

        monkeypatch.setattr(mailer.urllib.request, "urlopen", fake_urlopen)
        clean_env.setenv("DOCIST_EMAIL_BACKEND", "resend")
        clean_env.setenv("DOCIST_BASE_URL", "https://docist.example")
        clean_env.setenv("DOCIST_RESEND_API_KEY", "re_test_123")
        assert mailer.send("a@x.io", "Subj", "Body") is True
        assert captured["url"] == "https://api.resend.com/emails"
        assert captured["method"] == "POST"
        assert captured["headers"]["authorization"] == "Bearer re_test_123"
        assert captured["headers"]["content-type"] == "application/json"
        assert captured["body"] == {
            "from": "Docist <no-reply@localhost>", "to": ["a@x.io"],
            "subject": "Subj", "text": "Body",
        }
        assert captured["timeout"] == 10


class TestRefusalsAndFailures:
    @pytest.mark.parametrize("backend", ["smtp", "resend"])
    def test_real_backend_without_base_url_refuses(self, clean_env, monkeypatch, backend):
        called = []
        monkeypatch.setattr(mailer.smtplib, "SMTP", lambda *a, **k: called.append(1))
        monkeypatch.setattr(mailer.urllib.request, "urlopen",
                            lambda *a, **k: called.append(1))
        clean_env.setenv("DOCIST_EMAIL_BACKEND", backend)
        clean_env.setenv("DOCIST_SMTP_HOST", "smtp.example")
        clean_env.setenv("DOCIST_RESEND_API_KEY", "re_x")
        assert mailer.send("a@x.io", "S", "B") is False
        assert called == []

    def test_real_backend_never_links_to_request_host(self, clean_env):
        clean_env.setenv("DOCIST_EMAIL_BACKEND", "smtp")
        with flask_app_module.app.test_request_context("/", base_url="http://evil.example/"):
            assert "evil.example" not in mailer.absolute_url("/reset/t")

    def test_smtp_failure_returns_false(self, clean_env, monkeypatch):
        def boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr(mailer.smtplib, "SMTP", boom)
        clean_env.setenv("DOCIST_EMAIL_BACKEND", "smtp")
        clean_env.setenv("DOCIST_BASE_URL", "https://docist.example")
        clean_env.setenv("DOCIST_SMTP_HOST", "smtp.example")
        assert mailer.send("a@x.io", "S", "B") is False

    def test_resend_failure_returns_false(self, clean_env, monkeypatch):
        def boom(*a, **k):
            raise OSError("network down")

        monkeypatch.setattr(mailer.urllib.request, "urlopen", boom)
        clean_env.setenv("DOCIST_EMAIL_BACKEND", "resend")
        clean_env.setenv("DOCIST_BASE_URL", "https://docist.example")
        clean_env.setenv("DOCIST_RESEND_API_KEY", "re_x")
        assert mailer.send("a@x.io", "S", "B") is False

    def test_unknown_backend_returns_false(self, clean_env):
        clean_env.setenv("DOCIST_EMAIL_BACKEND", "pigeon")
        assert mailer.get_sender() is None
        assert mailer.send("a@x.io", "S", "B") is False
