"""Route tests for the Convert Files feature (routes/convert.py).

The four transform-plugin modules are authored concurrently, so the real
conversion matrix may be empty or partial while these tests run. To keep the
route tests deterministic and independent of which pairs exist, we monkeypatch
``transforms._DIRECT`` with fake entries -- every registry helper
(supported_sources / targets_for / matrix / get_transform) reads that module
global at call time, so swapping it reroutes the whole registry.

One soft integration test runs a genuine conversion, but only when the real
matrix is non-empty; otherwise it skips.

Reuses fixtures from tests/backend/conftest.py (``client``, ``builders``).
"""
import io
import os
import zipfile

import pytest

import transforms


# --------------------------------------------------------------------------
# Fake transform functions: func(input_path, output_path) -> actual path|None
# --------------------------------------------------------------------------
def _fake_write_requested(input_path, output_path):
    """Write exactly the requested output; return None (actual == requested)."""
    with open(output_path, 'wb') as fh:
        fh.write(b'converted-bytes')
    return None  # signals "wrote output_path as given"


def _fake_write_zip(input_path, output_path):
    """Multi-page style conversion: writes a .zip and returns its real path."""
    zip_path = os.path.splitext(output_path)[0] + '.zip'
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('page_001.png', b'\x89PNG-fake')
        zf.writestr('page_002.png', b'\x89PNG-fake')
    return zip_path  # actual path differs from the requested .png


def _fake_raises(input_path, output_path):
    raise transforms.TransformError('conversion blew up on purpose')


@pytest.fixture
def fake_registry(monkeypatch):
    """Install a small deterministic registry and return its dict."""
    fake = {
        ('.csv', '.xlsx'): _fake_write_requested,
        ('.csv', '.json'): _fake_write_requested,
        ('.pdf', '.png'): _fake_write_zip,
        ('.boom', '.txt'): _fake_raises,
    }
    monkeypatch.setattr(transforms, '_DIRECT', fake)
    return fake


# --------------------------------------------------------------------------
# GET /convert (page)
# --------------------------------------------------------------------------
def test_get_convert_renders(client):
    resp = client.get('/convert')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '/static/convert.js' in body
    assert 'class="active"' in body
    assert 'Convert Files' in body


# --------------------------------------------------------------------------
# GET /convert/matrix
# --------------------------------------------------------------------------
def test_matrix_endpoint(client, fake_registry):
    resp = client.get('/convert/matrix')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'matrix' in data
    m = data['matrix']
    assert m['.csv'] == ['.json', '.xlsx']  # sorted targets
    assert m['.pdf'] == ['.png']
    assert '.boom' in m


# --------------------------------------------------------------------------
# GET /convert/targets
# --------------------------------------------------------------------------
def test_targets_endpoint(client, fake_registry):
    resp = client.get('/convert/targets?ext=.csv')
    assert resp.status_code == 200
    assert resp.get_json()['targets'] == ['.json', '.xlsx']


def test_targets_endpoint_normalizes_ext(client, fake_registry):
    # No leading dot, mixed case -> still resolves.
    resp = client.get('/convert/targets?ext=CSV')
    assert resp.status_code == 200
    assert resp.get_json()['targets'] == ['.json', '.xlsx']


def test_targets_missing_param_is_400(client, fake_registry):
    resp = client.get('/convert/targets')
    assert resp.status_code == 400
    assert 'error' in resp.get_json()


def test_targets_malformed_param_is_400(client, fake_registry):
    resp = client.get('/convert/targets?ext=.')
    assert resp.status_code == 400
    resp2 = client.get('/convert/targets?ext=not..valid')
    assert resp2.status_code == 400


def test_targets_unknown_source_is_empty(client, fake_registry):
    resp = client.get('/convert/targets?ext=.nope')
    assert resp.status_code == 200
    assert resp.get_json()['targets'] == []


# --------------------------------------------------------------------------
# POST /convert/run
# --------------------------------------------------------------------------
def test_run_happy_path(client, fake_registry):
    resp = client.post('/convert/run', data={
        'target': '.xlsx',
        'file': (io.BytesIO(b'a,b,c\n1,2,3\n'), 'data.csv'),
    }, content_type='multipart/form-data')
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload['success'] is True
    assert payload['filename'] == 'data.xlsx'
    assert payload['download_url'] == '/download?filename=data.xlsx'
    out = client.output_dir / 'data.xlsx'
    assert out.exists()
    assert out.read_bytes() == b'converted-bytes'


