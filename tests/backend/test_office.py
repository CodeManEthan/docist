"""The Word engine, pdf_ops/office.py (round prelaunch-fixes B2, design §5.2, §8).

Tests marked ``real_lo`` need a real LibreOffice and skip when
``office.available()`` is false. The rest drive a fake ``soffice`` written into
``tmp_path``, which records its pid, its process group and its resource limits,
starts a child in the same group, and then hangs, writes a PDF, or writes junk.
"""
import http.server
import math
import os
import signal
import stat
import sys
import textwrap
import threading
import time
import zipfile

import pytest
from flask import g
from pypdf import PdfReader
from werkzeug.datastructures import MultiDict

import app as flask_app_module
from converters import docx_converter
from converters.options import RenderOptions
from pdf_ops import office
from pdf_ops.office import OfficeError
from utils import render_opts

import word_fixtures as wf

app = flask_app_module.app

real_lo = pytest.mark.skipif(not office.available(), reason='LibreOffice not installed')

A4 = (595.3, 841.9)
LETTER = (612.0, 792.0)


def page_size(pdf_path, index=0):
    box = PdfReader(str(pdf_path)).pages[index].mediabox
    return float(box.width), float(box.height)


def close_to(size, expected, tol=1.0):
    return abs(size[0] - expected[0]) <= tol and abs(size[1] - expected[1]) <= tol


# --------------------------------------------------------------------------
# A fake soffice
# --------------------------------------------------------------------------
FAKE = textwrap.dedent('''\
    #!{python}
    import os, resource, subprocess, sys, time
    log = {log!r}
    mode = {mode!r}
    os.makedirs(log, exist_ok=True)
    def note(name, text):
        with open(os.path.join(log, name), 'w') as fh:
            fh.write(text)
    cpu = resource.getrlimit(resource.RLIMIT_CPU)[0]
    mem = resource.getrlimit(resource.RLIMIT_AS)[0]
    note('started', '%d %d %d %d' % (os.getpid(), os.getpgid(0), cpu, mem))
    child_code = (
        'import os, resource, sys, time\\n'
        'pgid = os.getpgid(0)\\n'
        'cpu = resource.getrlimit(resource.RLIMIT_CPU)[0]\\n'
        'open(sys.argv[1], "w").write(" ".join(map(str, (os.getpid(), pgid, cpu))))\\n'
        'time.sleep(300)\\n')
    child = subprocess.Popen([sys.executable, '-c', child_code, os.path.join(log, 'child')])
    for _ in range(100):
        if os.path.exists(os.path.join(log, 'child')):
            break
        time.sleep(0.02)
    outdir = sys.argv[sys.argv.index('--outdir') + 1]
    if mode == 'hang':
        time.sleep(300)
    elif mode == 'junk':
        open(os.path.join(outdir, 'input.pdf'), 'w').write('not a pdf')
    elif mode == 'pdf':
        open(os.path.join(outdir, 'input.pdf'), 'wb').write(b'%PDF-1.4 fake')
''')


@pytest.fixture
def fake_soffice(tmp_path, monkeypatch):
    """``make(mode)`` installs a fake soffice; returns its log directory."""
    made = []

    def make(mode):
        log = tmp_path / f'fake-{mode}-log'
        script = tmp_path / f'fake-soffice-{mode}'
        script.write_text(FAKE.format(python=sys.executable, log=str(log), mode=mode))
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setattr(office, 'soffice_path', lambda: str(script))
        made.append(log)
        return log

    yield make
    for log in made:   # never leave a sleeper behind, whatever a test did
        for name in ('started', 'child'):
            path = log / name
            if path.exists():
                pid = int(path.read_text().split()[0])
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass


def started_pids(log):
    pids = []
    for name in ('started', 'child'):
        path = log / name
        if path.exists():
            pids.append(int(path.read_text().split()[0]))
    return pids


