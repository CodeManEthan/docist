"""Packet staging endpoints: stage, hold, and page renders (design §4.2, §4.3).

Blueprint ``packet_stage``, apart from the page (``routes/packet.py``) and
the build (``routes/packet_build.py``). Nothing here is metered
(``METERED_BLUEPRINTS`` names ``packet_build`` only); POSTs share the app's
POST rate limit and CSRF check.

Every refusal is JSON ``{error, code}`` with the codes in
``tests/fixtures/packet-api.json``. A ``LimitError`` from a route's own job
is answered here with a code, ahead of the app-wide handler, which has none.

Work on upload content runs in the request's job (launch-hardening Rule A):
the stage's conversion, page count and first thumbnails, and every render
that isn't already cached. The request's temp directory sits under the
store's ``.tmp/`` so a finished entry or thumbnail moves in with one rename.
"""
import hashlib
import json
import logging
import os
import re

from flask import Blueprint, current_app, jsonify, make_response, request

from converters import get_converter
from pdf_ops import limits
from pdf_ops.limits import LimitError
from utils import staging
from utils.identity import current_user, owner_tag
from utils.metering import resolve
from utils.ratelimit import RateLimiter
from utils.render_opts import RenderOptionsError, from_form, run_in_job
from utils.uploads import charge_uploads, limit_bytes, limit_mb, merge_large_ok, merge_plan, upload_ext
from utils.validation import UploadValidationError, validate_upload

log = logging.getLogger(__name__)

bp = Blueprint('packet_stage', __name__)

STAGE_ENDPOINT = 'packet_stage.stage'
JSON_BODY_BYTES = limits.FORM_FIELD_BYTES   # hold and build bodies
PACKET_RENDERS_PER_MINUTE = 60
BUSY_STAGE_SECONDS = 3
BUSY_RENDER_SECONDS = 2
CACHE_CONTROL = 'private, max-age=3600'
_INDEX_RE = re.compile(r'[0-9]{1,6}')


# ---------------------------------------------------------------------------
# Shared helpers (routes/packet_build.py uses these too)
# ---------------------------------------------------------------------------
def refusal(status, code, error, headers=None, **extra):
    response = jsonify({'error': error, 'code': code, **extra})
    response.status_code = status
    for name, value in (headers or {}).items():
        response.headers[name] = value
    return response


def busy(seconds, error='Another file is still being prepared. Trying again shortly.', **extra):
    return refusal(429, 'busy', error, {'Retry-After': str(max(1, int(seconds)))}, **extra)


def store():
    return staging.Store.from_config(current_app.config)


def subject():
    """Metering's subject: the IP hash for anonymous and unverified users,
    the user id for verified ones. A new cookie doesn't change it."""
    return resolve(current_user())[0]


def packet_limit_bytes(user):
    """The per-build cap (§4.6): the tier's request limit."""
    return limit_bytes(user, STAGE_ENDPOINT)


def mb(nbytes):
    """Bytes as the MB number users see."""
    value = nbytes / staging.MIB
    return f'{value:.0f}' if value >= 10 else f'{value:.1f}'.rstrip('0').rstrip('.')


class _Malformed:
    """A body that isn't JSON (read_json_body's sentinel)."""


MALFORMED = _Malformed()


def read_json_body():
    """``(value, None)`` or ``(None, response)``. The body is read only up to
    ``JSON_BODY_BYTES`` (1 MiB), so a large body is never parsed in the
    worker; a larger one is the 413. A body that isn't JSON gives
    ``MALFORMED``."""
    length = request.content_length
    raw = b''
    if length is None or length <= JSON_BODY_BYTES:
        raw = request.stream.read(JSON_BODY_BYTES + 1)
    if (length is not None and length > JSON_BODY_BYTES) or len(raw) > JSON_BODY_BYTES:
        cap = limit_mb(JSON_BODY_BYTES)
        return None, refusal(413, 'upload_too_large',
                             f'This request is over the {cap} MB limit.', limit_mb=cap)
    try:
        return json.loads(raw), None
    except (ValueError, RecursionError):
        return MALFORMED, None


# ---------------------------------------------------------------------------
# POST /packet/stage
# ---------------------------------------------------------------------------
def _bad_stage():
    return refusal(400, 'bad_request', 'Bad stage request.')


