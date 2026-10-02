"""The upload limit, per request and per tier (design prelaunch-fixes v0.4 §7).

Every tier but paid gets ``DOCIST_MAX_UPLOAD_MB`` (default 50); paid gets
``DOCIST_MAX_UPLOAD_MB_PAID`` (default 90, which keeps the request body at
least 5 MB under Cloudflare's 100 MB cap on proxied requests). "MB" here is
MiB: the code multiplies by 1024 x 1024. Both limits are compared against the
whole request body.

Rule: the body limit is set per request before anything reads the body.
Werkzeug reads ``max_content_length`` once, when ``request.stream`` is first
built, so :func:`apply_limit` runs in app.py's before-request chain right
after ``identity.load_identity`` and before ``csrf.check_csrf``, the first
step that may parse the form. It also records ``g.request_started``, the
moment the request's render budget counts from (utils/render_opts.py).

Rule: a missed step fails closed. :class:`LimitedRequest` returns the limit
:func:`apply_limit` stored in the WSGI environ, else ``MAX_CONTENT_LENGTH``,
the free limit. Only that step can raise a request's limit.

Every 413 is JSON, the same shape as the daily limit's 429:
``{"error": ..., "code": "upload_too_large", "limit_mb": N}``.
"""
import time

from flask import Request, current_app, g, jsonify, request

from utils.identity import current_user
from utils.metering import tier

ENVIRON_KEY = 'docist.max_body'
MIB = 1024 * 1024


class LimitedRequest(Request):
    """``flask.Request`` whose body limit is the one set for this request."""

    @property
    def max_content_length(self):
        value = self.environ.get(ENVIRON_KEY)
        if value is not None:
            return value
        return super().max_content_length


def free_limit_bytes():
    return current_app.config['MAX_CONTENT_LENGTH']


def paid_limit_bytes():
    return current_app.config['MAX_CONTENT_LENGTH_PAID']


def limit_bytes(user):
    """The body limit for ``user``, in bytes. The one place it is computed."""
    if tier(user) == 'paid':
        return paid_limit_bytes()
    return free_limit_bytes()


def limit_mb(limit):
    """``limit`` bytes as the MB number users see (52428800 -> 50)."""
    value = limit / MIB
    return int(value) if value == int(value) else round(value, 1)


def too_large(limit):
    response = jsonify({
        'error': f'This upload is over the {limit_mb(limit)} MB limit.',
        'code': 'upload_too_large',
        'limit_mb': limit_mb(limit),
    })
    response.status_code = 413
    return response


def apply_limit():
    """Before-request step: set this request's body limit; maybe refuse at once.

    Reads only headers, never the body. A declared length over the limit gets
    the 413 here; a chunked body is cut at the limit when it is read.
    """
    g.request_started = time.monotonic()
    limit = limit_bytes(current_user())
    request.environ[ENVIRON_KEY] = limit
    length = request.content_length
    if length is not None and length > limit:
        return too_large(limit)
    return None


def init_app(app):
    app.request_class = LimitedRequest

    @app.errorhandler(413)
    def _too_large(_error):
        # Werkzeug raised while reading the body: the limit is this request's.
        return too_large(request.max_content_length or free_limit_bytes())

    @app.context_processor
    def _inject_upload_limit():
        limit = limit_bytes(current_user())
        return {'upload_limit_bytes': limit, 'upload_limit_mb': limit_mb(limit)}