def alive(pid):
    """True while ``pid`` runs; a zombie counts as gone."""
    try:
        with open(f'/proc/{pid}/stat') as fh:
            state = fh.read().rsplit(')', 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state not in ('Z', 'X')


def wait_gone(pids, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not any(alive(p) for p in pids):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def docx(tmp_path):
    return wf.letter_1page(tmp_path / 'letter.docx')


# --------------------------------------------------------------------------
# Step 1: Word only
# --------------------------------------------------------------------------
def test_odt_renamed_docx_never_reaches_soffice(tmp_path, fake_soffice):
    log = fake_soffice('pdf')
    src = wf.odt(tmp_path / 'renamed.docx')
    with pytest.raises(OfficeError, match='not a Word document'):
        office.docx_to_pdf(str(src), str(tmp_path / 'out.pdf'))
    assert not (log / 'started').exists()


def test_package_without_word_main_part_is_refused(tmp_path, fake_soffice):
    log = fake_soffice('pdf')
    src = tmp_path / 'sheet.docx'
    ct = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
          'officedocument.spreadsheetml.sheet.main+xml"/></Types>')
    with zipfile.ZipFile(src, 'w') as zf:
        zf.writestr('[Content_Types].xml', ct)
        zf.writestr('xl/workbook.xml', '<workbook/>')
    with pytest.raises(OfficeError, match='not a Word document'):
        office.docx_to_pdf(str(src), str(tmp_path / 'out.pdf'))
    assert not (log / 'started').exists()


def test_declared_word_part_must_exist(tmp_path, fake_soffice):
    fake_soffice('pdf')
    src = tmp_path / 'hollow.docx'
    with zipfile.ZipFile(src, 'w') as zf:
        zf.writestr('[Content_Types].xml', wf._content_types())
    with pytest.raises(OfficeError, match='not a Word document'):
        office.docx_to_pdf(str(src), str(tmp_path / 'out.pdf'))


def test_not_a_zip_is_refused(tmp_path, fake_soffice):
    fake_soffice('pdf')
    src = tmp_path / 'junk.docx'
    src.write_bytes(b'PK\x03\x04 but not really a zip')
    with pytest.raises(OfficeError, match='not a Word document'):
        office.docx_to_pdf(str(src), str(tmp_path / 'out.pdf'))


def test_soffice_gets_the_word_infilter(tmp_path, fake_soffice, monkeypatch, docx):
    fake_soffice('pdf')
    seen = {}
    real_popen = office.subprocess.Popen

    def spy(cmd, **kwargs):
        seen['cmd'] = cmd
        seen['kwargs'] = kwargs
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(office.subprocess, 'Popen', spy)
    office.docx_to_pdf(str(docx), str(tmp_path / 'out.pdf'))
    assert '--infilter=MS Word 2007 XML' in seen['cmd']
    assert seen['kwargs']['start_new_session'] is True
    profile_arg = [a for a in seen['cmd'] if a.startswith('-env:UserInstallation=file://')]
    assert len(profile_arg) == 1
    # The child sees no proxy settings and a throwaway HOME.
    env = seen['kwargs']['env']
    assert not any('proxy' in k.lower() for k in env)


# --------------------------------------------------------------------------
# Step 2: the strip
# --------------------------------------------------------------------------
def _strip(rel_items, body):
    import io
    data = wf.rels(*rel_items).encode()
    doc = wf.document(body, '').encode()
    new, removed = office.strip_rels(
        data, lambda ids: office.scan_references(io.BytesIO(doc), ids))
    return new.decode(), removed


def test_strip_keeps_a_hyperlink_used_as_hyperlink():
    new, removed = _strip([('rId7', 'hyperlink', 'http://x.example/a', True)],
                          '<w:p><w:hyperlink r:id="rId7"/></w:p>')
    assert removed == []
    assert 'rId7' in new


def test_strip_removes_hyperlink_type_used_as_image_link():
    new, removed = _strip([('rId5', 'hyperlink', 'http://x.example/i.png', True)],
                          wf._picture('rId5', 'r:link'))
    assert removed == ['rId5']
    assert 'rId5' not in new


def test_strip_removes_a_hyperlink_also_used_as_image_link():
    body = '<w:p><w:hyperlink r:id="rId5"/></w:p>' + wf._picture('rId5', 'r:link')
    _, removed = _strip([('rId5', 'hyperlink', 'http://x.example/i.png', True)], body)
    assert removed == ['rId5']


def test_strip_removes_an_unreferenced_external():
    _, removed = _strip([('rId3', 'hyperlink', 'http://x.example/', True)], '<w:p/>')
    assert removed == ['rId3']


def test_strip_keeps_internal_parts():
    new, removed = _strip([('rId8', 'image', 'media/image1.png', False)],
                          wf._picture('rId8', 'r:embed'))
    assert removed == [] and 'media/image1.png' in new


@pytest.mark.parametrize('target', ['http://x.example/i.png', 'file:///etc/passwd',
                                    '//host/share/i.png', '\\\\host\\share\\i.png'])
def test_strip_treats_an_internal_mode_url_as_outside(target):
    _, removed = _strip([('rId8', 'image', target, False)], wf._picture('rId8', 'r:embed'))
    assert removed == ['rId8']


def test_strip_external_drops_package_level_and_settings_targets(tmp_path):
    src = wf.network_guard(tmp_path / 'net.docx', 'http://127.0.0.1:9')
    dst = tmp_path / 'clean.docx'
    removed = office.strip_external(str(src), str(dst))
    assert sorted(removed) == ['rId5', 'rId6', 'rId9']
    with zipfile.ZipFile(dst) as zf:
        doc_rels = zf.read('word/_rels/document.xml.rels').decode()
        settings_rels = zf.read('word/_rels/settings.xml.rels').decode()
        assert zf.read('word/document.xml') == zipfile.ZipFile(src).read('word/document.xml')
    assert 'rId7' in doc_rels and 'rId8' in doc_rels
    assert 'rId5' not in doc_rels and 'rId6' not in doc_rels
    assert 'template.dotx' not in settings_rels


def test_duplicate_part_names_are_refused(tmp_path, fake_soffice):
    """Two document.xml entries: the strip could read one, LibreOffice the other."""
    log = fake_soffice('pdf')
    src = tmp_path / 'dup.docx'
    with zipfile.ZipFile(src, 'w') as zf:
        zf.writestr('[Content_Types].xml', wf._content_types())
        zf.writestr('word/document.xml', wf.document(wf._picture('rId5', 'r:link'), ''))
        zf.writestr('WORD/Document.xml', wf.document(
            '<w:p><w:hyperlink r:id="rId5"/></w:p>', ''))
        zf.writestr('word/_rels/document.xml.rels',
                    wf.rels(('rId5', 'hyperlink', 'http://x.example/i.png', True)))
    with pytest.raises(OfficeError, match='duplicate'):
        office.docx_to_pdf(str(src), str(tmp_path / 'out.pdf'))
    assert not (log / 'started').exists()


def test_upper_case_rels_part_is_stripped_too(tmp_path):
    src = wf.write_docx(tmp_path / 'upper.docx', {
        'word/document.xml': wf.document(wf._picture('rId5', 'r:link'), ''),
        'WORD/_RELS/DOCUMENT.XML.RELS': wf.rels(
            ('rId5', 'hyperlink', 'http://x.example/i.png', True)),
    })
    removed = office.strip_external(str(src), str(tmp_path / 'out.docx'))
    assert removed == ['rId5']


def test_soffice_that_cannot_start_raises_office_error(tmp_path, monkeypatch, docx):
    monkeypatch.setattr(office, 'soffice_path', lambda: str(tmp_path / 'missing-binary'))
    with pytest.raises(OfficeError, match='could not start'):
        office.docx_to_pdf(str(docx), str(tmp_path / 'out.pdf'))


def test_strip_bounds_what_it_unpacks(tmp_path, monkeypatch, docx):
    monkeypatch.setattr(office, 'MAX_UNPACKED_BYTES', 100)
    with pytest.raises(OfficeError, match='unpacks too large'):
        office.strip_external(str(docx), str(tmp_path / 'x.docx'))


def test_strip_bounds_rels_part_size(tmp_path, monkeypatch, docx):
    monkeypatch.setattr(office, 'MAX_RELS_PART_BYTES', 50)
    with pytest.raises(OfficeError, match='too large'):
        office.strip_external(str(docx), str(tmp_path / 'x.docx'))


def test_strip_bounds_scanned_part_size(tmp_path, monkeypatch):
    src = wf.network_guard(tmp_path / 'net.docx', 'http://127.0.0.1:9')
    monkeypatch.setattr(office, 'MAX_XML_PART_BYTES', 100)
    with pytest.raises(OfficeError, match='too large'):
        office.strip_external(str(src), str(tmp_path / 'x.docx'))


# --------------------------------------------------------------------------
# The strip's cost stays linear and inside the deadline (verification-b2
# MAJOR 1, MINOR 1)
# --------------------------------------------------------------------------
def _unreferenced_rels(n):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<Relationships xmlns="{wf.PKG}">'
            + ''.join(f'<Relationship Id="r{i}" Type="t" Target="http://x/" '
                      'TargetMode="External"/>' for i in range(n))
            + '</Relationships>')


