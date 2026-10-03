"""Tests for the spreadsheet (.csv/.xlsx) and RTF (.rtf) converter plugins.

All fixtures are built programmatically in ``tmp_path`` -- no binaries are
committed. Valid outputs are checked with pypdf (page count + extracted text
markers); error cases assert ``ConversionError``; the registry is checked for
the new extensions; and one route-level check uploads a CSV through /upload.
"""
import io

import openpyxl
import pytest
from pypdf import PdfReader

import converters
from converters import (
    ConversionError,
    get_converter,
    spreadsheet_converter,
    supported_extensions,
)


# --------------------------------------------------------------------------
# Markers embedded in fixtures so extracted text can be spot-checked.
# --------------------------------------------------------------------------
CSV_QUOTED = "Paris, FR"          # a quoted field containing a comma
CSV_UNICODE = "café"
CSV_SEMI = "SemiColMarker"

XLSX_SHEET_A = "AlphaSheet"
XLSX_SHEET_B = "BetaSheet"
XLSX_CELL_A = "CellAlphaXi"
XLSX_CELL_B = "CellBetaOmega"
XLSX_WIDE_SHEET = "WideSheet"

RTF_TEXT = "RtfBodyMarkerZeta"


# --------------------------------------------------------------------------
# Programmatic fixture builders
# --------------------------------------------------------------------------
def _build_csv(path):
    """Comma-delimited CSV with a quoted comma-bearing field + unicode."""
    path.write_text(
        "Name,City,Note\n"
        f'Alice,"{CSV_QUOTED}",{CSV_UNICODE}\n'
        "Bob,London,plain\n",
        encoding="utf-8",
    )
    return path


def _build_csv_semicolon(path):
    """Semicolon-delimited CSV (delimiter must be sniffed, not assumed)."""
    path.write_text(
        "Name;City;Tag\n"
        f"Zoe;Berlin;{CSV_SEMI}\n"
        "Yan;Oslo;other\n",
        encoding="utf-8",
    )
    return path


def _build_xlsx(path):
    """Workbook with 2 populated sheets, a wide sheet, and an empty sheet."""
    wb = openpyxl.Workbook()
    a = wb.active
    a.title = XLSX_SHEET_A
    a.append(["Head1", "Head2"])
    a.append([XLSX_CELL_A, 123])

    b = wb.create_sheet(XLSX_SHEET_B)
    b.append(["ColX", "ColY"])
    b.append([XLSX_CELL_B, 4.5])

    wide = wb.create_sheet(XLSX_WIDE_SHEET)
    wide.append([f"C{i}" for i in range(14)])
    wide.append([f"v{i}" for i in range(14)])

    wb.create_sheet("EmptyTab")  # left empty on purpose
    wb.save(path)
    return path


