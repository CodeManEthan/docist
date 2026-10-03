"""The packet build endpoint: packet JSON in, one PDF result out (design §4.3).

Blueprint ``packet_build`` holds only this endpoint, so metering can name it
alone: ``METERED_BLUEPRINTS`` includes ``packet_build``, and one successful
build costs one operation (§4.4). Metering's ``check()`` refuses at the daily
limit before any of this runs.

The build renews the caller's holder on every staged entry the packet names
and holds each entry's use lock shared until the job is done, so the janitor
can't remove one mid-build. It checks the packet's shape, each file item's
stage (owner, liveness, the key's hash) and the fit of explicit page refs,
then the per-build byte cap (§4.6), then runs ``packet_pipeline`` in one job
under the work lane. The result is written in the request's temp directory,
outside the job directory, and moved to ``OUTPUT_FOLDER`` after the job
succeeds (launch-hardening §10.3); ``/download`` serves it.

Every refusal is ``{error, code, items}`` (``tests/fixtures/packet-api.json``,
``build_refusal``): ``code`` is the first item's when ``items`` isn't empty.
"""
import contextlib
import logging
import os
import shutil
import tempfile

from flask import Blueprint, current_app, jsonify
from werkzeug.utils import secure_filename

from pdf_ops.limits import LimitError
from pdf_ops.packet import PacketError, _check_shape, packet_pipeline
from routes.packet_stage import (MALFORMED, BUSY_STAGE_SECONDS, busy, mb, packet_limit_bytes,
                                 read_json_body, refusal, store, subject)
from utils import staging
from utils.identity import current_user, owner_tag
from utils.naming import result_name
from utils.render_opts import run_in_job

log = logging.getLogger(__name__)

bp = Blueprint('packet_build', __name__)


def _refused(code, error, items=(), **extra):
    return refusal(400, code, error, items=list(items), **extra)


def _from_packet_error(exc):
    return _refused(exc.code, exc.message, exc.items)


def _malformed():
    return _refused('invalid', 'This packet is malformed.')


def _friendly_name(packet):
    """``<cover title or packet title>.pdf``, else ``packet.pdf`` (§2.1)."""
    for title in ((packet.get('cover') or {}).get('title'), packet.get('title')):
        if isinstance(title, str):
            safe = secure_filename(title)[:100]
            if safe:
                return f'{safe}.pdf'
    return 'packet.pdf'


def _file_items(packet):
    return [item for section in packet['sections'] for item in section['items']
            if item.get('kind') == 'file']


def _fit_code(item, meta):
    """None when an item's explicit refs fit its stage (§3.1 Fit), else the code."""
    refs = item.get('pages')
    if refs is None:
        return None
    count = int(meta['pages'])
    if item.get('key') != meta['key'] or item.get('src_pages') != count:
        return 'stale'
    if any(not 0 <= ref['src'] < count for ref in refs):
        return 'out_of_range'
    return None


def build_work(packet, staged_paths, out_path):
    """The build's work, run in its job. A ``PacketError`` comes back as a
    value, not raised: its constructor takes more than the ``args`` it
    pickles with, so the job's parent couldn't rebuild it."""
    work_dir = tempfile.mkdtemp(prefix='packet-')   # inside the job directory
    try:
        planned = packet_pipeline(packet, staged_paths, None, work_dir, out_path)
    except PacketError as exc:
        return {'refusal': {'code': exc.code, 'error': exc.message, 'items': exc.items}}
    return {'pages': len(planned['pages'])}


@bp.route('/packet/build', methods=['POST'])
def build():
    body, response = read_json_body()
    if response is not None:
        return response
    if body is MALFORMED or not isinstance(body, dict):
        return _malformed()
    packet, stages, holder = body.get('packet'), body.get('stages'), body.get('holder')
    if (not isinstance(packet, dict) or not isinstance(stages, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in stages.items())
            or not staging.valid_holder(holder)):
        return _malformed()
    try:
        _check_shape(packet)
    except PacketError as exc:
        return _from_packet_error(exc)

    user = current_user()
    owner = owner_tag()
    st = store()
    items = _file_items(packet)
    hashes = list(dict.fromkeys(item['hash'] for item in items))

    with contextlib.ExitStack() as held:
        # Renew this holder on every entry the packet names, and keep each
        # one's use lock shared until the job is done.
        metas = {}
        for file_hash in hashes:
            stage_id = stages.get(file_hash)
            fd = st.use(stage_id)
            if fd is None:
                continue
            held.callback(os.close, fd)
            meta = st.keep(stage_id, owner, holder)
            if meta is not None and staging.key_hash(meta['key']) == file_hash:
                metas[file_hash] = meta

        bad = []
        for item in items:
            meta = metas.get(item['hash'])
            code = 'expired' if meta is None else _fit_code(item, meta)
            if code is not None:
                bad.append({'id': item['id'], 'code': code})
        if bad:
            return _refused(bad[0]['code'],
                            "Some files can't be used. Re-add them and build again.", bad)

        limit = packet_limit_bytes(user)
        staged_paths = {h: st.pdf_path(m['id']) for h, m in metas.items()}
        total = sum(os.path.getsize(p) for p in set(staged_paths.values()))
        if total > limit:
            return _refused('too_large', f'This packet uses {mb(total)} MB of files. '
                                         f'A packet can use at most {mb(limit)} MB.',
                            limit_bytes=limit)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, 'packet.pdf')
            with st.lane(subject(), 'work') as taken:
                if not taken:
                    return busy(BUSY_STAGE_SECONDS)
                try:
                    done = run_in_job(lambda: build_work(packet, staged_paths, out_path), tmpdir)
                except LimitError as exc:
                    return _refused('limit', str(exc))
                except Exception:
                    log.exception('packet build failed')
                    return refusal(500, 'server_error',
                                   "Docist couldn't build this packet. Try again.", items=[])
            if 'refusal' in done:
                r = done['refusal']
                return _refused(r['code'], r['error'], r['items'])
            filename = result_name(_friendly_name(packet))
            shutil.move(out_path, os.path.join(current_app.config['OUTPUT_FOLDER'], filename))
    return jsonify({'filename': filename, 'pages': done['pages']})
