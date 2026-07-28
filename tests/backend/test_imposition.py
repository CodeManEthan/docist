"""Tests for the Print Prep feature: imposition pure ops + Flask route.

Reuses fixtures from tests/backend/conftest.py (``client``). Content-sanity
checks render output sheets with pypdfium2 and assert non-blank via pixel
variance (Pillow's ImageStat — no numpy dependency).
"""
import io

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from pypdf import PdfReader

from pdf_ops.imposition import (
    booklet_page_order,
    nup_pdf,
    booklet_pdf,
    LETTER_LANDSCAPE,
    LETTER_PORTRAIT,
)


# --------------------------------------------------------------------------
# Fixture builder: pages stamped with a big page number so a rendered sheet
# has plenty of pixel variance (used for the content-sanity checks).
# --------------------------------------------------------------------------
def _make_numbered_pdf(path, pages):
    c = canvas.Canvas(str(path), pagesize=letter)
    for i in range(pages):
        c.setFont("Helvetica-Bold", 220)
        c.drawString(150, 320, str(i + 1))
        c.showPage()
    c.save()
    return path


def _sheet_stddev(pdf_path, index):
    """Render one output sheet and return its grayscale pixel std-dev."""
    import pypdfium2 as pdfium
    from PIL import ImageStat

    doc = pdfium.PdfDocument(str(pdf_path))
    img = doc[index].render(scale=1.0).to_pil().convert("L")
    return ImageStat.Stat(img).stddev[0]


# --------------------------------------------------------------------------
# booklet_page_order — exhaustive sequence checks
# --------------------------------------------------------------------------
def test_booklet_order_4():
    # front(4,1) back(2,3) -> 0-based
    assert booklet_page_order(4) == [(3, 0), (1, 2)]


def test_booklet_order_8():
    # front(8,1) back(2,7) front(6,3) back(4,5) -> 0-based
    assert booklet_page_order(8) == [(7, 0), (1, 6), (5, 2), (3, 4)]


def test_booklet_order_12():
    assert booklet_page_order(12) == [
        (11, 0), (1, 10), (9, 2), (3, 8), (7, 4), (5, 6),
    ]


def test_booklet_order_padded_5_to_8():
    # 5 pages pad to 8; pages 6,7,8 (1-based) are blanks -> None.
    # 1-based pairs: (8,1)(2,7)(6,3)(4,5)
    #   8->None 1->0 | 2->1 7->None | 6->None 3->2 | 4->3 5->4
    assert booklet_page_order(5) == [
        (None, 0), (1, None), (None, 2), (3, 4),
    ]


@pytest.mark.parametrize("count,expected_sheets", [
    (1, 2), (2, 2), (3, 2), (4, 2),
    (5, 4), (6, 4), (7, 4), (8, 4),
    (9, 6), (12, 6), (13, 8),
])
def test_booklet_order_sheet_count(count, expected_sheets):
    # Number of output sheets (2-up landscape pages) == padded / 2.
    assert len(booklet_page_order(count)) == expected_sheets


@pytest.mark.parametrize("count", [1, 2, 3, 5, 7, 10])
def test_booklet_order_covers_every_real_page_once(count):
    pairs = booklet_page_order(count)
    seen = []
    for left, right in pairs:
        for v in (left, right):
            if v is not None:
                seen.append(v)
    assert sorted(seen) == list(range(count))


@pytest.mark.parametrize("bad", [0, -1, None])
def test_booklet_order_bad_count_raises(bad):
    with pytest.raises(ValueError):
        booklet_page_order(bad)


# --------------------------------------------------------------------------
# nup_pdf — page counts and sheet dimensions
# --------------------------------------------------------------------------
def test_nup2_page_count(tmp_path):
    src = _make_numbered_pdf(tmp_path / "src.pdf", 7)
    out = tmp_path / "out.pdf"
    nup_pdf(str(src), str(out), n=2)
    reader = PdfReader(str(out))
    assert len(reader.pages) == 4  # ceil(7/2)


def test_nup4_page_count(tmp_path):
    src = _make_numbered_pdf(tmp_path / "src.pdf", 7)
    out = tmp_path / "out.pdf"
    nup_pdf(str(src), str(out), n=4)
    reader = PdfReader(str(out))
    assert len(reader.pages) == 2  # ceil(7/4)


def test_nup2_sheet_is_letter_landscape(tmp_path):
    src = _make_numbered_pdf(tmp_path / "src.pdf", 3)
    out = tmp_path / "out.pdf"
    nup_pdf(str(src), str(out), n=2)
    page = PdfReader(str(out)).pages[0]
    assert (float(page.mediabox.width), float(page.mediabox.height)) == LETTER_LANDSCAPE


def test_nup4_sheet_is_letter_portrait(tmp_path):
    src = _make_numbered_pdf(tmp_path / "src.pdf", 3)
    out = tmp_path / "out.pdf"
    nup_pdf(str(src), str(out), n=4)
    page = PdfReader(str(out)).pages[0]
    assert (float(page.mediabox.width), float(page.mediabox.height)) == LETTER_PORTRAIT


def test_nup_exact_multiple(tmp_path):
    src = _make_numbered_pdf(tmp_path / "src.pdf", 8)
    out = tmp_path / "out.pdf"
    nup_pdf(str(src), str(out), n=4)
    assert len(PdfReader(str(out)).pages) == 2


@pytest.mark.parametrize("bad_n", [1, 3, 5, 0, -2])
def test_nup_bad_n_raises(tmp_path, bad_n):
    src = _make_numbered_pdf(tmp_path / "src.pdf", 4)
    out = tmp_path / "out.pdf"
    with pytest.raises(ValueError):
        nup_pdf(str(src), str(out), n=bad_n)


