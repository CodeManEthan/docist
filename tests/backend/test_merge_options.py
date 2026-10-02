"""Tests for the configurable merge pipeline and bookmark support.

Reuses the ``builders`` / ``client`` fixtures from conftest.py.
"""
import io
import re

import pytest
from pypdf import PdfReader

from pdf_ops.merge import merge_pipeline, parse_options, OptionsError


# A page-number overlay is drawn by reportlab at y == 30; source-document text
# lives elsewhere on the page.  Matching on the y=30 baseline isolates the
# numbers we stamp from any digits in the source content.
_NUM_RE = re.compile(rb'1 0 0 1 ([0-9.]+) 30 Tm\s*\((\d+)\) Tj')


def _stamped(reader):
    """Return [(x, number), ...] for every page-number overlay in the doc."""
    found = []
    for page in reader.pages:
        contents = page.get_contents()
        if contents is None:
            continue
        data = bytes(contents.get_data())
        for m in _NUM_RE.finditer(data):
            found.append((float(m.group(1)), int(m.group(2))))
    return found


def _render(writer, tmp_path, name="out.pdf"):
    path = tmp_path / name
    with open(path, "wb") as fh:
        writer.write(fh)
    return PdfReader(str(path))


def _sources(tmp_path, builders, spec):
    """spec: list of (title, page_count) -> list of (path, title)."""
    out = []
    for title, pages in spec:
        p = builders.pdf(tmp_path / f"{title}.pdf", pages=pages, marker=title)
        out.append((str(p), title))
    return out


# ---------------------------------------------------------------------------
# Page numbering
# ---------------------------------------------------------------------------
def test_numbers_on_stamps_sequential(tmp_path, builders):
    sources = _sources(tmp_path, builders, [("alpha", 2)])
    writer = merge_pipeline(sources, {"blank_pages": False})
    reader = _render(writer, tmp_path)
    nums = sorted(n for _x, n in _stamped(reader))
    assert nums == [1, 2]


def test_numbers_off_produces_no_stamps(tmp_path, builders):
    sources = _sources(tmp_path, builders, [("alpha", 2)])
    writer = merge_pipeline(sources, {"page_numbers": False, "blank_pages": False})
    reader = _render(writer, tmp_path)
    assert _stamped(reader) == []


def test_start_number_offsets_the_sequence(tmp_path, builders):
    sources = _sources(tmp_path, builders, [("alpha", 3)])
    writer = merge_pipeline(sources, {"start_number": 5, "blank_pages": False})
    reader = _render(writer, tmp_path)
    nums = sorted(n for _x, n in _stamped(reader))
    assert nums == [5, 6, 7]


def test_position_variants_place_number_in_expected_x_region(tmp_path, builders):
    xs = {}
    for pos in ("bottom-left", "bottom-center", "bottom-right"):
        sources = _sources(tmp_path, builders, [("alpha", 1)])
        writer = merge_pipeline(
            sources, {"number_position": pos, "blank_pages": False}
        )
        reader = _render(writer, tmp_path, name=f"{pos}.pdf")
        stamps = _stamped(reader)
        assert len(stamps) == 1
        xs[pos] = stamps[0][0]

    # Letter width is 612pt: left ~50, center ~306, right ~562.
    assert xs["bottom-left"] < 200
    assert 200 < xs["bottom-center"] < 400
    assert xs["bottom-right"] > 400
    assert xs["bottom-left"] < xs["bottom-center"] < xs["bottom-right"]


# ---------------------------------------------------------------------------
# Blank pages
# ---------------------------------------------------------------------------
def test_blank_pages_false_yields_exact_sum_of_source_pages(tmp_path, builders):
    sources = _sources(tmp_path, builders, [("alpha", 1), ("beta", 3)])
    writer = merge_pipeline(sources, {"blank_pages": False})
    reader = _render(writer, tmp_path)
    assert len(reader.pages) == 4


