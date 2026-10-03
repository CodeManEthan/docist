"""Dev-only stand-in for [B1]'s packet endpoints, for hand-testing /packet.

Not shipped: it lives under tests/ and registers on the real app only when
run as a script. It answers in the shapes of tests/fixtures/packet-api.json:

    POST /packet/stage   converts with the real converters, counts pages
    POST /packet/hold    keep/release by holder; ids it doesn't know are gone
    GET  /packet/stage/<id>/thumb/<n>, /page/<n>   pypdfium2 renders
    POST /packet/build   a plain pypdf assembly (no numbers or bookmarks)

State is in memory and per process, with none of B1's jobs, lanes, caps or
janitor. File names drive the refusals, so every card state is reachable:
a name containing "fail" is refused, "quota" gets staging_quota, "full" gets
staging_full, "busy" gets one 429 busy first, "slow" takes three seconds.

    .venv/bin/python tests/manual/packet_fake_server.py     # port 5011
"""
import hashlib
import io
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import json  # noqa: E402

from flask import Blueprint, current_app, jsonify, request, send_file  # noqa: E402
from pypdf import PdfReader, PdfWriter  # noqa: E402
from werkzeug.utils import secure_filename  # noqa: E402

API = json.load(open(os.path.join(PROJECT_ROOT, 'tests', 'fixtures', 'packet-api.json')))
STORE = {}            # stage_id -> {path, key, pages, bytes, name, type, owner, holders}
LOCK = threading.Lock()
TMP = tempfile.mkdtemp(prefix='packet-fake-')
SEEN_BUSY = set()

bp = Blueprint('packet_fake', __name__)


def _body(endpoint, name):
    return dict(API['endpoints'][endpoint]['responses'][name]['body'])


def _owner():
    from utils.identity import owner_tag
    return owner_tag()


def _entry(stage_id):
    entry = STORE.get(stage_id)
    if entry is None or entry['owner'] != _owner():
        return None
    return entry


@bp.route('/packet/stage', methods=['POST'])
def stage():
    upload = request.files.get('file')
    paper = request.form.get('paper', 'letter')
    holder = request.form.get('holder', '')
    if not upload or not upload.filename:
        return jsonify({'error': 'No file provided.', 'code': 'rejected'}), 400
    name = upload.filename
    lowered = name.lower()
    if 'slow' in lowered:
        time.sleep(3)
    if 'busy' in lowered and name not in SEEN_BUSY:
        SEEN_BUSY.add(name)
        return jsonify(_body('stage', 'busy')), 429, {'Retry-After': '1'}
    if 'fail' in lowered:
        return jsonify({'error': 'The fake server refuses files named "fail".', 'code': 'rejected'}), 400
    if 'quota' in lowered:
        return jsonify(_body('stage', 'staging_quota')), 400
    if 'full' in lowered:
        return jsonify(_body('stage', 'staging_full')), 400

    data = upload.read()
    digest = hashlib.sha256(data).hexdigest()
    ext = os.path.splitext(secure_filename(name))[1].lower()
    kind = ext.lstrip('.')
    key = f'{digest}::' if ext == '.pdf' else f'{digest}:reflow:{paper}'
    owner = _owner()
    with LOCK:
        for stage_id, entry in STORE.items():
            if entry['key'] == key and entry['owner'] == owner:
                entry['holders'][holder] = time.time()
                return jsonify({'stage_id': stage_id, 'pages': entry['pages'], 'name': name,
                                'type': kind, 'key': key, 'bytes': entry['bytes'],
                                'sizes': entry['sizes']})

    stage_id = secrets.token_urlsafe(16)
    folder = os.path.join(TMP, stage_id)
    os.makedirs(folder)
    pdf_path = os.path.join(folder, 'staged.pdf')
    try:
        if ext == '.pdf':
            with open(pdf_path, 'wb') as fh:
                fh.write(data)
        else:
            from converters import get_converter
            from utils.render_opts import from_form
            convert = get_converter(ext)
            if convert is None:
                raise ValueError(f'{ext or "This"} files are not supported.')
            src = os.path.join(folder, 'upload' + ext)
            with open(src, 'wb') as fh:
                fh.write(data)
            convert(src, pdf_path, from_form(request.form))
        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            shutil.rmtree(folder, ignore_errors=True)
            return jsonify(_body('stage', 'encrypted')), 400
        pages = len(reader.pages)
        from utils.staging import page_sizes
        sizes = page_sizes(reader)   # B7: each page's size for its number
    except Exception as exc:  # the fake reports any failure as the card's reason
        shutil.rmtree(folder, ignore_errors=True)
        return jsonify({'error': str(exc) or 'Could not read this file.', 'code': 'rejected'}), 400

    size = os.path.getsize(pdf_path)
    with LOCK:
        STORE[stage_id] = {'path': pdf_path, 'key': key, 'pages': pages, 'bytes': size,
                           'name': name, 'type': kind, 'owner': owner, 'holders': {holder: time.time()},
                           'sizes': sizes}
    return jsonify({'stage_id': stage_id, 'pages': pages, 'name': name, 'type': kind,
                    'key': key, 'bytes': size, 'sizes': sizes})