def test_rels_over_the_cap_is_refused_before_parsing(tmp_path):
    """The verifier's 160,000-relationship part: 20 s of quadratic removal
    before; now refused at the 1 MiB cap without being parsed."""
    rels = _unreferenced_rels(160_000)
    assert len(rels) > office.MAX_RELS_PART_BYTES
    src = wf.write_docx(tmp_path / 'many.docx', {
        'word/document.xml': wf.document(wf.para('x'), ''),
        'word/_rels/document.xml.rels': rels,
    })
    t0 = time.monotonic()
    with pytest.raises(OfficeError, match='too large'):
        office.strip_external(str(src), str(tmp_path / 'out.docx'))
    assert time.monotonic() - t0 < 1


def test_removal_is_linear_at_the_rels_cap(tmp_path):
    """As many relationships as fit under the cap, all removed: one pass."""
    n = 1
    while len(_unreferenced_rels(n * 2)) <= office.MAX_RELS_PART_BYTES:
        n *= 2
    src = wf.write_docx(tmp_path / 'many.docx', {
        'word/document.xml': wf.document(wf.para('x'), ''),
        'word/_rels/document.xml.rels': _unreferenced_rels(n),
    })
    t0 = time.monotonic()
    removed = office.strip_external(str(src), str(tmp_path / 'out.docx'))
    assert len(removed) == n
    assert time.monotonic() - t0 < 2


