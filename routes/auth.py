"""Auth gate: a single shared password protects every page and endpoint.

The gate is OFF when ``ACCESS_PASSWORD`` is empty (local/dev use) and ON as
soon as a password is configured (set the ``DOCIST_PASSWORD`` env var). It is
enforced app-wide via ``before_app_request`` — new blueprints are protected
automatically, with only the login page, static assets and the health check
exempt.

Browser page loads are redirected to /login; JSON/API requests get a 401 so
the frontend fetch() error paths surface a clear message instead of HTML.

Non-browser clients (curl, scripts hitting /api/v1/...) can skip the cookie
entirely and send ``Authorization: Bearer <password>`` -- the same shared
password, compared in constant time exactly like the login form.

Public demo: with ``DOCIST_PUBLIC_DEMO=1`` the login page prints the password
and prefills the box, so a visitor from the portfolio gets in with one click
while crawlers and scripts still hit a gate.
"""
import hmac

from flask import (
    Blueprint, current_app, jsonify, redirect, render_template, request,
    session, url_for,
)

bp = Blueprint('auth', __name__)

SESSION_KEY = 'docist_authed'

# Endpoints reachable without a session even when the gate is on.
_EXEMPT_ENDPOINTS = {'auth.login', 'static', 'healthz'}


def gate_enabled():
    return bool(current_app.config.get('ACCESS_PASSWORD'))


def is_authed():
    return session.get(SESSION_KEY) is True


def shown_password():
    """The password to print on the login page, or '' when it stays secret."""
    if current_app.config.get('PUBLIC_DEMO'):
        return current_app.config.get('ACCESS_PASSWORD') or ''
    return ''


def _bearer_token(header):
    """Pull ``<token>`` out of an ``Authorization: Bearer <token>`` header."""
    if not header:
        return ''
    scheme, _, token = str(header).partition(' ')
    if scheme.strip().lower() != 'bearer':
        return ''
    return token.strip()


def has_valid_bearer():
    """True when the request carries a Bearer token matching the password.

    Lets API clients authenticate per-request without a session cookie. The
    comparison is constant-time, mirroring the login form check.
    """
    expected = current_app.config.get('ACCESS_PASSWORD') or ''
    if not expected:
        return False
    token = _bearer_token(request.headers.get('Authorization'))
    if not token:
        return False
    return hmac.compare_digest(token.encode(), expected.encode())


@bp.app_context_processor
def inject_auth_state():
    """Let templates render the Sign out link only when it means something."""
    return {'auth_enabled': gate_enabled(), 'authed': is_authed()}


@bp.before_app_request
def require_login():
    if not gate_enabled() or is_authed():
        return None
    # API clients present the shared password as a Bearer token instead of a
    # cookie; a valid one authenticates this request (no session is created).
    if has_valid_bearer():
        return None
    if (request.endpoint or '') in _EXEMPT_ENDPOINTS:
        return None
    # A GET is a browser navigation unless the client explicitly asked for
    # something other than HTML (fetch() calls send Accept: application/json).
    accepts = request.accept_mimetypes
    if request.method == 'GET' and (not accepts or accepts.accept_html):
        return redirect(url_for('auth.login', next=request.path))
    return jsonify({'error': 'Authentication required. Reload and sign in.'}), 401


def _safe_next(target):
    """Only allow same-site relative redirect targets (no open redirects)."""
    if target and target.startswith('/') and not target.startswith('//'):
        return target
    return '/'


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if not gate_enabled() or is_authed():
        return redirect('/')

    if request.method == 'POST':
        submitted = request.form.get('password', '')
        expected = current_app.config['ACCESS_PASSWORD']
        if hmac.compare_digest(submitted.encode(), expected.encode()):
            session[SESSION_KEY] = True
            return redirect(_safe_next(request.form.get('next')))
        return render_template(
            'login.html', error='Wrong password.',
            next=_safe_next(request.form.get('next')),
            shown_password=shown_password(),
        ), 401

    return render_template(
        'login.html', error=None, next=_safe_next(request.args.get('next')),
        shown_password=shown_password(),
    )


@bp.route('/logout', methods=['GET', 'POST'])
def logout():
    session.pop(SESSION_KEY, None)
    return redirect(url_for('auth.login') if gate_enabled() else '/')