def _hash_upload(upload):
    """SHA-256 hex of an upload's bytes, read from its spooled stream."""
    digest = hashlib.sha256()
    stream = upload.stream
    stream.seek(0)
    while True:
        chunk = stream.read(1 << 20)
        if not chunk:
            break
        digest.update(chunk)
    size = stream.tell()
    stream.seek(0)
    return digest.hexdigest(), size


def _shown_name(raw):
    name = re.split(r'[\\/]', raw or '')[-1].strip()
    return name[:255] or 'file'


def _stage_body(meta, name=None):
    return {
        'stage_id': meta['id'], 'pages': meta['pages'], 'name': name or meta['name'],
        'type': meta['type'], 'key': meta['key'], 'bytes': meta['bytes'],
    }


def _quota(st, subj, holder, code):
    if code == 'staging_full':
        return refusal(400, 'staging_full', 'Docist is busy, try again later.')
    numbers = st.usage(subj, holder)
    return refusal(400, 'staging_quota',
                   f"Over your staging limit of {mb(numbers['cap_bytes'])} MB "
                   f"or {numbers['cap_files']} files.", **numbers)


@bp.route('/packet/stage', methods=['POST'])
def stage():
    uploads = request.files.getlist('file')
    if len(uploads) != 1 or set(request.files.keys()) != {'file'} or not uploads[0].filename:
        return _bad_stage()
    holder = request.form.get('holder', '')
    if not staging.valid_holder(holder):
        return _bad_stage()
    user = current_user()
    try:
        render_opts = from_form(request.form, user)
    except RenderOptionsError:
        return _bad_stage()
    upload = uploads[0]

    # Before anything is saved: the per-file limit, exactly as /upload does
    # (only a PDF or a Word-engine .docx may be over the free limit), and a
    # type Docist can make a PDF from.
    ext = upload_ext(upload)
    try:
        charge_uploads([(upload, merge_large_ok(ext, render_opts))])
        plan = merge_plan([upload], get_converter)
    except UploadValidationError as exc:
        return refusal(400, 'rejected', str(exc))
    _upload, relative, _ext, _title, _name = plan[0]

    digest, size = _hash_upload(upload)
    key = staging.make_key(digest, ext, render_opts.word_engine, render_opts.paper)
    file_type = ext.lstrip('.')
    name = _shown_name(upload.filename)
    owner = owner_tag()
    subj = subject()
    st = store()

    # Dedup: the same owner's live entry with this key costs no job and no bytes.
    meta = st.find(owner, subj, key, file_type, holder)
    if meta is not None:
        return jsonify(_stage_body(meta, name))

    code = st.check_room(subj, size)
    if code is not None:
        return _quota(st, subj, holder, code)

    limit = packet_limit_bytes(user)
    with st.temp_dir() as tmpdir:
        upload_path = os.path.join(tmpdir, 'upload', relative)
        os.makedirs(os.path.dirname(upload_path))
        upload.save(upload_path)
        try:
            validate_upload(upload_path, ext)
        except UploadValidationError as exc:
            return refusal(400, 'rejected', str(exc))
        entry_src = os.path.join(tmpdir, 'entry')
        with st.lane(subj, 'work') as taken:
            if not taken:
                return busy(BUSY_STAGE_SECONDS)
            try:
                done = run_in_job(
                    lambda: staging.stage_work(upload_path, ext, render_opts, entry_src), tmpdir)
            except staging.StageRefusal as exc:
                return refusal(400, exc.code, exc.message)
            except LimitError as exc:
                return refusal(400, 'rejected', str(exc))
            except Exception:
                log.exception('packet stage failed')
                return refusal(500, 'server_error', "Docist couldn't stage this file. Try again.")

        if done['pdf_bytes'] > limit:
            return refusal(400, 'too_large',
                           f"This file is {mb(done['pdf_bytes'])} MB as a PDF. "
                           f"A packet can use at most {mb(limit)} MB of files.",
                           limit_bytes=limit)
        meta, code = st.add(entry_src, owner=owner, subject=subj, holder=holder, key=key,
                            name=name, file_type=file_type, pages=done['pages'])
        if code is not None:
            return _quota(st, subj, holder, code)
    return jsonify(_stage_body(meta))


# ---------------------------------------------------------------------------
# POST /packet/hold
# ---------------------------------------------------------------------------
def _bad_hold():
    return refusal(400, 'bad_request', 'Bad hold request.')