def test_blank_pages_true_pads_odd_sources(tmp_path, builders):
    sources = _sources(tmp_path, builders, [("alpha", 1), ("beta", 3)])
    writer = merge_pipeline(sources, {"blank_pages": True})
    reader = _render(writer, tmp_path)
    # 1 (odd -> +1) + 3 (odd -> +1) = 6
    assert len(reader.pages) == 6


# ---------------------------------------------------------------------------
# Bookmarks / outline
# ---------------------------------------------------------------------------
def test_bookmarks_true_match_names_and_target_pages_with_blanks(tmp_path, builders):
    sources = _sources(tmp_path, builders, [("alpha", 1), ("beta", 3)])
    writer = merge_pipeline(sources, {"blank_pages": True})
    reader = _render(writer, tmp_path)

    outline = reader.outline
    titles = [item.title for item in outline]
    targets = [reader.get_destination_page_number(item) for item in outline]
    assert titles == ["alpha", "beta"]
    # alpha starts at page 0; its 1 page + 1 blank pushes beta to index 2.
    assert targets == [0, 2]


def test_bookmarks_true_target_pages_without_blanks(tmp_path, builders):
    sources = _sources(tmp_path, builders, [("alpha", 2), ("beta", 2)])
    writer = merge_pipeline(sources, {"blank_pages": False})
    reader = _render(writer, tmp_path)
    outline = reader.outline
    assert [item.title for item in outline] == ["alpha", "beta"]
    assert [reader.get_destination_page_number(item) for item in outline] == [0, 2]


def test_bookmarks_false_yields_empty_outline(tmp_path, builders):
    sources = _sources(tmp_path, builders, [("alpha", 1), ("beta", 1)])
    writer = merge_pipeline(sources, {"bookmarks": False})
    reader = _render(writer, tmp_path)
    assert len(reader.outline) == 0


# ---------------------------------------------------------------------------
# Option parsing / validation
# ---------------------------------------------------------------------------
def test_parse_options_defaults_reproduce_original_behavior():
    opts = parse_options({})
    assert opts == {
        "page_numbers": True,
        "blank_pages": False,
        "bookmarks": True,
        "number_position": "bottom-right",
        "start_number": 1,
    }


@pytest.mark.parametrize("bad", ["0", "-3", "abc", "1.5"])
def test_parse_options_rejects_bad_start_number(bad):
    with pytest.raises(OptionsError):
        parse_options({"start_number": bad})


def test_parse_options_rejects_bad_position():
    with pytest.raises(OptionsError):
        parse_options({"number_position": "top-left"})


def test_parse_options_reads_falsey_flags():
    opts = parse_options(
        {"page_numbers": "false", "blank_pages": "false", "bookmarks": "false"}
    )
    assert opts["page_numbers"] is False
    assert opts["blank_pages"] is False
    assert opts["bookmarks"] is False


# ---------------------------------------------------------------------------
# Route integration
# ---------------------------------------------------------------------------
def _post(client, tmp_path, builders, form):
    pdf = builders.pdf(tmp_path / "a.pdf", pages=1, marker="A")
    data = {"files[]": (io.BytesIO(pdf.read_bytes()), "a.pdf")}
    data.update(form)
    return client.post("/upload", data=data, content_type="multipart/form-data")


def test_upload_invalid_start_number_returns_400(client, tmp_path, builders):
    resp = _post(client, tmp_path, builders, {"start_number": "0"})
    assert resp.status_code == 400
    assert "start_number" in resp.get_json()["error"]


def test_upload_non_integer_start_number_returns_400(client, tmp_path, builders):
    resp = _post(client, tmp_path, builders, {"start_number": "abc"})
    assert resp.status_code == 400


def test_upload_with_options_succeeds_and_bookmarks_disabled(client, tmp_path, builders):
    resp = _post(
        client,
        tmp_path,
        builders,
        {"bookmarks": "false", "page_numbers": "false", "blank_pages": "false"},
    )
    assert resp.status_code == 200
    filename = resp.get_json()["filename"]
    reader = PdfReader(str(client.output_dir / filename))
    assert len(reader.pages) == 1  # no blank padding
    assert len(reader.outline) == 0  # bookmarks disabled
    assert _stamped(reader) == []  # numbers disabled