def test_run_actual_path_differs_returns_zip(client, fake_registry):
    # pdf -> png produces a .zip; the route must honour the actual filename.
    resp = client.post('/convert/run', data={
        'target': '.png',
        'file': (io.BytesIO(b'%PDF-1.4 fake'), 'doc.pdf'),
    }, content_type='multipart/form-data')
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload['success'] is True
    assert payload['filename'] == 'doc.zip'  # NOT doc.png
    assert payload['download_url'] == '/download?filename=doc.zip'
    # Message should flag that the requested target wasn't produced directly.
    assert '.zip' in payload['message']
    assert '.png' in payload['message']
    out = client.output_dir / 'doc.zip'
    assert out.exists()
    with zipfile.ZipFile(str(out)) as zf:
        assert sorted(zf.namelist()) == ['page_001.png', 'page_002.png']
    # The requested .png must NOT have leaked into OUTPUT_FOLDER.
    assert not (client.output_dir / 'doc.png').exists()


def test_run_collision_safe_naming(client, fake_registry):
    def post():
        return client.post('/convert/run', data={
            'target': '.xlsx',
            'file': (io.BytesIO(b'a,b\n1,2\n'), 'data.csv'),
        }, content_type='multipart/form-data')

    first = post()
    second = post()
    third = post()
    assert first.get_json()['filename'] == 'data.xlsx'
    assert second.get_json()['filename'] == 'data_1.xlsx'
    assert third.get_json()['filename'] == 'data_2.xlsx'
    for name in ('data.xlsx', 'data_1.xlsx', 'data_2.xlsx'):
        assert (client.output_dir / name).exists()


def test_run_unsupported_source_is_400(client, fake_registry):
    resp = client.post('/convert/run', data={
        'target': '.xlsx',
        'file': (io.BytesIO(b'hello'), 'notes.txt'),
    }, content_type='multipart/form-data')
    assert resp.status_code == 400
    err = resp.get_json()['error']
    assert '.csv' in err  # tells the user what IS supported


def test_run_unsupported_target_is_400(client, fake_registry):
    resp = client.post('/convert/run', data={
        'target': '.png',  # csv can't reach png in the fake registry
        'file': (io.BytesIO(b'a,b\n1,2\n'), 'data.csv'),
    }, content_type='multipart/form-data')
    assert resp.status_code == 400
    err = resp.get_json()['error']
    assert '.xlsx' in err  # lists the available targets


def test_run_missing_target_is_400(client, fake_registry):
    resp = client.post('/convert/run', data={
        'file': (io.BytesIO(b'a,b\n1,2\n'), 'data.csv'),
    }, content_type='multipart/form-data')
    assert resp.status_code == 400


def test_run_missing_file_is_400(client, fake_registry):
    resp = client.post('/convert/run', data={'target': '.xlsx'},
                       content_type='multipart/form-data')
    assert resp.status_code == 400
    assert 'error' in resp.get_json()


def test_run_empty_filename_is_400(client, fake_registry):
    resp = client.post('/convert/run', data={
        'target': '.xlsx',
        'file': (io.BytesIO(b'x'), ''),
    }, content_type='multipart/form-data')
    assert resp.status_code == 400


def test_run_transform_error_is_400(client, fake_registry):
    resp = client.post('/convert/run', data={
        'target': '.txt',
        'file': (io.BytesIO(b'kaboom'), 'thing.boom'),
    }, content_type='multipart/form-data')
    assert resp.status_code == 400
    assert 'purpose' in resp.get_json()['error']


# --------------------------------------------------------------------------
# Soft integration: run a REAL pair, only if the live matrix has one.
# --------------------------------------------------------------------------
# Map real source extensions to conftest builders that produce valid inputs.
_BUILDERS_BY_EXT = {
    '.png': 'png',
    '.jpg': 'jpg',
    '.jpeg': 'jpg',
    '.tiff': 'tiff',
    '.tif': 'tiff',
    '.md': 'markdown',
    '.markdown': 'markdown',
    '.txt': 'text_rich',
    '.html': 'html',
    '.htm': 'html',
    '.docx': 'docx',
    '.pdf': 'pdf',
}


def test_soft_real_pair_integration(client, builders, tmp_path):
    live = transforms.matrix()
    if not live:
        pytest.skip('No transform plugins registered yet; nothing to integrate.')

    # Find the first (src, target) where we can synthesise a valid source file.
    chosen = None
    for src in sorted(live):
        if src in _BUILDERS_BY_EXT and live[src]:
            chosen = (src, live[src][0])
            break
    if chosen is None:
        pytest.skip('No live source pair has a matching test-file builder.')

    src_ext, target_ext = chosen
    builder = getattr(builders, _BUILDERS_BY_EXT[src_ext])
    src_path = builder(tmp_path / ('input' + src_ext))

    resp = client.post('/convert/run', data={
        'target': target_ext,
        'file': (io.BytesIO(src_path.read_bytes()), 'input' + src_ext),
    }, content_type='multipart/form-data')

    # A real conversion should succeed (200); if the plugin rejects our synthetic
    # sample it returns a clean 400 -- either is acceptable, a 500 is not.
    assert resp.status_code in (200, 400), resp.get_data(as_text=True)
    payload = resp.get_json()
    if resp.status_code == 200:
        assert payload['success'] is True
        assert (client.output_dir / payload['filename']).exists()
    else:
        assert 'error' in payload
