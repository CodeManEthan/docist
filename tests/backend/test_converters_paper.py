"""Paper choice in every converter (round prelaunch-fixes, design §3.2).

Each converter that decides its page size lays out on Letter with no options
and on A4 with ``paper='a4'``. HTML that sets its own page size keeps it;
detection reads the CSS by structure, at any nesting depth.
"""
import pytest
from pypdf import PdfReader

from converters import get_converter
from converters.html_converter import css_sets_page_size, document_sets_page_size
from converters.options import RenderOptions

LETTER = (612.0, 792.0)
A4 = (595.28, 841.89)
A5 = (419.53, 595.28)
TOL = 0.5


def _first_page_size(path):
    box = PdfReader(str(path)).pages[0].mediabox
    return float(box.width), float(box.height)


def _assert_size(path, expected, tol=TOL):
    w, h = _first_page_size(path)
    assert abs(w - expected[0]) <= tol and abs(h - expected[1]) <= tol, (w, h, expected)


def _rtf(path):
    path.write_text(r'{\rtf1\ansi Hello {\b RTF} world.\par}', encoding='latin-1')
    return path


def _svg(path):
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
        '<rect width="200" height="100" fill="#369"/></svg>', encoding='utf-8')
    return path


def _csv(path):
    path.write_text('name,value\nalpha,1\nbeta,2\n', encoding='utf-8')
    return path


def _csv_wide(path):
    header = ','.join(f'c{i}' for i in range(12))
    path.write_text(header + '\n' + ','.join('x' * 12) + '\n', encoding='utf-8')
    return path


def _xlsx(path):
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.append(['name', 'value'])
    wb.active.append(['alpha', 1])
    wb.save(path)
    return path


def _heic(path):
    import pillow_heif
    from PIL import Image
    pillow_heif.register_heif_opener()
    try:
        Image.new('RGB', (200, 120), (120, 20, 200)).save(path, format='HEIF')
    except Exception as exc:  # pragma: no cover - encoder missing
        pytest.skip(f'cannot write HEIF here: {exc}')
    return path


def _make(kind, tmp_path, builders):
    return {
        'md': lambda: builders.markdown(tmp_path / 'doc.md'),
        'docx': lambda: builders.docx(tmp_path / 'doc.docx'),
        'html': lambda: builders.html(tmp_path / 'doc.html'),
        'txt': lambda: builders.text_rich(tmp_path / 'doc.txt'),
        'rtf': lambda: _rtf(tmp_path / 'doc.rtf'),
        'svg': lambda: _svg(tmp_path / 'doc.svg'),
        'png': lambda: builders.png(tmp_path / 'doc.png'),
        'heic': lambda: _heic(tmp_path / 'doc.heic'),
        'csv': lambda: _csv(tmp_path / 'doc.csv'),
        'xlsx': lambda: _xlsx(tmp_path / 'doc.xlsx'),
    }[kind]()


KINDS = ['md', 'docx', 'html', 'txt', 'rtf', 'svg', 'png', 'heic', 'csv', 'xlsx']


@pytest.mark.parametrize('kind', KINDS)
def test_no_options_is_letter(kind, tmp_path, builders):
    src = _make(kind, tmp_path, builders)
    out = tmp_path / 'out.pdf'
    get_converter('.' + kind)(str(src), str(out))
    _assert_size(out, LETTER)


@pytest.mark.parametrize('kind', KINDS)
def test_letter_option_is_letter(kind, tmp_path, builders):
    src = _make(kind, tmp_path, builders)
    out = tmp_path / 'out.pdf'
    get_converter('.' + kind)(str(src), str(out), RenderOptions(paper='letter'))
    _assert_size(out, LETTER)


@pytest.mark.parametrize('kind', KINDS)
def test_a4_option_is_a4(kind, tmp_path, builders):
    src = _make(kind, tmp_path, builders)
    out = tmp_path / 'out.pdf'
    get_converter('.' + kind)(str(src), str(out), RenderOptions(paper='a4'))
    _assert_size(out, A4)


