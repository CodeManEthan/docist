"""B2 adds no new route to the resource holes the 2026-10-02 critic review
found on main (notes: jpluto/projects/docist/reports/2026-10-02-critic-docist-main.md).

  * Blocker 3 (archive expansion): every Word file bound for the Word engine
    passes an archive-expansion check before LibreOffice opens it, and one
    that fails is refused, never handed to the reflow.
  * Blockers 2, 4, 6, 7 and the paid limit: the paid limit applies only to
    Merge and Convert, and there only to PDFs and Word files the engine
    renders.
  * Blocker 7 (results in memory): /api/v1/merge and /api/v1/convert stream.
"""
import io
import os
import zipfile

import pytest
from pypdf import PdfReader

import app as flask_app_module
from converters import ConversionError, docx_converter, get_converter
from converters.options import RenderOptions
from models import ApiKey, User, db
from pdf_ops import office
from routes import api as api_routes

import word_fixtures as wf

app = flask_app_module.app
MIB = 1024 * 1024


@pytest.fixture
def no_reflow(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError('the reflow ran')
    monkeypatch.setattr(docx_converter, '_reflow', boom)


@pytest.fixture
def no_libreoffice(monkeypatch):
    calls = []

    def fake(*a, **k):
        calls.append(a)
        raise AssertionError('LibreOffice was started')
    monkeypatch.setattr(office.subprocess, 'Popen', fake)
    return calls


def bomb_docx(path, size=30 * MIB):
    """A valid .docx whose document.xml deflates about 1000:1 (the critic's
    30 KB file holding 30 MB of text)."""
    body = '<w:p><w:r><w:t>' + 'a' * size + '</w:t></w:r></w:p>'
    return wf.write_docx(path, {'word/document.xml': wf.document(body, wf.sect(wf.LETTER_TWIPS))})


# --------------------------------------------------------------------------
# Blocker 3: the archive-expansion check
# --------------------------------------------------------------------------
def test_the_critics_bomb_is_refused_before_anything_opens_it(tmp_path, no_reflow,
                                                              no_libreoffice):
    src = bomb_docx(tmp_path / 'bomb.docx')
    assert os.path.getsize(src) < 100 * 1024
    with pytest.raises(ConversionError, match='compressed too far'):
        get_converter('.docx')(str(src), str(tmp_path / 'out.pdf'),
                               RenderOptions(word_engine='libreoffice'))
    assert no_libreoffice == []


@pytest.mark.parametrize('limit,value,match', [
    ('MAX_ENTRIES', 3, 'too many parts'),
    ('MAX_ENTRY_BYTES', 100, 'unpacks too large'),
    ('MAX_UNPACKED_BYTES', 1000, 'unpacks too large'),
])
def test_each_archive_limit_refuses(tmp_path, monkeypatch, no_reflow, no_libreoffice,
                                    limit, value, match):
    monkeypatch.setattr(office, limit, value)
    src = wf.a4_header_footer(tmp_path / 'hf.docx')   # 5 entries, a few KB
    with pytest.raises(ConversionError, match=match):
        get_converter('.docx')(str(src), str(tmp_path / 'out.pdf'),
                               RenderOptions(word_engine='libreoffice'))


def test_an_archive_that_lies_about_its_sizes_is_caught_while_read(tmp_path, monkeypatch):
    """The strip counts the bytes that actually come out against the limits."""
    src = tmp_path / 'liar.docx'
    wf.write_docx(src, {'word/document.xml': wf.document(wf.para('x'), '')})
    with zipfile.ZipFile(src, 'a') as zf:
        zf.writestr('word/media/big.bin', b'\0' * (2 * MIB))
    # Claim small sizes in the archive's metadata; zipfile reads the claim.
    real_infolist = zipfile.ZipFile.infolist

    def lying_infolist(self):
        infos = real_infolist(self)
        for info in infos:
            if info.filename == 'word/media/big.bin':
                info.file_size = 10
        return infos
    monkeypatch.setattr(zipfile.ZipFile, 'infolist', lying_infolist)
    monkeypatch.setattr(office, 'MAX_ENTRY_BYTES', MIB)
    with pytest.raises((office.ArchiveError, zipfile.BadZipFile)):
        office.strip_external(str(src), str(tmp_path / 'out.docx'))


def test_a_lied_uncompressed_size_is_caught_by_the_ratio_while_read(tmp_path, monkeypatch):
    """A declared size that passes the ratio check, and actual output that
    doesn't: the strip measures the ratio on the bytes that come out."""
    src = tmp_path / 'liar2.docx'
    wf.write_docx(src, {'word/document.xml': wf.document(wf.para('x'), '')})
    with zipfile.ZipFile(src, 'a', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('word/media/zeros.bin', b'\0' * (4 * MIB))
    real_infolist = zipfile.ZipFile.infolist

    def lying_infolist(self):
        infos = real_infolist(self)
        for info in infos:
            if info.filename == 'word/media/zeros.bin':
                info.file_size = 1000      # declared tiny, so the declared ratio passes
        return infos
    monkeypatch.setattr(zipfile.ZipFile, 'infolist', lying_infolist)
    with pytest.raises((office.ArchiveError, zipfile.BadZipFile)):
        office.strip_external(str(src), str(tmp_path / 'out.docx'))


def test_out_of_time_still_checks_the_archive_first(tmp_path, no_reflow, no_libreoffice):
    """With no time left the engine falls back, but only after the check: a
    bomb is refused rather than re-flowed."""
    import time
    src = bomb_docx(tmp_path / 'bomb.docx')
    opts = RenderOptions(word_engine='libreoffice', deadline=time.monotonic() - 1)
    with pytest.raises(ConversionError, match='compressed too far'):
        get_converter('.docx')(str(src), str(tmp_path / 'out.pdf'), opts)


def test_a_normal_word_file_passes_the_check(tmp_path):
    for build in (wf.letter_1page, wf.table_36pages, wf.a4_header_footer, wf.calibri_cambria):
        office.check_package(str(build(tmp_path / f'{build.__name__}.docx')))


def test_a_large_word_file_is_never_re_flowed(tmp_path, monkeypatch, no_reflow):
    """The fallback takes only what a free user could send."""
    def fail(*_a, **_k):
        raise office.OfficeError('boom')
    monkeypatch.setattr(office, 'docx_to_pdf', fail)
    src = wf.letter_1page(tmp_path / 'l.docx')
    opts = RenderOptions(word_engine='libreoffice', reflow_max_bytes=os.path.getsize(src) - 1)
    with pytest.raises(ConversionError, match='too large for the basic converter'):
        get_converter('.docx')(str(src), str(tmp_path / 'out.pdf'), opts)


def test_the_reflow_budget_is_shared_across_the_request(tmp_path, monkeypatch):
    """verification-b2-bounds-recheck MINOR 1: two Word files that each fit the
    free limit but not together; the engine fails on both. Only the first is
    re-flowed, as a free request could carry only one of them."""
    reflowed = []
    monkeypatch.setattr(docx_converter, '_reflow', lambda i, o, opts: reflowed.append(i))

    def fail(*_a, **_k):
        raise office.OfficeError('boom')
    monkeypatch.setattr(office, 'docx_to_pdf', fail)
    one = wf.letter_1page(tmp_path / 'one.docx')
    two = wf.letter_1page(tmp_path / 'two.docx')
    size = os.path.getsize(one)
    opts = RenderOptions(word_engine='libreoffice', reflow_max_bytes=size + size // 2)
    get_converter('.docx')(str(one), str(tmp_path / 'one.pdf'), opts)
    with pytest.raises(ConversionError, match='too large for the basic converter'):
        get_converter('.docx')(str(two), str(tmp_path / 'two.pdf'), opts)
    assert reflowed == [str(one)]


# --------------------------------------------------------------------------
# The paid limit only where B2 bounds the work
# --------------------------------------------------------------------------
class ZeroBody(io.RawIOBase):
    """multipart: one file part of ``size`` bytes starting with ``head``, plus
    optional text fields."""

    def __init__(self, size, field, filename, head=b'', fields=()):
        b = 'b2boundsboundary'
        self.content_type = f'multipart/form-data; boundary={b}'
        pre = b''
        for name, value in fields:
            pre += (f'--{b}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                    ).encode() + value + b'\r\n'
        pre += (f'--{b}\r\nContent-Disposition: form-data; name="{field}"; '
                f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                ).encode() + head
        self.pre = pre
        self.tail = f'\r\n--{b}--\r\n'.encode()
        self.size = size - len(head)
        self.length = len(pre) + self.size + len(self.tail)
        self.pos = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = offset if whence == 0 else self.length + offset
        return self.pos

    def readinto(self, buf):
        n = min(len(buf), self.length - self.pos)
        if n <= 0:
            return 0
        out = bytearray()
        start = self.pos
        for seg_start, seg, seg_len in ((0, self.pre, len(self.pre)),
                                        (len(self.pre), None, self.size),
                                        (len(self.pre) + self.size, self.tail, len(self.tail))):
            lo, hi = max(start, seg_start), min(start + n, seg_start + seg_len)
            if lo < hi:
                out += bytes(hi - lo) if seg is None else seg[lo - seg_start:hi - seg_start]
        buf[:n] = out
        self.pos += n
        return n


def post(client, path, body, headers=None, data_fields=None):
    return client.post(path, input_stream=body, content_length=body.length,
                       content_type=body.content_type, headers=headers or {})


@pytest.fixture
def paid(client, make_user, login):
    user = make_user(email='paid@x.io', plan='monthly')
    login(client, user)
    return user


def paid_key(make_user):
    user = make_user(email='key@x.io', plan='monthly')
    with app.app_context():
        raw, _ = ApiKey.issue(db.session.get(User, user.id), 'k')
        db.session.commit()
    return {'Authorization': f'Bearer {raw}'}


@pytest.mark.parametrize('path,field,filename', [
    ('/pages/run', 'file', 'a.pdf'),
    ('/print/run', 'file', 'a.pdf'),
    ('/export/run', 'file', 'a.pdf'),
    ('/security/run', 'file', 'a.pdf'),
    ('/preview/thumbs', 'file', 'a.pdf'),
])
def test_other_tools_keep_the_free_limit_for_paid_users(client, paid, path, field, filename):
    response = post(client, path, ZeroBody(51 * MIB, field, filename, b'%PDF-'))
    assert response.status_code == 413
    assert response.get_json()['limit_mb'] == 50


@pytest.mark.parametrize('path', ['/api/v1/pages/extract', '/api/v1/pages/split',
                                  '/api/v1/watermark'])
def test_other_api_endpoints_keep_the_free_limit_for_paid_keys(client, make_user, path):
    response = post(client, path, ZeroBody(51 * MIB, 'file', 'a.pdf', b'%PDF-'),
                    headers=paid_key(make_user))
    assert response.status_code == 413
    assert response.get_json()['limit_mb'] == 50


@pytest.mark.parametrize('filename,head', [
    ('big.png', b'\x89PNG\r\n\x1a\n'),     # blocker 2
    ('big.gif', b'GIF89a'),
    ('big.svg', b'<svg'),                  # blocker 6
    ('big.yaml', b'a: 1\n'),               # blocker 1
    ('big.xlsx', b'PK'),                   # blocker 3, no engine for it
    ('big.txt', b'text'),
])
def test_merge_holds_other_files_to_the_free_limit(client, paid, filename, head):
    response = post(client, '/upload', ZeroBody(51 * MIB, 'files[]', filename, head))
    body = response.get_json()
    if filename.endswith('.yaml'):   # merge doesn't take YAML at all: skipped
        assert response.status_code == 400 and 'over the 50 MB' not in body['error']
        return
    assert response.status_code == 400
    assert "can't be more than 50 MB in one request" in body['error']


class MultiZeroBody(ZeroBody):
    """Several file parts of ``size`` bytes each."""

    def __init__(self, count, size, field, filename, head=b''):
        b = 'b2boundsboundary'
        self.content_type = f'multipart/form-data; boundary={b}'
        part = (f'--{b}\r\nContent-Disposition: form-data; name="{field}"; '
                f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                ).encode() + head
        # Build as one in-memory body: count * size stays small in these tests.
        body = b''.join(part + bytes(size - len(head)) + b'\r\n' for _ in range(count))
        self.pre = body[:-2]
        self.tail = f'\r\n--{b}--\r\n'.encode()
        self.size = 0
        self.length = len(self.pre) + len(self.tail)
        self.pos = 0


def test_files_that_cant_be_large_share_the_free_limit_per_request(client, paid,
                                                                    monkeypatch):
    """verification-b2-bounds MAJOR 1: two 44 MiB SVGs in one paid Merge carried
    twice what a free request can. Such files now share the free limit."""
    from utils import uploads
    monkeypatch.setitem(app.config, 'MAX_CONTENT_LENGTH', 3 * MIB)   # small, for speed
    one = post(client, '/upload', MultiZeroBody(1, 2 * MIB, 'files[]', 'a.svg', b'<svg'))
    assert "in one request" not in (one.get_json() or {}).get('error', '')
    two = post(client, '/upload', MultiZeroBody(2, 2 * MIB, 'files[]', 'a.svg', b'<svg'))
    assert two.status_code == 400
    assert "can't be more than 3 MB in one request" in two.get_json()['error']
    # PDFs don't count against it.
    pdfs = post(client, '/upload', MultiZeroBody(2, 2 * MIB, 'files[]', 'a.pdf', b'%PDF-'))
    assert "in one request" not in (pdfs.get_json() or {}).get('error', '')
    assert uploads  # imported for the monkeypatched config's sake


def test_api_merge_shares_the_free_limit_too(client, make_user, monkeypatch):
    monkeypatch.setitem(app.config, 'MAX_CONTENT_LENGTH', 3 * MIB)
    response = post(client, '/api/v1/merge',
                    MultiZeroBody(2, 2 * MIB, 'files[]', 'a.png', b'\x89PNG\r\n\x1a\n'),
                    headers=paid_key(make_user))
    assert response.status_code == 400
    assert "in one request" in response.get_json()['error']


def test_a_lying_archive_is_refused_not_re_flowed(tmp_path, monkeypatch, no_reflow):
    """verification-b2-bounds MINOR 1: zipfile stops a lying entry with a CRC
    error; that is an archive failure, so the file is refused."""
    src = tmp_path / 'liar.docx'
    wf.write_docx(src, {'word/document.xml': wf.document(wf.para('x'), '')})
    with zipfile.ZipFile(src, 'a', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('word/media/zeros.bin', b'\0' * (2 * MIB))
    real_infolist = zipfile.ZipFile.infolist

    def lying_infolist(self):
        infos = real_infolist(self)
        for info in infos:
            if info.filename == 'word/media/zeros.bin':
                info.file_size = 1000
        return infos
    monkeypatch.setattr(zipfile.ZipFile, 'infolist', lying_infolist)
    with pytest.raises(ConversionError, match="can't be converted"):
        get_converter('.docx')(str(src), str(tmp_path / 'out.pdf'),
                               RenderOptions(word_engine='libreoffice'))


def test_convert_page_meta_without_the_engine_is_the_free_limit(client, paid, monkeypatch):
    import re
    monkeypatch.setattr(office, 'available', lambda: False)
    html = client.get('/convert').get_data(as_text=True)
    assert re.search(r'docist-upload-limit" content="52428800"', html)
    monkeypatch.setattr(office, 'available', lambda: True)
    html = client.get('/convert').get_data(as_text=True)
    assert re.search(r'docist-upload-limit" content="94371840"', html)


def test_merge_lets_a_large_pdf_through_the_size_check(client, paid):
    response = post(client, '/upload', ZeroBody(51 * MIB, 'files[]', 'big.pdf', b'%PDF-'))
    # The zeros aren't a real PDF, so merging fails later; but not on size.
    assert 'over the 50 MB' not in (response.get_json() or {}).get('error', '')


def test_merge_without_the_engine_holds_word_files_to_the_free_limit(client, paid,
                                                                     monkeypatch):
    monkeypatch.setattr(office, 'available', lambda: False)
    response = post(client, '/upload', ZeroBody(51 * MIB, 'files[]', 'big.docx', b'PK'))
    assert "can't be more than 50 MB in one request" in response.get_json()['error']


@pytest.mark.parametrize('target,refused', [('.pdf', False), ('.txt', True), ('.png', True)])
def test_convert_lets_only_word_to_pdf_be_large(client, paid, monkeypatch, target, refused):
    monkeypatch.setattr(office, 'available', lambda: True)
    body = ZeroBody(51 * MIB, 'file', 'big.docx', b'PK', fields=[('target', target.encode())])
    error = post(client, '/convert/run', body).get_json().get('error', '')
    assert ("can't be more than 50 MB in one request" in error) is refused


def test_api_convert_holds_a_large_png_to_the_free_limit(client, make_user):
    body = ZeroBody(51 * MIB, 'file', 'big.png', b'\x89PNG\r\n\x1a\n',
                    fields=[('target', b'.jpg')])
    response = post(client, '/api/v1/convert', body, headers=paid_key(make_user))
    assert response.status_code == 400
    assert "can't be more than 50 MB in one request" in response.get_json()['error']


# --------------------------------------------------------------------------
# Blocker 7: merge and convert stream their results
# --------------------------------------------------------------------------
@pytest.fixture
def no_memory_send(monkeypatch):
    """Fail on the in-memory senders; record what send_file is handed."""
    def boom(*_a, **_k):
        raise AssertionError('the result was read into memory')
    monkeypatch.setattr(api_routes, '_send_bytes', boom)
    monkeypatch.setattr(api_routes, '_send_path', boom)
    sent = []
    real = api_routes.send_file

    def spy(path_or_file, *a, **k):
        sent.append(path_or_file)
        return real(path_or_file, *a, **k)
    monkeypatch.setattr(api_routes, 'send_file', spy)
    return sent


def assert_streamed_from_a_file(sent):
    assert len(sent) == 1
    handle = sent[0]
    assert not isinstance(handle, (bytes, io.BytesIO))
    assert handle.fileno() >= 0          # an open file on disk, not a buffer


def test_api_merge_streams_its_result(client, make_user, tmp_path, builders, no_memory_send):
    one = builders.pdf(tmp_path / 'one.pdf', pages=2)
    two = builders.pdf(tmp_path / 'two.pdf', pages=3)
    response = client.post('/api/v1/merge', data={
        'files[]': [(open(one, 'rb'), 'one.pdf'), (open(two, 'rb'), 'two.pdf')],
    }, content_type='multipart/form-data', headers=paid_key(make_user))
    assert response.status_code == 200
    assert_streamed_from_a_file(no_memory_send)
    assert int(response.headers['Content-Length']) > 0
    assert len(PdfReader(io.BytesIO(response.get_data())).pages) == 5
    assert 'one-merged.pdf' in response.headers['Content-Disposition']


def test_api_convert_streams_its_result(client, make_user, tmp_path, no_memory_send):
    src = wf.letter_1page(tmp_path / 'l.docx')
    response = client.post('/api/v1/convert', data={
        'file': (open(src, 'rb'), 'l.docx'), 'target': '.pdf',
    }, content_type='multipart/form-data', headers=paid_key(make_user))
    assert response.status_code == 200
    assert_streamed_from_a_file(no_memory_send)
    data = response.get_data()
    assert data.startswith(b'%PDF-')
    assert int(response.headers['Content-Length']) == len(data)
