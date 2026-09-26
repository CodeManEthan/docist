"""Who is making this request: client IP, signed-in user or API key, owner tag.

``load_identity()`` runs in app.py's before-request chain and sets ``g.user``:

  * browser routes read the signed session cookie (``uid`` + ``epoch``); the
    user is kept only if they exist, are not disabled, and their
    ``session_epoch`` still matches -- bumping the epoch signs out everywhere;
  * ``/api/v1`` ignores the cookie and authenticates only by
    ``Authorization: Bearer <key>``. A bad key is a 401, never a fall back to
    anonymous; no key on a POST is a 401 unless ``API_ANONYMOUS`` is set.

Proxy trust is not decided here: app.py wraps the WSGI app in ProxyFix when
``DOCIST_TRUSTED_PROXIES`` > 0, so ``request.remote_addr`` is already the
client as far as the configured hops can tell.
"""
import hashlib
import hmac
import ipaddress
import secrets

from flask import current_app, g, jsonify, request, session

from models import ApiKey, User, db


def client_ip():
    """The client's address; an IPv6 client is grouped by its /64 network."""
    addr = request.remote_addr or '0.0.0.0'
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f'{ip}/64', strict=False))
    return str(ip)


def _hmac16(message):
    key = current_app.config['SECRET_KEY']
    if isinstance(key, str):
        key = key.encode()
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()[:16]


def anon_subject():
    """The usage subject for this client's IP, keyed so raw IPs are never stored."""
    return 'ip:' + _hmac16(client_ip())


def _bearer_token(header):
    """Pull ``<token>`` out of an ``Authorization: Bearer <token>`` header."""
    scheme, _, token = str(header or '').partition(' ')
    if scheme.strip().lower() != 'bearer':
        return ''
    return token.strip()


def _api_error(message):
    response = jsonify({'error': message})
    response.status_code = 401
    return response


def load_identity():
    """Set ``g.user`` for this request; may return a 401 response on /api/v1."""
    g.user = None
    if request.endpoint == 'static':
        return None

    if request.blueprint == 'api':
        header = request.headers.get('Authorization')
        if header is not None:
            key = ApiKey.lookup(_bearer_token(header))
            if key is None:
                return _api_error('Invalid or revoked API key.')
            g.user = key.user
            return None
        if request.method == 'POST' and not current_app.config.get('API_ANONYMOUS'):
            return _api_error('API key required. Create one at /account.')
        return None

    uid = session.get('uid')
    if uid is None:
        return None
    user = db.session.get(User, uid)
    if (user is None or user.disabled
            or user.session_epoch != session.get('epoch')):
        session.pop('uid', None)
        session.pop('epoch', None)
        return None
    g.user = user
    return None


def current_user():
    """The signed-in (or API-key) user for this request, or None."""
    return g.get('user')


def login_user(user):
    """Start a fresh session for ``user`` (clearing it also fixes fixation)."""
    session.clear()
    session['uid'] = user.id
    session['epoch'] = user.session_epoch
    session.permanent = True
    g.user = user


def logout_user():
    """Drop the login but keep the rest of the session (e.g. the CSRF token)."""
    session.pop('uid', None)
    session.pop('epoch', None)
    g.user = None


def owner_tag():
    """16 hex binding a stored result to this user, or to this browser session."""
    user = current_user()
    if user is not None:
        return _hmac16(f'user:{user.id}')
    owner = session.get('owner')
    if not owner:
        owner = session['owner'] = secrets.token_hex(16)
    return _hmac16(f'anon:{owner}')


def init_app(app):
    @app.context_processor
    def _inject_current_user():
        return {'current_user': current_user()}