def test_wide_spreadsheet_turns_the_chosen_paper(tmp_path):
    src = _csv_wide(tmp_path / 'wide.csv')
    out = tmp_path / 'out.pdf'
    get_converter('.csv')(str(src), str(out), RenderOptions(paper='a4'))
    _assert_size(out, (A4[1], A4[0]))
    get_converter('.csv')(str(src), str(out))
    _assert_size(out, (LETTER[1], LETTER[0]))


# --------------------------------------------------------------------- HTML
def _html(tmp_path, style, body='<h1>Heading</h1><p>body text</p>'):
    path = tmp_path / 'page.html'
    path.write_text(
        f'<html><head><style>{style}</style></head><body>{body}</body></html>',
        encoding='utf-8')
    return path


def _render_html(tmp_path, style, paper):
    src = _html(tmp_path, style)
    out = tmp_path / f'out-{paper}.pdf'
    get_converter('.html')(str(src), str(out), RenderOptions(paper=paper))
    return out


@pytest.mark.parametrize('paper, expected', [('letter', LETTER), ('a4', A4)])
def test_html_without_page_rule_follows_paper(tmp_path, paper, expected):
    _assert_size(_render_html(tmp_path, 'p { color: #333 }', paper), expected)


@pytest.mark.parametrize('style', [
    '@page { size: A5 }',
    '@media print { @page { size: A5 } }',
    '@page :first { size: A5 }',
    '@page { @frame body { left: 1cm; width: 10cm; top: 1cm; height: 15cm; } size: A5 }',
])
@pytest.mark.parametrize('paper', ['letter', 'a4'])
def test_html_with_own_size_keeps_it(tmp_path, style, paper):
    _assert_size(_render_html(tmp_path, style, paper), A5)


@pytest.mark.parametrize('style', [
    '@page { font-size: 12pt }',
    '/* @page { size: A5 } */',
    '@page { margin: 1cm }',
])
@pytest.mark.parametrize('paper, expected', [('letter', LETTER), ('a4', A4)])
def test_html_page_rule_without_size_follows_paper(tmp_path, style, paper, expected):
    _assert_size(_render_html(tmp_path, style, paper), expected)


def test_html_headings_keep_pisa_default_styling(tmp_path):
    """The paper rule is added to pisa's default CSS, never in place of it."""
    src = tmp_path / 'h.html'
    src.write_text('<html><body><h1>BigHeading</h1><p>smallbody</p></body></html>',
                   encoding='utf-8')
    out = tmp_path / 'h.pdf'
    get_converter('.html')(str(src), str(out), RenderOptions(paper='a4'))
    sizes = {}

    def visit(text, cm, tm, _font, font_size):
        if text.strip():
            sizes[text.strip()] = font_size * tm[3] * cm[3]

    PdfReader(str(out)).pages[0].extract_text(visitor_text=visit)
    assert sizes['BigHeading'] > sizes['smallbody']


@pytest.mark.parametrize('css, expected', [
    ('@page { size: A5 }', True),
    ('@page{SIZE:a5}', True),
    ('@page :first { size: A5 }', True),
    ('@media print { @page { size: A5 } }', True),
    ('@supports (display: grid) { @media print { @page { size: A5 } } }', True),
    ('@page { @frame x { left: 1in; } size: A5 }', True),
    ('@page { font-size: 12pt }', False),
    ('@page { margin: 1in }', False),
    ('@page { @frame x { size: 1in; } }', False),
    ('body { size: A5 }', False),
    ('p { content: "@page { size: a5 }" }', False),
    ('', False),
])
def test_css_detection(css, expected):
    assert css_sets_page_size(css) is expected


def test_commented_rule_is_not_detected():
    assert not document_sets_page_size('<style>/* @page { size: A5 } */</style>')
    assert document_sets_page_size('<STYLE type="text/css">@page{size:a5}</STYLE>')
