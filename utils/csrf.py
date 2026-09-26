"""Session-bound CSRF token, checked on every state-changing browser request.

The token lives in the signed session and reaches the page two ways: a hidden
``csrf_token`` field in server-rendered forms, and ``<meta name="csrf-token">``
which static/csrf.js copies into an ``X-CSRF-Token`` header on same-origin
fetch() calls. The header is read first, so a fetch upload is never parsed
just to find the token.

Exempt: the ``api`` blueprint (it never authenticates from the cookie, so
there is nothing to forge) and ``healthz``. ``CSRF_ENABLED`` switches the
check off for tests.
"""
import hmac
import secrets

from flask import current_app, jsonify, render_template, request, session

HEADER = 'X-CSRF-Token'
FIELD = 'csrf_token'
_CHECKED_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
_EXEMPT_ENDPOINTS = {'healthz'}
_MESSAGE = 'Session expired — reload the page and try again.'


def csrf_token():
    """This session's CSRF token, created on first use."""
    return session.setdefault('csrf', secrets.token_urlsafe(32))


def _refuse():
    # A form navigation gets a page; fetch() (Accept: */*) and API-style
    # clients (no Accept) get JSON the tool pages already know how to show.
    if request.accept_mimetypes.best == 'text/html':
        return render_template(
            'message.html', title='Session expired',
            body='This form was open too long or came from another site. '
                 'Reload the page and try again.',
            link_href='/', link_text='Back to Docist',
        ), 400
    response = jsonify({'error': _MESSAGE})
    response.status_code = 400
    return response


def check_csrf():
    """Return a 400 response when a checked request lacks a matching token."""
    if not current_app.config.get('CSRF_ENABLED', True):
        return None
    if request.method not in _CHECKED_METHODS:
        return None
    if request.blueprint == 'api' or request.endpoint in _EXEMPT_ENDPOINTS:
        return None
    expected = session.get('csrf')
    submitted = request.headers.get(HEADER)
    if submitted is None:
        submitted = request.form.get(FIELD)
    if (not expected or not submitted
            or not hmac.compare_digest(str(submitted).encode(), expected.encode())):
        return _refuse()
    return None


def init_app(app):
    app.jinja_env.globals['csrf_token'] = csrf_token
