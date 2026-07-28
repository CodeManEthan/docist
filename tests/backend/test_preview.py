"""Tests for page-thumbnail previews: pure rendering + the /preview/thumbs route.

Reuses fixtures from tests/backend/conftest.py (``client``, ``builders``).
"""
import base64
import io

import pytest

from pdf_ops.preview import MAX_THUMBS, render_thumbnails


DATA_URL_PREFIX = "data:image/png;base64,"


def _post(client, name, data):
    return client.post(
        '/preview/thumbs',
        data={'file': (io.BytesIO(data), name)},
        content_type='multipart/form-data',
    )


def _post_path(client, path, name=None):
    return _post(client, name or path.name, path.read_bytes())


# --------------------------------------------------------------------------
# pdf_ops.preview.render_thumbnails
# --------------------------------------------------------------------------
def test_render_thumbnails_returns_one_data_url_per_page(tmp_path, builders):
    src = builders.pdf(tmp_path / "doc.pdf", pages=3)
    result = render_thumbnails(str(src))

    assert result['pages'] == 3
    assert result['rendered'] == 3
    assert len(result['thumbs']) == 3
    for thumb in result['thumbs']:
        assert thumb.startswith(DATA_URL_PREFIX)


def test_render_thumbnails_decodes_to_a_small_png(tmp_path, builders):
    from PIL import Image

    src = builders.pdf(tmp_path / "doc.pdf", pages=1)
    thumb = render_thumbnails(str(src))['thumbs'][0]
    raw = base64.b64decode(thumb[len(DATA_URL_PREFIX):])

    assert raw.startswith(b'\x89PNG\r\n\x1a\n')
    image = Image.open(io.BytesIO(raw))
    assert image.format == 'PNG'
    # Rendered to the requested target width (portrait Letter -> taller).
    assert image.width == 120
    assert image.height > image.width


def test_render_thumbnails_honours_max_pages(tmp_path, builders):
    src = builders.pdf(tmp_path / "doc.pdf", pages=5)
    result = render_thumbnails(str(src), max_pages=2)

    assert result['pages'] == 5
    assert result['rendered'] == 2
    assert len(result['thumbs']) == 2


def test_render_thumbnails_custom_width(tmp_path, builders):
    from PIL import Image

    src = builders.pdf(tmp_path / "doc.pdf", pages=1)
    thumb = render_thumbnails(str(src), width=60)['thumbs'][0]
    raw = base64.b64decode(thumb[len(DATA_URL_PREFIX):])

    assert Image.open(io.BytesIO(raw)).width == 60


def test_render_thumbnails_raises_on_garbage(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    with pytest.raises(Exception):
        render_thumbnails(str(bad))


# --------------------------------------------------------------------------
# POST /preview/thumbs — happy path
# --------------------------------------------------------------------------
def test_thumbs_happy_path(client, tmp_path, builders):
    src = builders.pdf(tmp_path / "report.pdf", pages=3)
    resp = _post_path(client, src)

    assert resp.status_code == 200
    data = resp.get_json()
    assert data['pages'] == 3
    assert data['rendered'] == 3
    assert len(data['thumbs']) == 3
    for thumb in data['thumbs']:
        assert thumb.startswith(DATA_URL_PREFIX)
        # The payload after the prefix must be real base64 PNG bytes.
        raw = base64.b64decode(thumb[len(DATA_URL_PREFIX):])
        assert raw.startswith(b'\x89PNG\r\n\x1a\n')


def test_thumbs_single_page(client, tmp_path, builders):
    src = builders.pdf(tmp_path / "one.pdf", pages=1)
    data = _post_path(client, src).get_json()

    assert data == {
        'pages': 1,
        'rendered': 1,
        'thumbs': data['thumbs'],
    }
    assert len(data['thumbs']) == 1


def test_thumbs_writes_nothing_to_output_folder(client, tmp_path, builders):
    src = builders.pdf(tmp_path / "doc.pdf", pages=2)
    _post_path(client, src)

    assert list(client.output_dir.iterdir()) == []


# --------------------------------------------------------------------------
# POST /preview/thumbs — the 24-page cap
# --------------------------------------------------------------------------
def test_thumbs_caps_at_24_pages(client, tmp_path, builders):
    src = builders.pdf(tmp_path / "long.pdf", pages=30)
    resp = _post_path(client, src)

    assert resp.status_code == 200
    data = resp.get_json()
    assert data['pages'] == 30
    assert data['rendered'] == MAX_THUMBS == 24
    assert len(data['thumbs']) == 24


def test_thumbs_exactly_at_the_cap_is_not_truncated(client, tmp_path, builders):
    src = builders.pdf(tmp_path / "exact.pdf", pages=24)
    data = _post_path(client, src).get_json()

    assert data['pages'] == 24
    assert data['rendered'] == 24


# --------------------------------------------------------------------------
# POST /preview/thumbs — rejections
# --------------------------------------------------------------------------
def test_thumbs_rejects_non_pdf_extension(client):
    resp = _post(client, 'notes.txt', b'just some text')

    assert resp.status_code == 400
    assert 'pdf' in resp.get_json()['error'].lower()


def test_thumbs_rejects_png_extension(client, tmp_path, builders):
    src = builders.png(tmp_path / "pic.png")
    resp = _post_path(client, src)

    assert resp.status_code == 400
    assert 'error' in resp.get_json()


def test_thumbs_rejects_fake_pdf_content(client):
    """A .pdf name over non-PDF bytes is caught by validate_upload."""
    resp = _post(client, 'fake.pdf', b'MZ\x90\x00 definitely an executable')

    assert resp.status_code == 400
    error = resp.get_json()['error']
    assert 'does not look like' in error


def test_thumbs_rejects_renamed_png(client, tmp_path, builders):
    src = builders.png(tmp_path / "pic.png")
    resp = _post(client, 'pic.pdf', src.read_bytes())

    assert resp.status_code == 400
    assert 'does not look like' in resp.get_json()['error']


def test_thumbs_rejects_pdf_header_with_garbage_body(client):
    """Passes the magic-byte check, then fails to parse -> 400, not 500."""
    resp = _post(client, 'broken.pdf', b'%PDF-1.7\nnot really a document')

    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'Could not read the PDF file.'


def test_thumbs_missing_file_field(client):
    resp = client.post('/preview/thumbs', data={}, content_type='multipart/form-data')

    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'No file provided.'


def test_thumbs_empty_filename(client):
    resp = _post(client, '', b'%PDF-1.4')

    assert resp.status_code == 400
    assert 'error' in resp.get_json()


def test_thumbs_rejects_get(client):
    assert client.get('/preview/thumbs').status_code == 405


def test_concurrent_rendering_is_safe(tmp_path, builders):
    """PDFium is not thread-safe; pdf_ops serializes it via PDFIUM_LOCK.

    Without the lock this test can crash the whole process (malloc
    corruption), which is exactly the regression it guards against: the merge
    UI fires one /preview/thumbs request per selected PDF, concurrently,
    under the threaded dev server.
    """
    from concurrent.futures import ThreadPoolExecutor

    from pdf_ops.preview import render_thumbnails

    pdfs = [
        str(builders.pdf(tmp_path / f"c{i}.pdf", pages=4, marker=f"Conc{i}"))
        for i in range(4)
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(render_thumbnails, pdfs * 3))

    assert len(results) == 12
    assert all(r["pages"] == 4 and r["rendered"] == 4 for r in results)
