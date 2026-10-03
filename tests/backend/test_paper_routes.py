"""The paper choice and OCR language selects on the browser routes
(round prelaunch-fixes, design §3.4, §4.2): Merge, Convert and Print Prep
take `paper`; Convert takes `language` for image to .txt; notes reach the
page message.
"""
import io

import pytest
from pypdf import PdfReader

import converters

LETTER = (612.0, 792.0)
A4 = (595.28, 841.89)


def _size(path, index=0):
    box = PdfReader(str(path)).pages[index].mediabox
    return round(float(box.width), 2), round(float(box.height), 2)


def _post(client, url, **data):
    return client.post(url, data=data, content_type='multipart/form-data')


# --------------------------------------------------------------------- merge
@pytest.mark.parametrize('paper, expected', [(None, LETTER), ('letter', LETTER), ('a4', A4)])
def test_merge_converts_on_the_chosen_paper(client, tmp_path, builders, paper, expected):
    md = builders.markdown(tmp_path / 'doc.md')
    data = {'files[]': (io.BytesIO(md.read_bytes()), 'doc.md')}
    if paper:
        data['paper'] = paper
    resp = _post(client, '/upload', **data)
    assert resp.status_code == 200, resp.get_json()
    assert _size(client.output_dir / resp.get_json()['filename']) == expected


def test_merge_keeps_pdf_pages(client, tmp_path, builders):
    pdf = builders.pdf(tmp_path / 'p.pdf', pages=1)
    resp = _post(client, '/upload', paper='a4',
                 **{'files[]': (io.BytesIO(pdf.read_bytes()), 'p.pdf')})
    assert _size(client.output_dir / resp.get_json()['filename']) == LETTER


def test_merge_bad_paper_is_400(client, tmp_path, builders):
    md = builders.markdown(tmp_path / 'doc.md')
    resp = _post(client, '/upload', paper='legal',
                 **{'files[]': (io.BytesIO(md.read_bytes()), 'doc.md')})
    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'Paper must be letter or a4.'


def test_merge_message_carries_notes(client, tmp_path, builders, monkeypatch):
    real = converters.get_converter('.md')

    def noting(i, o, opts=None):
        real(i, o, opts)
        opts.notes.append('A note from the renderer.')

    monkeypatch.setitem(converters._REGISTRY, '.md', noting)
    md = builders.markdown(tmp_path / 'doc.md')
    resp = _post(client, '/upload', **{'files[]': (io.BytesIO(md.read_bytes()), 'doc.md')})
    assert resp.get_json()['message'] == 'Successfully merged 1 file(s) A note from the renderer.'


# ------------------------------------------------------------------- convert
def test_convert_targets_mark_paper_and_ocr(client):
    data = client.get('/convert/targets?ext=.md').get_json()
    assert '.pdf' in data['paper_targets']
    assert '.png' in data['paper_targets']          # pivots through PDF
    assert data['ocr_targets'] == []
    png = client.get('/convert/targets?ext=.png').get_json()
    assert png['ocr_targets'] == ['.txt']
    assert '.jpg' not in png['paper_targets']
    assert '.pdf' in png['paper_targets']
    pdf = client.get('/convert/targets?ext=.pdf').get_json()
    assert pdf['paper_targets'] == []


def test_convert_to_pdf_on_a4(client, tmp_path, builders):
    md = builders.markdown(tmp_path / 'doc.md')
    resp = _post(client, '/convert/run', target='.pdf', paper='a4',
                 file=(io.BytesIO(md.read_bytes()), 'doc.md'))
    assert resp.status_code == 200, resp.get_json()
    assert _size(client.output_dir / resp.get_json()['filename']) == A4


def test_convert_bad_paper_is_400_only_where_read(client, tmp_path, builders):
    md = builders.markdown(tmp_path / 'doc.md')
    resp = _post(client, '/convert/run', target='.pdf', paper='legal',
                 file=(io.BytesIO(md.read_bytes()), 'doc.md'))
    assert resp.status_code == 400
    png = builders.png(tmp_path / 'p.png')
    resp = _post(client, '/convert/run', target='.jpg', paper='legal',
                 file=(io.BytesIO(png.read_bytes()), 'p.png'))
    assert resp.status_code == 200


def test_convert_image_to_text_validates_language(client, tmp_path, builders, monkeypatch,
                                                  shared_list):
    import pytesseract
    from pdf_ops import ocr as ocr_ops
    from transforms import ocr_text
    seen = shared_list()
    monkeypatch.setattr(ocr_text, 'is_available', lambda: True)
    monkeypatch.setattr(ocr_ops, 'is_available', lambda: True)
    monkeypatch.setattr(ocr_ops, 'installed_languages', lambda: ['eng', 'spa', 'fra'])
    monkeypatch.setattr(pytesseract, 'image_to_string',
                        lambda image, lang=None, **k: seen.append(lang) or 'x')
    png = builders.png(tmp_path / 'p.png')
    ok = _post(client, '/convert/run', target='.txt', language='eng+spa',
               file=(io.BytesIO(png.read_bytes()), 'p.png'))
    assert ok.status_code == 200, ok.get_json()
    assert seen == ['eng+spa']
    too_many = _post(client, '/convert/run', target='.txt', language='eng+spa+fra',
                     file=(io.BytesIO(png.read_bytes()), 'p.png'))
    assert too_many.status_code == 400
    assert too_many.get_json()['error'] == 'Choose at most 2 OCR languages.'


def test_convert_page_has_paper_and_language(client, monkeypatch):
    from routes import convert as convert_routes
    monkeypatch.setattr(convert_routes, 'installed_languages', lambda: ['eng', 'fil'])
    html = client.get('/convert').get_data(as_text=True)
    assert 'id="paperSelect"' in html
    assert '<option value="fil">Filipino</option>' in html
    assert '/static/paper.js' in html and '/static/ocr-langs.js' in html


# --------------------------------------------------------------------- print
@pytest.mark.parametrize('operation, n, paper, expected', [
    ('nup', '2', 'a4', (841.89, 595.28)),
    ('nup', '4', 'a4', A4),
    ('booklet', '', 'a4', (841.89, 595.28)),
    ('nup', '2', None, (792.0, 612.0)),
])
def test_print_sheet_follows_paper(client, tmp_path, builders, operation, n, paper, expected):
    pdf = builders.pdf(tmp_path / 'p.pdf', pages=4)
    data = {'operation': operation, 'n': n, 'file': (io.BytesIO(pdf.read_bytes()), 'p.pdf')}
    if paper:
        data['paper'] = paper
    resp = _post(client, '/print/run', **data)
    assert resp.status_code == 200, resp.get_json()
    assert _size(client.output_dir / resp.get_json()['filename']) == expected


def test_print_bad_paper_is_400(client, tmp_path, builders):
    pdf = builders.pdf(tmp_path / 'p.pdf', pages=2)
    resp = _post(client, '/print/run', operation='nup', n='2', paper='legal',
                 file=(io.BytesIO(pdf.read_bytes()), 'p.pdf'))
    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'Paper must be letter or a4.'


@pytest.mark.parametrize('url', ['/', '/print'])
def test_paper_select_on_page(client, url):
    html = client.get(url).get_data(as_text=True)
    assert 'id="paperSelect"' in html and 'class="paper-select"' in html
    assert '<option value="a4">A4</option>' in html
    assert '/static/paper.js' in html


@pytest.mark.parametrize('url', ['/pages', '/export', '/security'])
def test_no_paper_select_on_pdf_only_pages(client, url):
    assert 'paperSelect' not in client.get(url).get_data(as_text=True)