@bp.route('/packet/hold', methods=['POST'])
def hold():
    body = request.get_json(silent=True) or {}
    holder = body.get('holder', '')
    answer = {'live': {}, 'gone': []}
    with LOCK:
        for stage_id in body.get('release', []):
            entry = _entry(stage_id)
            if entry:
                entry['holders'].pop(holder, None)
        for stage_id in body.get('keep', []):
            entry = _entry(stage_id)
            if entry is None:
                answer['gone'].append(stage_id)
                continue
            entry['holders'][holder] = time.time()
            answer['live'][stage_id] = entry['pages']
    return jsonify(answer)


def _render(stage_id, n, width):
    entry = _entry(stage_id)
    if entry is None or not 0 <= n < entry['pages']:
        return jsonify({'error': 'Not found.', 'code': 'not_found'}), 404
    import pypdfium2 as pdfium
    from pdf_ops.pdfium_lock import PDFIUM_LOCK
    width = 1200 if width > 320 else (320 if width > 160 else 160)
    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(entry['path'])
        try:
            page = doc[n]
            image = page.render(scale=width / max(1.0, page.get_width())).to_pil().convert('RGB')
        finally:
            doc.close()
    buf = io.BytesIO()
    image.save(buf, 'PNG')
    buf.seek(0)
    response = send_file(buf, mimetype='image/png')
    response.headers['Cache-Control'] = 'private, max-age=3600'
    return response


@bp.route('/packet/stage/<stage_id>/thumb/<int:n>')
def thumb(stage_id, n):
    return _render(stage_id, n, request.args.get('w', 160, type=int))


@bp.route('/packet/stage/<stage_id>/page/<int:n>')
def page(stage_id, n):
    return _render(stage_id, n, request.args.get('w', 1200, type=int))


@bp.route('/packet/build', methods=['POST'])
def build():
    from utils.naming import result_name
    body = request.get_json(silent=True) or {}
    packet = body.get('packet') or {}
    stages = body.get('stages') or {}
    bad = []
    writer = PdfWriter()
    readers = {}
    for section in packet.get('sections', []):
        for item in section.get('items', []):
            if item.get('kind') == 'blank':
                writer.add_blank_page(612, 792)
                continue
            entry = _entry(stages.get(item.get('hash'), ''))
            if entry is None:
                bad.append({'id': item.get('id'), 'code': 'expired'})
                continue
            if item.get('pages') is not None and (item.get('key') != entry['key']
                                                  or item.get('src_pages') != entry['pages']):
                bad.append({'id': item.get('id'), 'code': 'stale'})
                continue
            reader = readers.setdefault(entry['path'], PdfReader(entry['path']))
            refs = item.get('pages') or [{'src': i, 'rot': 0} for i in range(entry['pages'])]
            for ref in refs:
                added = writer.add_page(reader.pages[ref['src']])
                if ref.get('rot'):
                    added.rotate(ref['rot'])
    if bad:
        refusal = _body('build', 'bad_items')
        refusal.update(code=bad[0]['code'], items=bad)
        return jsonify(refusal), 400
    if not writer.pages:
        return jsonify(_body('build', 'empty')), 400
    filename = result_name('packet.pdf')
    out = os.path.join(current_app.config['OUTPUT_FOLDER'], filename)
    with open(out, 'wb') as fh:
        writer.write(fh)
    response = jsonify({'filename': filename, 'pages': len(writer.pages)})
    response.headers['X-Docist-Usage-Remaining'] = 'unlimited'
    return response


def main():
    import app as app_module
    app = app_module.app
    app.register_blueprint(bp)
    port = int(os.environ.get('PACKET_FAKE_PORT', '5011'))
    print(f'Packet fake server on http://127.0.0.1:{port}/packet (staged files in {TMP})')
    app.run(host='127.0.0.1', port=port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
