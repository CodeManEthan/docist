"""Tests for header/footer text and Bates numbering.

Covers the pdf_ops.stamp units (slot placement, {page}/{pages} substitution,
Bates formatting/increment, validation, return value) plus the two new
/security/run route operations, including 400s and the Bates range message.

Reuses conftest.py's ``client`` fixture and ``build_pdf`` helper.
"""
import io

import pytest
from PyPDF2 import PdfReader

from pdf_ops.stamp import (
    apply_header_footer,
    apply_bates_numbers,
    format_bates,
)

from conftest import build_pdf, extract_all_text


def _run(client, filename, content, form):
    data = {"file": (io.BytesIO(content), filename)}
    data.update(form)
    return client.post("/security/run", data=data,
                       content_type="multipart/form-data")


def _download_bytes(client, filename):
    resp = client.get(f"/download?filename={filename}")
    assert resp.status_code == 200, resp.data
    return resp.data


# ==========================================================================
# Header / footer — pdf_ops unit
# ==========================================================================
def test_headerfooter_slots_land_on_page(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    out = tmp_path / "out.pdf"
    apply_header_footer(
        str(src), str(out),
        header_left="HeadLeftMark",
        header_center="HeadCenterMark",
        header_right="HeadRightMark",
        footer_left="FootLeftMark",
        footer_center="FootCenterMark",
        footer_right="FootRightMark",
    )
    text = extract_all_text(out)
    for mark in ("HeadLeftMark", "HeadCenterMark", "HeadRightMark",
                 "FootLeftMark", "FootCenterMark", "FootRightMark"):
        assert mark in text


def test_headerfooter_subset_of_slots(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    out = tmp_path / "out.pdf"
    apply_header_footer(str(src), str(out), footer_center="OnlyFooterMark")
    text = extract_all_text(out)
    assert "OnlyFooterMark" in text


def test_headerfooter_regions_top_vs_bottom(tmp_path):
    """Header text is drawn near the top; footer near the bottom of the page."""
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    out = tmp_path / "out.pdf"
    apply_header_footer(str(src), str(out),
                        header_center="TopRegionMark",
                        footer_center="BottomRegionMark")
    reader = PdfReader(str(out))
    page = reader.pages[0]
    height = float(page.mediabox.height)

    positions = {}
    def visitor(text, cm, tm, font_dict, font_size):
        stripped = text.strip()
        if stripped in ("TopRegionMark", "BottomRegionMark"):
            positions[stripped] = tm[5]  # y coordinate
    page.extract_text(visitor_text=visitor)

    assert positions["TopRegionMark"] > height / 2
    assert positions["BottomRegionMark"] < height / 2


def test_headerfooter_page_placeholder_substituted(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=3)
    out = tmp_path / "out.pdf"
    apply_header_footer(str(src), str(out),
                        footer_center="Page {page} of {pages}")
    reader = PdfReader(str(out))
    texts = [p.extract_text() or "" for p in reader.pages]
    assert "Page 1 of 3" in texts[0]
    assert "Page 2 of 3" in texts[1]
    assert "Page 3 of 3" in texts[2]


def test_headerfooter_all_empty_raises(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    with pytest.raises(ValueError):
        apply_header_footer(str(src), str(tmp_path / "o.pdf"))


def test_headerfooter_whitespace_only_raises(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    with pytest.raises(ValueError):
        apply_header_footer(str(src), str(tmp_path / "o.pdf"),
                            header_left="   ", footer_right="")


def test_headerfooter_bad_color_raises(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    with pytest.raises(ValueError):
        apply_header_footer(str(src), str(tmp_path / "o.pdf"),
                            header_left="x", color="notacolor")


def test_headerfooter_preserves_page_count(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=4)
    out = tmp_path / "out.pdf"
    apply_header_footer(str(src), str(out), header_center="Mark")
    assert len(PdfReader(str(out)).pages) == 4


# ==========================================================================
# Bates numbering — pdf_ops unit
# ==========================================================================
def test_format_bates_padding_and_prefix():
    assert format_bates("ACME", 1, 6) == "ACME000001"
    assert format_bates("", 42, 6) == "000042"
    assert format_bates("X", 5, 3) == "X005"


def test_bates_stamps_prefix_and_padding(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    out = tmp_path / "out.pdf"
    apply_bates_numbers(str(src), str(out), prefix="ACME", start=1, digits=6)
    text = extract_all_text(out)
    assert "ACME000001" in text


def test_bates_increments_per_page(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=3)
    out = tmp_path / "out.pdf"
    apply_bates_numbers(str(src), str(out), prefix="ACME", start=1, digits=6)
    reader = PdfReader(str(out))
    texts = [p.extract_text() or "" for p in reader.pages]
    assert "ACME000001" in texts[0]
    assert "ACME000002" in texts[1]
    assert "ACME000003" in texts[2]


def test_bates_returns_last_label(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=5)
    out = tmp_path / "out.pdf"
    last = apply_bates_numbers(str(src), str(out), prefix="ACME",
                               start=1, digits=6)
    assert last == "ACME000005"


def test_bates_returns_last_label_custom_start(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=3)
    out = tmp_path / "out.pdf"
    last = apply_bates_numbers(str(src), str(out), prefix="P",
                               start=100, digits=4)
    assert last == "P0102"


@pytest.mark.parametrize("position",
                         ["bottom-right", "bottom-left", "top-right", "top-left"])
def test_bates_positions_all_valid(tmp_path, position):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    out = tmp_path / f"out_{position}.pdf"
    apply_bates_numbers(str(src), str(out), prefix="A", position=position)
    assert "A000001" in extract_all_text(out)


def test_bates_top_vs_bottom_region(tmp_path):
    """top-* positions draw high on the page; bottom-* draw low."""
    src = build_pdf(tmp_path / "src.pdf", pages=1)

    def y_of(position):
        out = tmp_path / f"o_{position}.pdf"
        apply_bates_numbers(str(src), str(out), prefix="B",
                            start=7, digits=3, position=position)
        reader = PdfReader(str(out))
        page = reader.pages[0]
        ys = []
        def visitor(text, cm, tm, font_dict, font_size):
            if "B007" in text:
                ys.append(tm[5])
        page.extract_text(visitor_text=visitor)
        return ys[0], float(page.mediabox.height)

    top_y, height = y_of("top-left")
    bottom_y, _ = y_of("bottom-left")
    assert top_y > height / 2
    assert bottom_y < height / 2


def test_bates_rejects_bad_position(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    with pytest.raises(ValueError):
        apply_bates_numbers(str(src), str(tmp_path / "o.pdf"),
                            position="middle")


@pytest.mark.parametrize("digits", [2, 11, 0, -1, "abc"])
def test_bates_rejects_bad_digits(tmp_path, digits):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    with pytest.raises(ValueError):
        apply_bates_numbers(str(src), str(tmp_path / "o.pdf"), digits=digits)


@pytest.mark.parametrize("start", [-1, -100, "xyz"])
def test_bates_rejects_bad_start(tmp_path, start):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    with pytest.raises(ValueError):
        apply_bates_numbers(str(src), str(tmp_path / "o.pdf"), start=start)


def test_bates_start_zero_allowed(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    out = tmp_path / "out.pdf"
    last = apply_bates_numbers(str(src), str(out), start=0, digits=3)
    assert last == "000"


def test_bates_bad_color_raises(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    with pytest.raises(ValueError):
        apply_bates_numbers(str(src), str(tmp_path / "o.pdf"), color="red")


# ==========================================================================
# Route — POST /security/run : headerfooter
# ==========================================================================
def test_route_headerfooter_success(client, tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=2)
    resp = _run(client, "src.pdf", src.read_bytes(), {
        "operation": "headerfooter",
        "footer_center": "Page {page} of {pages}",
        "header_left": "RouteHeadMark",
    })
    assert resp.status_code == 200, resp.data
    data = resp.get_json()
    assert data["success"] is True

    pdf_bytes = _download_bytes(client, data["filename"])
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 2
    texts = [p.extract_text() or "" for p in reader.pages]
    assert "RouteHeadMark" in texts[0]
    assert "Page 1 of 2" in texts[0]
    assert "Page 2 of 2" in texts[1]


def test_route_headerfooter_all_empty_400(client, tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    resp = _run(client, "src.pdf", src.read_bytes(), {
        "operation": "headerfooter",
    })
    assert resp.status_code == 400
    assert "slot" in resp.get_json()["error"].lower()


# ==========================================================================
# Route — POST /security/run : bates
# ==========================================================================
def test_route_bates_success_and_range_message(client, tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=42)
    resp = _run(client, "src.pdf", src.read_bytes(), {
        "operation": "bates",
        "prefix": "ACME",
        "start": "1",
        "digits": "6",
        "position": "bottom-right",
    })
    assert resp.status_code == 200, resp.data
    data = resp.get_json()
    assert data["success"] is True
    # The success message states the stamped range.
    assert "ACME000001" in data["message"]
    assert "ACME000042" in data["message"]

    pdf_bytes = _download_bytes(client, data["filename"])
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 42
    assert "ACME000001" in (reader.pages[0].extract_text() or "")
    assert "ACME000042" in (reader.pages[41].extract_text() or "")


def test_route_bates_bad_digits_400(client, tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    resp = _run(client, "src.pdf", src.read_bytes(), {
        "operation": "bates", "digits": "1",
    })
    assert resp.status_code == 400
    assert "digits" in resp.get_json()["error"].lower()


def test_route_bates_bad_start_400(client, tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    resp = _run(client, "src.pdf", src.read_bytes(), {
        "operation": "bates", "start": "-5",
    })
    assert resp.status_code == 400
    assert "start" in resp.get_json()["error"].lower()