def test_scanning_a_huge_part_keeps_memory_flat():
    """The verifier's 33 KB upload of a 33.5 MB part of empty elements took
    1.7 GB to parse into a tree. The scan builds no tree."""
    import io
    import tracemalloc

    class Elements(io.RawIOBase):
        def __init__(self, count):
            self.head = f'<w:document xmlns:w="{wf.W}">'.encode()
            self.left = count
            self.done = False

        def readable(self):
            return True

        def read(self, n=-1):
            if self.head:
                out, self.head = self.head, b''
                return out
            if self.left:
                k = min(self.left, 1024 * 1024 // 4)
                self.left -= k
                return b'<a/>' * k
            if not self.done:
                self.done = True
                return b'</w:document>'
            return b''

    tracemalloc.start()
    try:
        refs = office.scan_references(Elements(2_000_000), {'rId1'})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert refs == {'rId1': []}
    assert peak < 32 * 1024 * 1024


def test_deep_nesting_is_refused_with_flat_memory():
    """verification-b2-recheck MINOR 1: 10 million nested elements from a 69 KB
    upload took 3 GB of stack. Past MAX_XML_DEPTH the scan stops."""
    import io
    import tracemalloc
    doc = f'<w:document xmlns:w="{wf.W}">'.encode() + b'<a>' * 2_000_000
    tracemalloc.start()
    try:
        with pytest.raises(OfficeError, match='nests too deeply'):
            office.scan_references(io.BytesIO(doc), {'rId1'})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 16 * 1024 * 1024


def test_normal_nesting_passes():
    import io
    depth = 200
    doc = (f'<w:document xmlns:w="{wf.W}">' + '<a>' * depth + '</a>' * depth
           + '</w:document>').encode()
    assert office.scan_references(io.BytesIO(doc), {'rId1'}) == {'rId1': []}


@pytest.mark.parametrize('text', [
    'rId5<w:x/>junk',            # text before the first child, as elem.text
    ' ' * 70 + 'rId5',           # long leading whitespace
    'rId5' + ' ' * 5000,         # long trailing whitespace
    '\n  rI<![CDATA[d]]>5  \n',  # split across character-data calls
])
def test_text_references_match_the_tree_walk(text):
    """verification-b2-recheck MINOR 2: a text use of a hyperlink's id is a
    non-hyperlink use, however the text is laid out."""
    body = f'<w:p><w:hyperlink r:id="rId5"/></w:p><w:p><w:t>{text}</w:t></w:p>'
    _, removed = _strip([('rId5', 'hyperlink', 'http://x.example/', True)], body)
    assert removed == ['rId5']


@pytest.mark.parametrize('text', ['rId5 x', 'xrId5', 'rId55', ' ' * 70 + 'rId5' + ' ' * 70 + 'x'])
def test_text_that_is_not_the_id_is_not_a_reference(text):
    body = f'<w:p><w:hyperlink r:id="rId5"/></w:p><w:p><w:t>{text}</w:t></w:p>'
    _, removed = _strip([('rId5', 'hyperlink', 'http://x.example/', True)], body)
    assert removed == []


def test_the_deadline_stops_the_strip(tmp_path, fake_soffice):
    """The strip runs inside the request's deadline: a check that says time is
    up stops it, and soffice never starts."""
    log = fake_soffice('pdf')
    src = wf.network_guard(tmp_path / 'net.docx', 'http://127.0.0.1:9')
    calls = []

    def check():
        calls.append(1)
        if len(calls) > 3:
            raise OfficeError('out of time')

    with pytest.raises(OfficeError, match='out of time'):
        office.strip_external(str(src), str(tmp_path / 'out.docx'), check=check)
    with pytest.raises(OfficeError, match='out of time'):
        office.docx_to_pdf(str(src), str(tmp_path / 'out.pdf'),
                           deadline=time.monotonic() + office.RESERVE_SECONDS + 5)
    assert not (log / 'started').exists()


# --------------------------------------------------------------------------
# Shapes the strip can't vouch for are refused (verification-b2 MINOR 2)
# --------------------------------------------------------------------------
def _docx_with(tmp_path, name, parts):
    base = {'word/document.xml': wf.document(wf.para('x'), '')}
    base.update(parts)
    return wf.write_docx(tmp_path / name, base)


def test_nested_relationships_are_refused(tmp_path):
    nested = (f'<?xml version="1.0"?><Relationships xmlns="{wf.PKG}">'
              f'<Relationships><Relationship Id="rId5" Type="t" Target="http://x/i.png" '
              'TargetMode="External"/></Relationships></Relationships>')
    src = _docx_with(tmp_path, 'nested.docx', {'word/_rels/document.xml.rels': nested})
    with pytest.raises(OfficeError, match='unexpected element'):
        office.strip_external(str(src), str(tmp_path / 'out.docx'))


def test_a_rels_with_another_root_is_refused(tmp_path):
    src = _docx_with(tmp_path, 'root.docx', {
        'word/_rels/document.xml.rels': '<?xml version="1.0"?><Other/>'})
    with pytest.raises(OfficeError, match='unexpected root'):
        office.strip_external(str(src), str(tmp_path / 'out.docx'))


def test_a_relationship_with_children_is_refused(tmp_path):
    rels = (f'<?xml version="1.0"?><Relationships xmlns="{wf.PKG}">'
            '<Relationship Id="rId1" Type="t" Target="a.xml"><x/></Relationship>'
            '</Relationships>')
    src = _docx_with(tmp_path, 'kids.docx', {'word/_rels/document.xml.rels': rels})
    with pytest.raises(OfficeError, match='unexpected element'):
        office.strip_external(str(src), str(tmp_path / 'out.docx'))


@pytest.mark.parametrize('part', ['word/document.xml', 'word/styles.xml',
                                  'word/_rels/document.xml.rels', 'customXml/item1.xml'])
def test_a_doctype_in_any_part_is_refused(tmp_path, part):
    xml = (f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "http://127.0.0.1:9/e">]>'
           f'<w:document xmlns:w="{wf.W}"><w:body/></w:document>')
    src = _docx_with(tmp_path, 'dtd.docx', {part: xml})
    with pytest.raises(OfficeError, match='DOCTYPE'):
        office.strip_external(str(src), str(tmp_path / 'out.docx'))