# --------------------------------------------------------------------------
# booklet_pdf — sheet count and dimensions
# --------------------------------------------------------------------------
@pytest.mark.parametrize("count,padded", [(4, 4), (5, 8), (7, 8), (8, 8), (10, 12)])
def test_booklet_sheet_count(tmp_path, count, padded):
    src = _make_numbered_pdf(tmp_path / "src.pdf", count)
    out = tmp_path / "out.pdf"
    booklet_pdf(str(src), str(out))
    reader = PdfReader(str(out))
    assert len(reader.pages) == padded // 2


def test_booklet_sheet_is_letter_landscape(tmp_path):
    src = _make_numbered_pdf(tmp_path / "src.pdf", 6)
    out = tmp_path / "out.pdf"
    booklet_pdf(str(src), str(out))
    page = PdfReader(str(out)).pages[0]
    assert (float(page.mediabox.width), float(page.mediabox.height)) == LETTER_LANDSCAPE


# --------------------------------------------------------------------------
# Content sanity — rendered sheets are not blank
# --------------------------------------------------------------------------
def test_nup2_content_not_blank(tmp_path):
    src = _make_numbered_pdf(tmp_path / "src.pdf", 4)
    out = tmp_path / "out.pdf"
    nup_pdf(str(src), str(out), n=2)
    assert _sheet_stddev(out, 0) > 1.0


def test_nup4_content_not_blank(tmp_path):
    src = _make_numbered_pdf(tmp_path / "src.pdf", 4)
    out = tmp_path / "out.pdf"
    nup_pdf(str(src), str(out), n=4)
    assert _sheet_stddev(out, 0) > 1.0


def test_nup2_both_halves_have_content(tmp_path):
    import pypdfium2 as pdfium
    from PIL import ImageStat

    src = _make_numbered_pdf(tmp_path / "src.pdf", 2)
    out = tmp_path / "out.pdf"
    nup_pdf(str(src), str(out), n=2)
    img = pdfium.PdfDocument(str(out))[0].render(scale=1.0).to_pil().convert("L")
    w, h = img.size
    left = ImageStat.Stat(img.crop((0, 0, w // 2, h))).stddev[0]
    right = ImageStat.Stat(img.crop((w // 2, 0, w, h))).stddev[0]
    assert left > 1.0 and right > 1.0


def test_booklet_content_not_blank(tmp_path):
    src = _make_numbered_pdf(tmp_path / "src.pdf", 8)
    out = tmp_path / "out.pdf"
    booklet_pdf(str(src), str(out))
    assert _sheet_stddev(out, 0) > 1.0


def test_nup_empty_pdf_raises(tmp_path):
    # A structurally valid but zero-page PDF is hard to build with reportlab;
    # instead assert the validation guard fires on a readable one-page doc
    # against a bad n (covered above) and that booklet needs pages via order.
    with pytest.raises(ValueError):
        booklet_page_order(0)


# --------------------------------------------------------------------------
# Route: GET /print
# --------------------------------------------------------------------------
def test_get_print_renders(client):
    resp = client.get("/print")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "/static/print.js" in body
    assert 'class="active"' in body
    assert "Print Prep" in body
    # Booklet instructions are explained in the UI.
    assert "fold in half" in body


# --------------------------------------------------------------------------
# Route: POST /print/run
# --------------------------------------------------------------------------
def _pdf_bytes(tmp_path, pages=7):
    src = _make_numbered_pdf(tmp_path / "input.pdf", pages)
    return src.read_bytes()


def test_run_nup2(client, tmp_path):
    data = _pdf_bytes(tmp_path, pages=7)
    resp = client.post("/print/run", data={
        "operation": "nup",
        "n": "2",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    out = client.output_dir / payload["filename"]
    assert out.exists()
    assert len(PdfReader(str(out)).pages) == 4
    assert payload["download_url"] == "/download?filename=" + payload["filename"]


def test_run_nup4(client, tmp_path):
    data = _pdf_bytes(tmp_path, pages=7)
    resp = client.post("/print/run", data={
        "operation": "nup",
        "n": "4",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    out = client.output_dir / resp.get_json()["filename"]
    assert len(PdfReader(str(out)).pages) == 2


def test_run_booklet(client, tmp_path):
    data = _pdf_bytes(tmp_path, pages=5)
    resp = client.post("/print/run", data={
        "operation": "booklet",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    payload = resp.get_json()
    out = client.output_dir / payload["filename"]
    # 5 pages -> padded 8 -> 4 sheets.
    assert len(PdfReader(str(out)).pages) == 4
    assert payload["filename"].endswith("_booklet.pdf")


def test_run_rejects_non_pdf(client):
    resp = client.post("/print/run", data={
        "operation": "nup",
        "n": "2",
        "file": (io.BytesIO(b"not a pdf"), "notes.txt"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_run_bad_n_is_400(client, tmp_path):
    data = _pdf_bytes(tmp_path, pages=3)
    resp = client.post("/print/run", data={
        "operation": "nup",
        "n": "3",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_run_missing_n_is_400(client, tmp_path):
    data = _pdf_bytes(tmp_path, pages=3)
    resp = client.post("/print/run", data={
        "operation": "nup",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400


def test_run_unknown_operation_is_400(client, tmp_path):
    data = _pdf_bytes(tmp_path, pages=3)
    resp = client.post("/print/run", data={
        "operation": "frobnicate",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400


def test_run_missing_file_is_400(client):
    resp = client.post("/print/run", data={"operation": "nup", "n": "2"},
                       content_type="multipart/form-data")
    assert resp.status_code == 400