# ---------------------------------------------------------------------------
# Padding blanks and number overlays on any paper (round prelaunch-fixes,
# design §3.3): a blank matches the page it follows in size and /Rotate.
# ---------------------------------------------------------------------------
from pdf_ops.merge import merge_pdfs_with_blanks  # noqa: E402

_A4 = (595.276, 841.89)


def _pdf_of_size(path, size, pages=1, rotate=0):
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=size)
    for i in range(pages):
        c.drawString(72, 400, f"Sized page {i + 1}")
        c.showPage()
    c.save()
    if rotate:
        from pypdf import PdfWriter
        reader = PdfReader(str(path))
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
            writer.pages[-1].rotate(rotate)
        with open(path, "wb") as fh:
            writer.write(fh)
    return str(path)


def _size(page):
    return round(float(page.mediabox.width), 2), round(float(page.mediabox.height), 2)


def test_blank_after_a4_source_is_a4(tmp_path):
    a4 = _pdf_of_size(tmp_path / "a4.pdf", _A4, pages=3)
    letter = _pdf_of_size(tmp_path / "l.pdf", (612.0, 792.0), pages=1)
    reader = _render(merge_pipeline([(a4, "a4"), (letter, "l")],
                                    {"blank_pages": True, "page_numbers": False}),
                     tmp_path)
    sizes = [_size(p) for p in reader.pages]
    assert len(sizes) == 6
    assert sizes[3] == (595.28, 841.89)   # the blank after 3 A4 pages
    assert sizes[5] == (612.0, 792.0)     # the blank after 1 Letter page


def test_blank_after_letter_source_is_letter(tmp_path):
    letter = _pdf_of_size(tmp_path / "l.pdf", (612.0, 792.0), pages=1)
    reader = _render(merge_pipeline([(letter, "l")], {"blank_pages": True}), tmp_path)
    assert [_size(p) for p in reader.pages] == [(612.0, 792.0)] * 2


def test_blank_copies_rotation(tmp_path):
    rotated = _pdf_of_size(tmp_path / "r.pdf", _A4, pages=1, rotate=90)
    reader = _render(merge_pipeline([(rotated, "r")], {"blank_pages": True}), tmp_path)
    assert len(reader.pages) == 2
    assert reader.pages[1].rotation == 90
    assert _size(reader.pages[1]) == (595.28, 841.89)


def test_unrotated_blank_has_no_rotation(tmp_path):
    src = _pdf_of_size(tmp_path / "s.pdf", _A4, pages=1)
    reader = _render(merge_pipeline([(src, "s")], {"blank_pages": True}), tmp_path)
    assert reader.pages[1].rotation == 0


def test_page_numbers_land_inside_an_a4_page(tmp_path):
    src = _pdf_of_size(tmp_path / "a4.pdf", _A4, pages=2)
    for position in ("bottom-right", "bottom-center", "bottom-left"):
        reader = _render(merge_pipeline([(src, "a4")], {"number_position": position}),
                         tmp_path, name=f"{position}.pdf")
        stamps = _stamped(reader)
        assert sorted(n for _x, n in stamps) == [1, 2]
        assert all(0 < x < _A4[0] for x, _n in stamps)


def test_overlay_canvas_matches_the_page(tmp_path):
    from pdf_ops.merge import _number_overlay
    overlay = _number_overlay(1, _A4[0], "bottom-right", _A4[1])
    assert _size(overlay) == (595.28, 841.89)


def test_legacy_helper_blank_matches_page(tmp_path):
    a4 = _pdf_of_size(tmp_path / "a4.pdf", _A4, pages=1, rotate=90)
    letter = _pdf_of_size(tmp_path / "l.pdf", (612.0, 792.0), pages=3)
    merger = merge_pdfs_with_blanks([a4, letter])
    reader = _render(merger, tmp_path)
    sizes = [_size(p) for p in reader.pages]
    assert sizes == [(595.28, 841.89)] * 2 + [(612.0, 792.0)] * 4
    assert reader.pages[1].rotation == 90
    assert reader.pages[5].rotation == 0