def test_a_doctype_split_across_chunks_is_found(tmp_path):
    filler = b' ' * (1024 * 1024 - 4)
    src = tmp_path / 'split.docx'
    wf.write_docx(src, {'word/document.xml': wf.document('', '')})
    with zipfile.ZipFile(src, 'a') as zf:
        zf.writestr('word/media/note.txt', filler + b'<!DOCTYPE x>')
    with pytest.raises(OfficeError, match='DOCTYPE'):
        office.strip_external(str(src), str(tmp_path / 'out.docx'))


@pytest.mark.parametrize('magic', [b'PK\x03\x04', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'])
def test_an_embedded_package_is_refused(tmp_path, magic):
    src = tmp_path / 'embed.docx'
    wf.write_docx(src, {'word/document.xml': wf.document('', '')})
    with zipfile.ZipFile(src, 'a') as zf:
        zf.writestr('word/embeddings/inner.bin', magic + b'rest of a package')
    with pytest.raises(OfficeError, match='embedded package'):
        office.strip_external(str(src), str(tmp_path / 'out.docx'))


def test_utf16_xml_is_refused(tmp_path):
    xml = wf.document(wf.para('x'), '').replace('encoding="UTF-8"', 'encoding="UTF-16"')
    src = tmp_path / 'u16.docx'
    wf.write_docx(src, {'word/document.xml': wf.document('', '')})
    with zipfile.ZipFile(src, 'a') as zf:
        zf.writestr('word/styles.xml', xml.encode('utf-16'))
    with pytest.raises(OfficeError, match='not UTF-8'):
        office.strip_external(str(src), str(tmp_path / 'out.docx'))


def test_any_error_in_the_strip_becomes_office_error(tmp_path, fake_soffice, monkeypatch, docx):
    """verification-b2 NIT 2: whatever a malformed package raises, the caller
    gets OfficeError and so the reflow and its note."""
    fake_soffice('pdf')

    def boom(*_a, **_k):
        raise ValueError('damaged header')
    monkeypatch.setattr(office, 'strip_external', boom)
    with pytest.raises(OfficeError, match='unreadable Word package'):
        office.docx_to_pdf(str(docx), str(tmp_path / 'out.pdf'))


def test_strip_cost_is_one_walk_however_many_externals(tmp_path):
    """5000 external targets against a 60,000-element document: one tree walk,
    not one per relationship (which would be 3e8 element visits)."""
    items = [(f'rId{i}', 'hyperlink', f'http://x.example/{i}', True) for i in range(5000)]
    body = ''.join('<w:p><w:r><w:t>x</w:t></w:r></w:p>' for _ in range(15000))
    body += '<w:p><w:hyperlink r:id="rId1"/></w:p>'
    src = wf.write_docx(tmp_path / 'many.docx', {
        'word/document.xml': wf.document(body, wf.sect(wf.LETTER_TWIPS)),
        'word/_rels/document.xml.rels': wf.rels(*items),
    })
    t0 = time.monotonic()
    removed = office.strip_external(str(src), str(tmp_path / 'out.docx'))
    assert time.monotonic() - t0 < 5
    assert len(removed) == 4999 and 'rId1' not in removed


def test_entity_expansion_in_a_part_is_refused(tmp_path):
    bomb = ('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">'
            + ''.join(f'<!ENTITY {chr(98 + i)} "{("&" + chr(97 + i) + ";") * 10}">'
                      for i in range(8))
            + ']><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
              'relationships">&i;</Relationships>')
    src = wf.write_docx(tmp_path / 'bomb.docx', {
        'word/document.xml': wf.document('', ''),
        'word/_rels/document.xml.rels': bomb,
    })
    t0 = time.monotonic()
    with pytest.raises(OfficeError):
        office.strip_external(str(src), str(tmp_path / 'out.docx'))
    assert time.monotonic() - t0 < 5


# --------------------------------------------------------------------------
# Each network guard alone, against a local server that logs requests
# --------------------------------------------------------------------------
@pytest.fixture
def logging_server():
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(404)
            self.end_headers()

        do_HEAD = do_GET

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}', hits
    finally:
        server.shutdown()
        server.server_close()


@real_lo
def test_unguarded_libreoffice_fetches(tmp_path, logging_server):
    """The control: with both guards off, the fixture does make requests, so
    the two tests below can tell a guard that works from one that doesn't."""
    base, hits = logging_server
    src = wf.network_guard(tmp_path / 'net.docx', base)
    office.docx_to_pdf(str(src), str(tmp_path / 'out.pdf'), strip=False, profile=False)
    assert '/spoofed.png' in hits


@real_lo
def test_locked_profile_alone_blocks_fetches(tmp_path, logging_server):
    base, hits = logging_server
    src = wf.network_guard(tmp_path / 'net.docx', base)
    office.docx_to_pdf(str(src), str(tmp_path / 'out.pdf'), strip=False, profile=True)
    assert hits == []
    assert PdfReader(str(tmp_path / 'out.pdf')).pages


@real_lo
def test_strip_alone_blocks_fetches(tmp_path, logging_server):
    base, hits = logging_server
    src = wf.network_guard(tmp_path / 'net.docx', base)
    office.docx_to_pdf(str(src), str(tmp_path / 'out.pdf'), strip=True, profile=False)
    assert hits == []
    assert PdfReader(str(tmp_path / 'out.pdf')).pages


# --------------------------------------------------------------------------
# Steps 4 and 5: time, and the process group
# --------------------------------------------------------------------------
def test_no_deadline_means_the_office_timeout_alone(monkeypatch):
    monkeypatch.delenv('DOCIST_OFFICE_TIMEOUT', raising=False)
    assert office.call_timeout(None) == office.DEFAULT_TIMEOUT
    monkeypatch.setenv('DOCIST_OFFICE_TIMEOUT', '7')
    assert office.call_timeout(None) == 7


def test_call_timeout_is_min_of_timeout_and_deadline_less_reserve(monkeypatch):
    monkeypatch.delenv('DOCIST_OFFICE_TIMEOUT', raising=False)
    now = 1000.0
    assert office.call_timeout(now + 90, now=now) == 60          # upload took 0 s
    assert office.call_timeout(now + 30, now=now) == 15          # upload took 60 s
    with pytest.raises(OfficeError, match='out of time'):
        office.call_timeout(now + 5, now=now)                    # upload took 85 s
    with pytest.raises(OfficeError, match='out of time'):
        office.call_timeout(now + 24.9, now=now)                 # 9.9 s left: not started


def test_under_ten_seconds_left_never_starts_soffice(tmp_path, fake_soffice, docx):
    log = fake_soffice('pdf')
    with pytest.raises(OfficeError, match='out of time'):
        office.docx_to_pdf(str(docx), str(tmp_path / 'out.pdf'),
                           deadline=time.monotonic() + office.RESERVE_SECONDS + 9)
    assert not (log / 'started').exists()


def test_timeout_kills_the_whole_group(tmp_path, fake_soffice, monkeypatch, docx):
    log = fake_soffice('hang')
    monkeypatch.setenv('DOCIST_OFFICE_TIMEOUT', '1.5')
    t0 = time.monotonic()
    with pytest.raises(OfficeError, match='timed out'):
        office.docx_to_pdf(str(docx), str(tmp_path / 'out.pdf'))
    assert time.monotonic() - t0 < 5
    pids = started_pids(log)
    assert len(pids) == 2
    assert wait_gone(pids)


def test_system_exit_while_waiting_still_kills_the_group(tmp_path, fake_soffice,
                                                         monkeypatch, docx):
    """gunicorn's sync worker raises SystemExit from its SIGABRT handler when it
    times a worker out; the finally must still kill the group."""
    log = fake_soffice('hang')
    monkeypatch.setenv('DOCIST_OFFICE_TIMEOUT', '30')

    def abort(_signum, _frame):
        raise SystemExit(1)

    old = signal.signal(signal.SIGALRM, abort)
    try:
        signal.setitimer(signal.ITIMER_REAL, 1.0)
        with pytest.raises(SystemExit):
            office.docx_to_pdf(str(docx), str(tmp_path / 'out.pdf'))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    pids = started_pids(log)
    assert len(pids) == 2
    assert wait_gone(pids)


def test_group_is_killed_after_success_too(tmp_path, fake_soffice, docx):
    """The fake writes its PDF and exits, leaving a child sleeping in its group."""
    log = fake_soffice('pdf')
    office.docx_to_pdf(str(docx), str(tmp_path / 'out.pdf'))
    assert (tmp_path / 'out.pdf').read_bytes().startswith(b'%PDF-')
    assert wait_gone(started_pids(log))


def test_limits_reach_every_process_in_the_group(tmp_path, fake_soffice, monkeypatch, docx):
    """The guard for the exits no finally sees (the worker SIGKILLed by gunicorn,
    the OOM killer or docker stop): every process in the group carries the CPU
    limit, three times the wall timeout, and the address-space cap."""
    log = fake_soffice('pdf')
    monkeypatch.setenv('DOCIST_OFFICE_TIMEOUT', '20')
    monkeypatch.setenv('DOCIST_OFFICE_MEM_MB', '1024')
    office.docx_to_pdf(str(docx), str(tmp_path / 'out.pdf'))
    pid, pgid, cpu, mem = map(int, (log / 'started').read_text().split())
    child_pid, child_pgid, child_cpu = map(int, (log / 'child').read_text().split())
    assert pgid == pid and child_pgid == pid      # its own group, child included
    assert pgid != os.getpgid(0)
    assert cpu == child_cpu == math.ceil(20 * 3)
    assert mem == 1024 * 1024 * 1024


# --------------------------------------------------------------------------
# Step 7: the output check
# --------------------------------------------------------------------------
def test_non_pdf_output_raises(tmp_path, fake_soffice, docx):
    fake_soffice('junk')
    with pytest.raises(OfficeError, match='no PDF'):
        office.docx_to_pdf(str(docx), str(tmp_path / 'out.pdf'))
    assert not (tmp_path / 'out.pdf').exists()


def test_missing_output_raises(tmp_path, fake_soffice, docx):
    fake_soffice('none')
    with pytest.raises(OfficeError, match='no PDF'):
        office.docx_to_pdf(str(docx), str(tmp_path / 'out.pdf'))


def test_not_installed_raises(tmp_path, monkeypatch, docx):
    monkeypatch.setattr(office, 'soffice_path', lambda: None)
    assert office.available() is False
    with pytest.raises(OfficeError, match='not installed'):
        office.docx_to_pdf(str(docx), str(tmp_path / 'out.pdf'))


# --------------------------------------------------------------------------
# The deadline counts from g.request_started (render_opts, design §5.2 step 4)
# --------------------------------------------------------------------------
def test_deadline_is_set_from_request_start(client):
    with app.test_request_context('/upload', method='POST'):
        g.request_started = 500.0
        opts = render_opts.from_form(MultiDict())
        assert opts.deadline == 500.0 + app.config['RENDER_BUDGET']


def test_deadline_is_none_when_request_start_is_missing(client):
    """Verification MINOR 3: a request the tier step never reached, or a call
    outside any request, gets no deadline instead of an AttributeError."""
    with app.test_request_context('/upload', method='POST'):
        assert render_opts.from_form(MultiDict()).deadline is None
    assert render_opts.from_form(MultiDict()).deadline is None


def test_a_request_that_spent_85s_uploading_skips_libreoffice(tmp_path, client,
                                                              fake_soffice, docx):
    log = fake_soffice('pdf')
    paid = type('U', (), {'is_verified': True, 'is_paid': True})()
    with app.test_request_context('/upload', method='POST'):
        g.request_started = time.monotonic() - 85
        opts = render_opts.from_form(MultiDict(), paid)
        assert opts.word_engine == 'libreoffice'
        docx_converter.convert(str(docx), str(tmp_path / 'out.pdf'), opts)
    assert not (log / 'started').exists()
    assert opts.notes == [docx_converter.FALLBACK_NOTE]
    assert page_size(tmp_path / 'out.pdf') == LETTER


# --------------------------------------------------------------------------
# A two-file Merge with a hanging soffice (design §8)
# --------------------------------------------------------------------------
def test_two_file_merge_with_hanging_soffice_finishes_in_budget(
        tmp_path, client, make_user, login, fake_soffice, monkeypatch):
    log = fake_soffice('hang')
    # A 4 s budget: the first call gets 4 - 1.5 = 2.5 s and is killed; the
    # second has under MIN_CALL_SECONDS left and never starts.
    monkeypatch.setitem(app.config, 'RENDER_BUDGET', 4)
    monkeypatch.setattr(office, 'RESERVE_SECONDS', 1.5)
    monkeypatch.setattr(office, 'MIN_CALL_SECONDS', 0.5)
    user = make_user(plan='monthly')
    login(client, user)
    one = wf.letter_1page(tmp_path / 'one.docx')
    two = wf.a4_header_footer(tmp_path / 'two.docx')
    t0 = time.monotonic()
    response = client.post('/upload', data={
        'files[]': [(open(one, 'rb'), 'one.docx'), (open(two, 'rb'), 'two.docx')],
        'page_numbers': 'false',
    }, content_type='multipart/form-data')
    elapsed = time.monotonic() - t0
    assert response.status_code == 200, response.get_json()
    assert elapsed < 4
    body = response.get_json()
    assert body['message'].count(docx_converter.FALLBACK_NOTE) == 1
    merged = client.output_dir / body['filename']
    reader = PdfReader(str(merged))
    # Both reflowed onto Letter: the A4 file didn't keep its own size.
    assert all(close_to((float(p.mediabox.width), float(p.mediabox.height)), LETTER)
               for p in reader.pages)
    text = ''.join(p.extract_text() or '' for p in reader.pages)
    assert wf.LETTER_MARKER in text and 'An A4 document' in text
    pids = started_pids(log)
    assert len(pids) == 2          # one call started, its child too; the second never did
    assert wait_gone(pids)


# --------------------------------------------------------------------------
# The fixture set on a real LibreOffice, under the default memory cap
# --------------------------------------------------------------------------
@real_lo
@pytest.mark.parametrize('build,pages,size,marker', [
    (wf.letter_1page, 1, LETTER, wf.LETTER_MARKER),
    (wf.table_36pages, None, LETTER, wf.TABLE_MARKER),
    (wf.a4_header_footer, 2, A4, wf.HEADER_MARKER),
    (wf.calibri_cambria, 1, LETTER, wf.FONT_MARKER),
])
def test_fixture_set_converts_under_the_memory_cap(tmp_path, monkeypatch, build, pages,
                                                   size, marker):
    monkeypatch.delenv('DOCIST_OFFICE_MEM_MB', raising=False)
    src = build(tmp_path / 'doc.docx')
    out = tmp_path / 'out.pdf'
    office.docx_to_pdf(str(src), str(out))
    reader = PdfReader(str(out))
    if pages is None:
        assert len(reader.pages) >= 30
    else:
        assert len(reader.pages) == pages
    assert close_to(page_size(out), size)
    text = ''.join(p.extract_text() or '' for p in reader.pages)
    assert marker in text


@real_lo
def test_header_and_footer_survive(tmp_path):
    src = wf.a4_header_footer(tmp_path / 'hf.docx')
    office.docx_to_pdf(str(src), str(tmp_path / 'out.pdf'))
    second = PdfReader(str(tmp_path / 'out.pdf')).pages[1].extract_text()
    assert wf.HEADER_MARKER in second and wf.FOOTER_MARKER in second


@real_lo
def test_calibri_is_rendered_with_carlito(tmp_path):
    fonts = os.popen('fc-list').read().lower()
    if 'carlito' not in fonts:
        pytest.skip('Carlito not installed')
    src = wf.calibri_cambria(tmp_path / 'fonts.docx')
    office.docx_to_pdf(str(src), str(tmp_path / 'out.pdf'))
    page = PdfReader(str(tmp_path / 'out.pdf')).pages[0]
    names = {str(page['/Resources']['/Font'][k].get_object()['/BaseFont'])
             for k in page['/Resources']['/Font']}
    assert any('Carlito' in n for n in names), names
    assert any('Caladea' in n for n in names), names