def _id_list(value):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return list(dict.fromkeys(value))


@bp.route('/packet/hold', methods=['POST'])
def hold():
    body, response = read_json_body()
    if response is not None:
        return response
    if not isinstance(body, dict):
        return _bad_hold()
    holder = body.get('holder')
    keep = _id_list(body.get('keep'))
    release = _id_list(body.get('release'))
    if (not staging.valid_holder(holder) or keep is None or release is None
            or len(keep) + len(release) > staging.HOLD_MAX_IDS or set(keep) & set(release)):
        return _bad_hold()

    owner = owner_tag()
    st = store()
    for stage_id in release:
        st.release(stage_id, owner, holder)
    live, gone = {}, []
    for stage_id in keep:
        meta = st.keep(stage_id, owner, holder)
        if meta is None:
            gone.append(stage_id)
        else:
            live[stage_id] = meta['pages']
    return jsonify({'live': live, 'gone': gone})


# ---------------------------------------------------------------------------
# GET /packet/stage/<id>/thumb/<n> and /page/<n>
# ---------------------------------------------------------------------------
def _not_found():
    return refusal(404, 'not_found', 'Not found.')


def _clamp_width(raw, allowed, default):
    try:
        width = int(raw)
    except (TypeError, ValueError):
        return default
    for value in allowed:
        if width <= value:
            return value
    return allowed[-1]


def render_budget():
    """This worker's render budget (``PACKET_RENDERS_PER_MINUTE`` per subject),
    counted per worker like the POST limiter."""
    per_minute = int(current_app.config.get('PACKET_RENDERS_PER_MINUTE', PACKET_RENDERS_PER_MINUTE))
    budget = current_app.extensions.get('packet_render_budget')
    if budget is None or budget.max_requests != per_minute:
        budget = RateLimiter(per_minute, 60)
        current_app.extensions['packet_render_budget'] = budget
    return budget


def _image(data, etag):
    response = make_response(data)
    response.headers['Content-Type'] = 'image/png'
    response.headers['Cache-Control'] = CACHE_CONTROL
    response.set_etag(etag)
    return response.make_conditional(request)


def _render(stage_id, n, width):
    if not staging.valid_id(stage_id) or not _INDEX_RE.fullmatch(n):
        return _not_found()
    index = int(n)
    owner = owner_tag()
    st = store()
    with st.using(stage_id) as held:
        meta = st.live_meta(stage_id, owner) if held else None
        if meta is None or index >= int(meta['pages']):
            return _not_found()
        etag = hashlib.sha256(f'{stage_id}/{index}/{width}'.encode()).hexdigest()[:24]
        if etag in request.if_none_match:
            return _image(b'', etag)
        cached = width in staging.THUMB_WIDTHS
        if cached:
            try:
                with open(st.thumb_path(stage_id, index, width), 'rb') as fh:
                    return _image(fh.read(), etag)   # a file read, not a job: no budget
            except FileNotFoundError:
                pass

        subj = subject()
        with st.lane(subj, 'render') as taken:
            if not taken:
                return busy(BUSY_RENDER_SECONDS, 'Too many previews at once.')
            budget = render_budget()
            if not budget.allow(subj):
                return busy(budget.retry_after(subj), 'Too many previews at once.')
            with st.temp_dir() as tmpdir:
                out = os.path.join(tmpdir, 'render.png')
                pdf = st.pdf_path(stage_id)
                try:
                    run_in_job(lambda: staging.render_work(pdf, index, width, out), tmpdir)
                except LimitError as exc:
                    return refusal(400, 'limit', str(exc))
                except Exception:
                    log.exception('packet render failed')
                    return refusal(500, 'server_error', "Docist couldn't render this page.")
                with open(out, 'rb') as fh:
                    data = fh.read()
                if cached:
                    st.cache_thumb(stage_id, index, width, out)
        return _image(data, etag)


@bp.route('/packet/stage/<stage_id>/thumb/<n>')
def thumb(stage_id, n):
    return _render(stage_id, n, _clamp_width(request.args.get('w'), staging.THUMB_WIDTHS, 160))


@bp.route('/packet/stage/<stage_id>/page/<n>')
def page(stage_id, n):
    return _render(stage_id, n, _clamp_width(request.args.get('w'), (160, 320, 1200), 1200))