def _build_wide_xlsx(path):
    """A single very wide sheet (forces landscape + column capping)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "OnlyWide"
    ws.append([f"Column_{i:02d}" for i in range(20)])
    ws.append(["x" * 300] + [f"val{i}" for i in range(19)])  # extreme cell
    wb.save(path)
    return path


def _build_rtf(path):
    r"""A simple RTF file with bold formatting (formatting is dropped)."""
    path.write_text(
        r"{\rtf1\ansi\deff0 {\b Bold heading}\par "
        + RTF_TEXT
        + r" and some more text.\par}",
        encoding="utf-8",
    )
    return path


def _extract(pdf_path):
    reader = PdfReader(str(pdf_path))
    return len(reader.pages), "\n".join(p.extract_text() or "" for p in reader.pages)


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------
def test_csv_converts_to_valid_pdf_with_content(tmp_path):
    src = _build_csv(tmp_path / "in.csv")
    out = tmp_path / "out.pdf"
    get_converter(".csv")(str(src), str(out))
    pages, text = _extract(out)
    assert pages >= 1
    # Quoted comma-field kept intact, and unicode survived.
    assert "Paris" in text
    assert CSV_UNICODE in text
    assert "Alice" in text and "Bob" in text


def test_csv_semicolon_delimiter_is_sniffed(tmp_path):
    src = _build_csv_semicolon(tmp_path / "semi.csv")
    out = tmp_path / "semi.pdf"
    spreadsheet_converter.convert(str(src), str(out))
    pages, text = _extract(out)
    assert pages >= 1
    assert CSV_SEMI in text
    assert "Berlin" in text


def test_csv_latin1_fallback(tmp_path):
    src = tmp_path / "latin.csv"
    # 0xE9 is 'é' in latin-1 but invalid standalone UTF-8.
    src.write_bytes(b"Name,Note\nJose,caf\xe9 latin\n")
    out = tmp_path / "latin.pdf"
    spreadsheet_converter.convert(str(src), str(out))
    pages, _ = _extract(out)
    assert pages >= 1


def test_empty_csv_yields_one_page_pdf(tmp_path):
    src = tmp_path / "empty.csv"
    src.write_text("", encoding="utf-8")
    out = tmp_path / "empty.pdf"
    spreadsheet_converter.convert(str(src), str(out))
    pages, text = _extract(out)
    assert pages == 1
    assert "empty" in text.lower()


# --------------------------------------------------------------------------
# XLSX
# --------------------------------------------------------------------------
def test_xlsx_multi_sheet_headings_and_cells(tmp_path):
    src = _build_xlsx(tmp_path / "book.xlsx")
    out = tmp_path / "book.pdf"
    get_converter(".xlsx")(str(src), str(out))
    pages, text = _extract(out)
    assert pages >= 1
    # Both sheet names appear as headings, their cells are present.
    assert XLSX_SHEET_A in text
    assert XLSX_SHEET_B in text
    assert XLSX_CELL_A in text
    assert XLSX_CELL_B in text
    # The empty sheet is skipped.
    assert "EmptyTab" not in text


def test_xlsx_wide_table_does_not_error(tmp_path):
    src = _build_wide_xlsx(tmp_path / "wide.xlsx")
    out = tmp_path / "wide.pdf"
    spreadsheet_converter.convert(str(src), str(out))
    pages, text = _extract(out)
    assert pages >= 1
    # A non-extreme column value still shows up.
    assert "val5" in text


def test_corrupt_xlsx_raises(tmp_path):
    src = tmp_path / "broken.xlsx"
    src.write_bytes(b"this is definitely not a real xlsx zip payload")
    out = tmp_path / "broken.pdf"
    with pytest.raises(ConversionError):
        spreadsheet_converter.convert(str(src), str(out))


# --------------------------------------------------------------------------
# RTF
# --------------------------------------------------------------------------
def test_rtf_converts_and_extracts_text(tmp_path):
    src = _build_rtf(tmp_path / "in.rtf")
    out = tmp_path / "out.pdf"
    get_converter(".rtf")(str(src), str(out))
    pages, text = _extract(out)
    assert pages >= 1
    assert RTF_TEXT in text
    assert "Bold heading" in text  # text kept even though bold is dropped


def test_non_rtf_file_raises(tmp_path):
    src = tmp_path / "fake.rtf"
    src.write_text("this is just plain text, not rtf at all", encoding="utf-8")
    out = tmp_path / "fake.pdf"
    with pytest.raises(ConversionError):
        get_converter(".rtf")(str(src), str(out))


def test_garbage_rtf_bytes_raise(tmp_path):
    src = tmp_path / "garbage.rtf"
    src.write_bytes(b"\x00\x01\x02 binary noise " * 20)
    out = tmp_path / "garbage.pdf"
    with pytest.raises(ConversionError):
        spreadsheet_converter  # noqa: F841 - keep import used
        get_converter(".rtf")(str(src), str(out))


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
def test_registry_includes_new_extensions():
    exts = supported_extensions()
    for ext in (".csv", ".xlsx", ".rtf"):
        assert ext in exts
        assert get_converter(ext) is not None


# --------------------------------------------------------------------------
# Route-level check: upload a CSV through POST /upload
# --------------------------------------------------------------------------
def test_upload_csv_merges_successfully(client):
    csv_bytes = (
        "Name,City,Note\n"
        f'Alice,"{CSV_QUOTED}",{CSV_UNICODE}\n'
        "Bob,London,plain\n"
    ).encode("utf-8")

    data = {"files[]": [(io.BytesIO(csv_bytes), "data.csv")]}
    resp = client.post("/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200, resp.data
    payload = resp.get_json()
    assert payload["success"] is True

    filename = payload["filename"]
    dl = client.get(f"/download?filename={filename}")
    assert dl.status_code == 200, dl.data
    reader = PdfReader(io.BytesIO(dl.data))
    # A single short CSV -> 1 content page, padded to an even count (2) by
    # the app's blank-page logic. Sanity-check it produced pages.
    assert len(reader.pages) >= 1


# --------------------------------------------------------------------------
# The Word engine through the converter and the API (prelaunch-fixes B2, §5.4)
# --------------------------------------------------------------------------
import word_fixtures as wf  # noqa: E402
from converters import docx_converter  # noqa: E402
from converters.options import RenderOptions  # noqa: E402
from pdf_ops import office  # noqa: E402

_A4 = (595.3, 841.9)
_LETTER = (612.0, 792.0)


def _size(path):
    box = PdfReader(str(path)).pages[0].mediabox
    return float(box.width), float(box.height)


def _near(a, b, tol=1.0):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


@pytest.mark.skipif(not office.available(), reason='LibreOffice not installed')
def test_word_engine_keeps_the_documents_own_page_size(tmp_path):
    src = wf.a4_header_footer(tmp_path / 'a4.docx')
    out = tmp_path / 'out.pdf'
    opts = RenderOptions(paper='letter', word_engine='libreoffice')
    get_converter('.docx')(str(src), str(out), opts)
    assert _near(_size(out), _A4)
    assert opts.notes == []


def test_reflow_follows_the_paper(tmp_path):
    src = wf.a4_header_footer(tmp_path / 'a4.docx')
    out = tmp_path / 'out.pdf'
    get_converter('.docx')(str(src), str(out), RenderOptions(paper='letter'))
    assert _near(_size(out), _LETTER)


def test_office_error_falls_back_to_reflow_with_the_note(tmp_path, monkeypatch):
    def fail(*_a, **_k):
        raise office.OfficeError('boom')
    monkeypatch.setattr(office, 'docx_to_pdf', fail)
    src = wf.a4_header_footer(tmp_path / 'a4.docx')
    out = tmp_path / 'out.pdf'
    opts = RenderOptions(paper='a4', word_engine='libreoffice')
    get_converter('.docx')(str(src), str(out), opts)
    assert _near(_size(out), _A4)
    assert opts.notes == [docx_converter.FALLBACK_NOTE]
    # A second file in the same request doesn't repeat the note.
    get_converter('.docx')(str(src), str(tmp_path / 'two.pdf'), opts)
    assert opts.notes == [docx_converter.FALLBACK_NOTE]


def test_reflow_engine_never_calls_libreoffice(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError('LibreOffice was called')
    monkeypatch.setattr(office, 'docx_to_pdf', boom)
    out = tmp_path / 'out.pdf'
    get_converter('.docx')(str(wf.letter_1page(tmp_path / 'l.docx')), str(out),
                           RenderOptions())
    assert PdfReader(str(out)).pages


def _paid_key(make_user):
    import app as flask_app_module
    from models import ApiKey, User, db
    user = make_user(email='paid@x.io', plan='monthly')
    with flask_app_module.app.app_context():
        raw, _ = ApiKey.issue(db.session.get(User, user.id), 'test')
        db.session.commit()
    return raw


def test_api_convert_sends_the_note_header(client, make_user, tmp_path, monkeypatch):
    monkeypatch.setattr(office, 'available', lambda: True)

    def fail(*_a, **_k):
        raise office.OfficeError('boom')
    monkeypatch.setattr(office, 'docx_to_pdf', fail)
    raw = _paid_key(make_user)
    src = wf.letter_1page(tmp_path / 'l.docx')
    response = client.post('/api/v1/convert', data={
        'file': (open(src, 'rb'), 'l.docx'), 'target': '.pdf',
    }, content_type='multipart/form-data', headers={'Authorization': f'Bearer {raw}'})
    assert response.status_code == 200
    assert response.headers['X-Docist-Notes'] == docx_converter.FALLBACK_NOTE
    assert response.data.startswith(b'%PDF-')


@pytest.mark.skipif(not office.available(), reason='LibreOffice not installed')
def test_api_convert_paid_key_gets_the_engine(client, make_user, tmp_path):
    raw = _paid_key(make_user)
    src = wf.a4_header_footer(tmp_path / 'a4.docx')
    response = client.post('/api/v1/convert', data={
        'file': (open(src, 'rb'), 'a4.docx'), 'target': '.pdf', 'paper': 'letter',
    }, content_type='multipart/form-data', headers={'Authorization': f'Bearer {raw}'})
    assert response.status_code == 200
    assert 'X-Docist-Notes' not in response.headers
    reader = PdfReader(io.BytesIO(response.data))
    box = reader.pages[0].mediabox
    assert _near((float(box.width), float(box.height)), _A4)


def test_free_user_merge_reflows_without_a_note(client, make_user, login, tmp_path,
                                                monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError('LibreOffice was called for a free user')
    monkeypatch.setattr(office, 'docx_to_pdf', boom)
    monkeypatch.setattr(office, 'available', lambda: True)
    login(client, make_user(plan='free'))
    src = wf.a4_header_footer(tmp_path / 'a4.docx')
    response = client.post('/upload', data={'files[]': (open(src, 'rb'), 'a4.docx')},
                           content_type='multipart/form-data')
    assert response.status_code == 200
    assert docx_converter.FALLBACK_NOTE not in response.get_json()['message']
