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

Rule: the paid limit raises a request's limit only where B2 bounds the work
it lets through: Merge and Convert, in the browser and on the API
(``PAID_ENDPOINTS``). There, only a PDF or a Word file the Word engine will
render may be over the free limit (the routes check each file with
:func:`file_limit`), and form text fields never take more memory than the
free limit allows. Every other endpoint, and every other file, keeps the free
limit for everyone, so the larger limit doesn't enlarge the resource holes
the 2026-10-02 critic review found on main (raster and frame bombs, archive
expansion, split parts, results read into memory).

Rule: a missed step fails closed. :class:`LimitedRequest` returns the limit
:func:`apply_limit` stored in the WSGI environ, else ``MAX_CONTENT_LENGTH``,
the free limit. Only that step can raise a request's limit.

Every 413 is JSON, the same shape as the daily limit's 429:
``{"error": ..., "code": "upload_too_large", "limit_mb": N}``.
"""
import os
import time

from flask import Request, current_app, g, has_request_context, jsonify, request
from werkzeug.utils import secure_filename

from pdf_ops import limits, office
from utils.identity import current_user
from utils.metering import tier
from utils.validation import UploadValidationError

ENVIRON_KEY = 'docist.max_body'
MIB = 1024 * 1024

# The endpoints a paid request may send more than the free limit to.
PAID_ENDPOINTS = frozenset({
    'merge.upload_files', 'api.api_merge', 'convert.run_convert', 'api.api_convert',
})
# A tool page's own uploads go to this endpoint (for the page's meta tag).
_PAGE_UPLOADS = {'merge.index': 'merge.upload_files', 'convert.convert_index': 'convert.run_convert'}


class LimitedRequest(Request):
    """``flask.Request`` whose body limit is the one set for this request.

    Each non-file multipart field is capped at ``limits.FORM_FIELD_BYTES``
    (1 MiB). Werkzeug 3.0.6 enforces ``max_form_memory_size`` per field
    (GHSA-q34m-jh98-gwm2); Flask 3.0.0 has no setting for it, so it is set
    here (design launch-hardening §11).
    """

    @property
    def max_form_memory_size(self):
        return limits.FORM_FIELD_BYTES

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


def limit_bytes(user, endpoint=None):
    """The body limit for ``user`` sending to ``endpoint``, in bytes. The one
    place it is computed: the paid limit for a paid user on
    ``PAID_ENDPOINTS``, else the free limit."""
    if tier(user) == 'paid' and endpoint in PAID_ENDPOINTS:
        return paid_limit_bytes()
    return free_limit_bytes()


def upload_ext(upload):
    """An upload's extension, from the name the user sent.

    Not from ``secure_filename``, which turns ``отчёт.pdf`` into ``pdf``
    with no extension (design launch-hardening §13).
    """
    return os.path.splitext(upload.filename or '')[1].lower()


def merge_plan(files, convertible):
    """Where each of a merge request's files is saved, checked before any is.

    ``files`` are the request's ``files[]`` uploads; ``convertible(ext)``
    says whether a non-PDF extension has a converter. An empty file part
    (no filename) is ignored, since the browser sends one when nothing is
    chosen. Returns ``[(upload, relative path, ext, bookmark title, name
    shown), ...]`` with each file in its own ``<index>/`` directory, so
    same-named uploads can't overwrite each other. Raises
    :class:`UploadValidationError` naming every file, by the name the user
    sent, that is neither a PDF nor convertible: the whole request is
    refused, nothing is skipped.
    """
    plan = []
    unsupported = []
    for index, upload in enumerate(files):
        if not upload or not upload.filename:
            continue
        raw = upload.filename
        ext = upload_ext(upload)
        if ext != '.pdf' and not convertible(ext):
            unsupported.append(raw)
            continue
        safe = secure_filename(raw)
        stem = safe[:-len(ext)] if ext and safe.lower().endswith(ext) else ''
        if stem:
            name, title = f'{stem}{ext}', stem
        else:
            name = f'file{index}{ext}'
            title = os.path.splitext(os.path.basename(raw))[0] or f'file{index}'
        plan.append((upload, os.path.join(str(index), name), ext, title, name))
    if unsupported:
        raise UploadValidationError(limits.merge_unsupported_message(unsupported))
    return plan


def merge_large_ok(ext, render_opts):
    """Merge: only a PDF, or a Word file the Word engine renders, may be over
    the free limit."""
    return ext == '.pdf' or (ext == '.docx' and render_opts.word_engine == 'libreoffice')


def convert_large_ok(src_ext, target, render_opts):
    """Convert: only a Word file the Word engine renders to PDF may be over the
    free limit."""
    return (src_ext == '.docx' and target == '.pdf'
            and render_opts.word_engine == 'libreoffice')


def _upload_size(upload):
    """Bytes in a parsed upload (a FileStorage), read from its spooled stream."""
    stream = upload.stream
    here = stream.tell()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(here)
    return size


def charge_uploads(uploads_and_flags):
    """Check a request's uploads against its budget before any is saved or
    converted; raise :class:`utils.validation.UploadValidationError`.

    ``uploads_and_flags`` is ``(FileStorage, large_ok)`` pairs. Rule: the
    files a route can't bound at the paid size (``large_ok`` False) share the
    free limit across the whole request, so a paid request carries no more of
    them than a free request can. The others are bounded only by the
    request's body limit.
    """
    limit = free_limit_bytes()
    used = 0
    for upload, large_ok in uploads_and_flags:
        if large_ok:
            continue
        used += _upload_size(upload)
        if used > limit:
            raise UploadValidationError(
                f"Files of this kind can't be more than {limit_mb(limit)} MB in one request.")


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
    limit = limit_bytes(current_user(), request.endpoint)
    request.environ[ENVIRON_KEY] = limit
    length = request.content_length
    if length is not None and length > limit:
        return too_large(limit)
    return None


def init_app(app):
    app.request_class = LimitedRequest

    @app.errorhandler(413)
    def _too_large(_error):
        # Werkzeug raised while reading the body, and raises the same error
        # for a body over the limit, a text field over FORM_FIELD_BYTES and
        # more than max_form_parts parts. A declared length says which.
        limit = request.max_content_length or free_limit_bytes()
        length = request.content_length
        if length is not None and length > limit:
            return too_large(limit)
        if length is not None:
            message = limits.FORM_MESSAGE      # within the limit: a field or the parts
        else:
            message = limits.CHUNKED_MESSAGE   # chunked: any of the three
        response = jsonify({'error': message, 'code': 'upload_too_large',
                            'limit_mb': limit_mb(limit)})
        response.status_code = 413
        return response

    @app.context_processor
    def _inject_upload_limit():
        # The number the page's own uploads will meet on the server.
        endpoint = _PAGE_UPLOADS.get(request.endpoint) if has_request_context() else None
        if endpoint == 'convert.run_convert' and not office.available():
            endpoint = None   # Convert's only large file is a Word file for the engine
        limit = limit_bytes(current_user(), endpoint)
        return {'upload_limit_bytes': limit, 'upload_limit_mb': limit_mb(limit),
                'paid_upload_limit_mb': limit_mb(paid_limit_bytes())}
