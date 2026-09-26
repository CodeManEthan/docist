"""Client IP (proxy trust, IPv6 grouping), anonymous subject, session
loading (epoch, disabled users) and result owner tags."""
from types import SimpleNamespace

import pytest
from flask import g
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.test import EnvironBuilder

import app as flask_app_module
from models import User, db
from utils import identity

app = flask_app_module.app


def _ip_via_proxyfix(trusted, remote, xff):
    """Run a request environ through ProxyFix(x_for=trusted) as app.py would,
    then return client_ip() for the resulting remote address."""
    environ = EnvironBuilder(
        headers={"X-Forwarded-For": xff}, environ_base={"REMOTE_ADDR": remote},
    ).get_environ()
    seen = {}

    def inner(env, start_response):
        seen["remote"] = env["REMOTE_ADDR"]
        return []

    wsgi = ProxyFix(inner, x_for=trusted) if trusted else inner
    wsgi(environ, lambda *a: None)
    with app.test_request_context(environ_base={"REMOTE_ADDR": seen["remote"]}):
        return identity.client_ip()


class TestClientIp:
    def test_zero_trusted_proxies_uses_the_socket_peer(self):
        assert _ip_via_proxyfix(0, "10.0.0.1", "203.0.113.9") == "10.0.0.1"

    def test_one_trusted_proxy_uses_the_last_xff_hop(self):
        assert _ip_via_proxyfix(1, "10.0.0.1", "203.0.113.9") == "203.0.113.9"

    def test_spoofed_prepended_xff_is_ignored(self):
        # The client sent "X-Forwarded-For: 6.6.6.6"; the proxy appended the
        # real address. With one trusted hop only the last entry counts.
        assert _ip_via_proxyfix(1, "10.0.0.1", "6.6.6.6, 203.0.113.9") == "203.0.113.9"

    def test_app_without_proxyfix_ignores_xff(self):
        with app.test_request_context(
            headers={"X-Forwarded-For": "6.6.6.6"},
            environ_base={"REMOTE_ADDR": "10.0.0.1"},
        ):
            assert identity.client_ip() == "10.0.0.1"

    def test_ipv6_grouped_by_64(self):
        with app.test_request_context(environ_base={"REMOTE_ADDR": "2001:db8:1:2:aaaa::1"}):
            first = identity.client_ip()
        with app.test_request_context(environ_base={"REMOTE_ADDR": "2001:db8:1:2:bbbb::9"}):
            second = identity.client_ip()
        with app.test_request_context(environ_base={"REMOTE_ADDR": "2001:db8:1:3::1"}):
            other = identity.client_ip()
        assert first == second == "2001:db8:1:2::/64"
        assert other != first

    def test_missing_remote_addr(self):
        with app.test_request_context(environ_base={"REMOTE_ADDR": ""}):
            assert identity.client_ip() == "0.0.0.0"


class TestAnonSubject:
    def test_stable_per_ip_and_hides_the_ip(self):
        with app.test_request_context(environ_base={"REMOTE_ADDR": "203.0.113.9"}):
            a = identity.anon_subject()
        with app.test_request_context(environ_base={"REMOTE_ADDR": "203.0.113.9"}):
            b = identity.anon_subject()
        with app.test_request_context(environ_base={"REMOTE_ADDR": "203.0.113.10"}):
            c = identity.anon_subject()
        assert a == b != c
        assert a.startswith("ip:") and len(a) == 3 + 16
        assert "203" not in a


def _session(client):
    with client.session_transaction() as sess:
        return dict(sess)


class TestLoadIdentity:
    def test_valid_session_keeps_the_user(self, client, make_user, login):
        user = make_user()
        login(client, user)
        client.get("/")
        assert _session(client)["uid"] == user.id

    def test_epoch_mismatch_drops_the_session(self, client, make_user, login):
        user = make_user()
        login(client, user)
        with app.app_context():
            db.session.get(User, user.id).bump_epoch()
            db.session.commit()
        client.get("/")
        sess = _session(client)
        assert "uid" not in sess and "epoch" not in sess

    def test_disabled_user_drops_the_session(self, client, make_user, login):
        user = make_user()
        login(client, user)
        with app.app_context():
            db.session.get(User, user.id).disabled = True
            db.session.commit()
        client.get("/")
        assert "uid" not in _session(client)

    def test_unknown_user_drops_the_session(self, client, login):
        login(client, SimpleNamespace(id=999, session_epoch=0))
        client.get("/")
        assert "uid" not in _session(client)

    def test_login_user_clears_and_sets(self, client, make_user):
        user = make_user()
        with app.test_request_context():
            from flask import session
            session["owner"] = "stale"
            identity.login_user(user)
            assert session["uid"] == user.id and session["epoch"] == 0
            assert session.permanent and "owner" not in session
            assert identity.current_user() is user
            identity.logout_user()
            assert "uid" not in session and identity.current_user() is None


class TestOwnerTag:
    def test_differs_by_user(self):
        with app.test_request_context():
            g.user = SimpleNamespace(id=1)
            one = identity.owner_tag()
            g.user = SimpleNamespace(id=2)
            two = identity.owner_tag()
        with app.test_request_context():
            g.user = SimpleNamespace(id=1)
            again = identity.owner_tag()
        assert one != two and one == again
        assert len(one) == 16 and int(one, 16) >= 0

    def test_differs_by_anonymous_session_and_is_stable_within_one(self):
        with app.test_request_context():
            g.user = None
            first = identity.owner_tag()
            assert identity.owner_tag() == first
        with app.test_request_context():
            g.user = None
            second = identity.owner_tag()
        assert first != second

    def test_signed_in_tag_differs_from_anonymous(self):
        with app.test_request_context():
            g.user = None
            anon = identity.owner_tag()
            g.user = SimpleNamespace(id=1)
            assert identity.owner_tag() != anon


@pytest.mark.parametrize("header", ["Bearer x", "Basic y"])
def test_api_bad_key_is_401_even_on_get(client, header):
    response = client.get("/api/v1/formats", headers={"Authorization": header})
    assert response.status_code == 401
