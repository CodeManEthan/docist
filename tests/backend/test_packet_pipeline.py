"""Tests for the packet page plan and pipeline (design sections 3.3 and 4.8).

Stage B6's parts of the design section 6 list. The shared page-plan vectors
come from tests/fixtures/packet-plan.json; every source PDF is built here with
reportlab and pypdf.
"""
import json
import os
import re
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import ContentStream
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from converters import docx_converter
from pdf_ops import packet as packet_mod
from pdf_ops.merge import _number_overlay
from pdf_ops.packet import (
    DEFAULT_OPTIONS, MAX_OUTPUT_PAGES, PHASE1_NAME, PacketError, packet_pipeline, page_plan,
)

# This file's stage. The fixture's stage order decides which vectors are later.
STAGE = "B6"
CONSUMER = "pipeline"

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "packet-plan.json"
PLAN_FIXTURE = json.loads(FIXTURE.read_text(encoding="utf-8"))

LETTER = (612.0, 792.0)
A4 = (595.276, 841.89)

# The merge overlay draws a number with "1 0 0 1 x 30 Tm" (see
# test_merge_options.py); here the same text is found through its matrix so
# that its displayed position and direction can be checked.
_DIGITS = re.compile(r"^\d+$")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def _pdf(path, pages=1, size=LETTER, marker="Src", rotate=0):
    """A text PDF of ``pages`` pages of ``size``, optionally with /Rotate set."""
    c = canvas.Canvas(str(path), pagesize=size)
    for i in range(pages):
        c.setFont("Helvetica", 14)
        c.drawString(72, size[1] - 72, f"{marker} page {i + 1}")
        c.showPage()
    c.save()
    if rotate:
        reader = PdfReader(str(path))
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page).rotate(rotate)
        with open(path, "wb") as fh:
            writer.write(fh)
    return str(path)


def _scan(path, pages=2, size=A4):
    """An image-only PDF, like a scanner's: no fonts, no text."""
    c = canvas.Canvas(str(path), pagesize=size)
    for i in range(pages):
        img = Image.new("RGB", (200, 280), (230 - 40 * i, 220, 200))
        c.drawImage(ImageReader(img), 0, 0, width=size[0], height=size[1])
        c.showPage()
    c.save()
    return str(path)


def _file(item_id, h, label=None, pages=None, src_pages=None):
    item = {"id": item_id, "kind": "file", "hash": h, "label": label or f"{h}.pdf",
            "pages": pages}
    if pages is not None:
        item["key"] = f"{h}::"
        item["src_pages"] = src_pages
    return item


def _blank(item_id):
    return {"id": item_id, "kind": "blank"}


def _refs(*pairs):
    return [{"src": s, "rot": r} for s, r in pairs]


def _packet(*sections, cover=None, **options):
    """A packet; each section is (title, items) or (title, items, divider)."""
    out = []
    for n, sec in enumerate(sections, 1):
        title, items = sec[0], sec[1]
        divider = sec[2] if len(sec) > 2 else False
        out.append({"id": f"s{n}", "title": title, "divider": divider, "items": items})
    return {"version": 1, "title": "Test", "options": options, "cover": cover, "sections": out}


def _build(tmp_path, packet, staged, options=None, name="out.pdf"):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    out = tmp_path / name
    plan = packet_pipeline(packet, staged, options, str(work), str(out))
    return PdfReader(str(out)), plan


def _size(page):
    return round(float(page.mediabox.width), 2), round(float(page.mediabox.height), 2)


def _mult(m, n):
    a, b, c, d, e, f = m
    p, q, r, s, t, u = n
    return (a * p + b * r, a * q + b * s, c * p + d * r, c * q + d * s,
            e * p + f * r + t, e * q + f * s + u)


def _texts(page):
    """[(text, user-space matrix)] for every text show on ``page``."""
    found = []

    def visit(text, cm, tm, _font, _size):
        if text.strip():
            found.append((text.strip(), _mult(tm, cm)))

    page.extract_text(visitor_text=visit)
    return found


def _numbers(page):
    return [(int(t), m) for t, m in _texts(page) if _DIGITS.match(t)]


def _shown_size(page):
    """The visible box's width and height as the page is displayed."""
    w, h = round(float(page.cropbox.width), 2), round(float(page.cropbox.height), 2)
    return (h, w) if page.rotation % 180 else (w, h)


def _shown(page, m):
    """A user-space matrix as displayed: /Rotate applied, from the crop box's
    lower left, so (1, 0, 0, 1) is upright text at the matrix's x, y."""
    box = page.cropbox
    l, b = float(box.left), float(box.bottom)
    w, h = float(box.width), float(box.height)
    display = {
        0: (1, 0, 0, 1, -l, -b),
        90: (0, -1, 1, 0, -b, w + l),
        180: (-1, 0, 0, -1, w + l, h + b),
        270: (0, 1, -1, 0, h + b, -l),
    }[page.rotation % 360]
    return _mult(m, display)


def _markers(reader):
    """The source marker on each output page ("A page 2"), or None for a blank."""
    out = []
    for page in reader.pages:
        texts = [t for t, _m in _texts(page) if not _DIGITS.match(t)]
        out.append(texts[0] if texts else None)
    return out


# ---------------------------------------------------------------------------
# Shared page-plan vectors
# ---------------------------------------------------------------------------
def _vector_params():
    order = PLAN_FIXTURE["stage_order"]
    params = []
    for vector in PLAN_FIXTURE["vectors"]:
        tag = vector["stage"][CONSUMER]
        marks = []
        if order.index(tag) > order.index(STAGE):
            marks.append(pytest.mark.skip(reason=f"vector tagged {tag}, later than {STAGE}"))
        params.append(pytest.param(vector, id=vector["name"], marks=marks))
    return params


@pytest.mark.parametrize("vector", _vector_params())
def test_page_plan_vectors(vector):
    packet = json.loads(json.dumps(vector["packet"]))
    plan = page_plan(packet, vector["page_counts"], vector.get("contents_pages", 1))
    assert plan == vector["expect"]
    assert packet == vector["packet"]   # the plan leaves its input alone


def test_fixture_lists_its_stage_order_and_tags():
    order = PLAN_FIXTURE["stage_order"]
    assert STAGE in order
    for vector in PLAN_FIXTURE["vectors"]:
        assert set(vector["stage"]) == {"pipeline", "model"}
        assert all(tag in order for tag in vector["stage"].values())


def test_option_defaults_match_the_fixture():
    assert DEFAULT_OPTIONS == PLAN_FIXTURE["option_defaults"]


# ---------------------------------------------------------------------------
# Page order, page edits, pages: null
# ---------------------------------------------------------------------------
def test_pages_in_order_across_files_with_edits(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=3, marker="A")
    b = _pdf(tmp_path / "b.pdf", pages=2, marker="B")
    packet = _packet(("", [
        _file("i1", "ha", pages=_refs((2, 0), (0, 0)), src_pages=3),   # page 2 deleted
        _file("i2", "hb"),                                              # pages: null
        _file("i3", "ha", pages=_refs((1, 0), (1, 0)), src_pages=3),   # duplicated
    ]), page_numbers=False)
    reader, plan = _build(tmp_path, packet, {"ha": a, "hb": b})
    assert _markers(reader) == ["A page 3", "A page 1", "B page 1", "B page 2",
                                "A page 2", "A page 2"]
    assert len(plan["pages"]) == 6


def test_duplicated_pages_rotate_independently(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1, marker="A")
    packet = _packet(("", [_file("i1", "ha", pages=_refs((0, 0), (0, 90), (0, 270)),
                                 src_pages=1)]), page_numbers=False)
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    assert [p.rotation for p in reader.pages] == [0, 90, 270]


def test_rotation_adds_to_a_source_rotation(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1, rotate=90)
    packet = _packet(("", [_file("i1", "ha", pages=_refs((0, 90)), src_pages=1)]),
                     page_numbers=False)
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    assert reader.pages[0].rotation == 180


def test_one_reader_per_staged_pdf(tmp_path, monkeypatch):
    a = _pdf(tmp_path / "a.pdf", pages=2, marker="A")
    b = _pdf(tmp_path / "b.pdf", pages=1, marker="B")
    opened = []
    real = packet_mod.PdfReader

    def counting(path, *args, **kwargs):
        opened.append(os.path.basename(str(path)))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(packet_mod, "PdfReader", counting)
    packet = _packet(("", [_file("i1", "ha", pages=_refs((0, 0)), src_pages=2),
                           _file("i2", "hb"),
                           _file("i3", "ha", pages=_refs((1, 0)), src_pages=2)]))
    _build(tmp_path, packet, {"ha": a, "hb": b})
    assert sorted(opened) == ["a.pdf", "b.pdf", PHASE1_NAME]


# ---------------------------------------------------------------------------
# Blanks and recto padding
# ---------------------------------------------------------------------------
def test_blank_matches_the_body_page_before_it(tmp_path):
    a4 = _pdf(tmp_path / "a4.pdf", pages=1, size=A4)
    letter = _pdf(tmp_path / "l.pdf", pages=1, size=LETTER)
    packet = _packet(("", [_file("i1", "h4"), _blank("b1"), _file("i2", "hl"), _blank("b2")]))
    reader, _plan = _build(tmp_path, packet, {"h4": a4, "hl": letter})
    assert [_size(p) for p in reader.pages] == [(595.28, 841.89)] * 2 + [(612.0, 792.0)] * 2


def test_blank_copies_the_rotation_before_it(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    packet = _packet(("", [_file("i1", "ha", pages=_refs((0, 90)), src_pages=1), _blank("b1")]),
                     page_numbers=False)
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    assert reader.pages[1].rotation == 90
    assert _size(reader.pages[1]) == (612.0, 792.0)


@pytest.mark.parametrize("paper, size", [("letter", (612.0, 792.0)), ("a4", (595.28, 841.89))])
def test_blank_at_the_start_takes_the_packet_paper(tmp_path, paper, size):
    a4 = _pdf(tmp_path / "a4.pdf", pages=1, size=A4)
    packet = _packet(("", [_blank("b1"), _file("i1", "h4")]), paper=paper)
    reader, _plan = _build(tmp_path, packet, {"h4": a4})
    assert _size(reader.pages[0]) == size
    assert reader.pages[0].rotation == 0


def test_recto_pads_each_section_start(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=3, size=A4, marker="A")
    b = _pdf(tmp_path / "b.pdf", pages=1, marker="B")
    c = _pdf(tmp_path / "c.pdf", pages=2, marker="C")
    packet = _packet(("A", [_file("i1", "ha")]), ("B", [_file("i2", "hb")]),
                     ("C", [_file("i3", "hc")]), recto_starts=True)
    reader, plan = _build(tmp_path, packet, {"ha": a, "hb": b, "hc": c})
    assert _markers(reader) == ["A page 1", "A page 2", "A page 3", None,
                                "B page 1", None, "C page 1", "C page 2"]
    # Each pad takes the size of the page before it.
    assert _size(reader.pages[3]) == (595.28, 841.89)
    assert _size(reader.pages[5]) == (612.0, 792.0)
    # Pads use numbers but don't print them with number_generated off.
    printed = [n for page in reader.pages for n, _m in _numbers(page)]
    assert printed == [1, 2, 3, 5, 7, 8]
    assert [p["label"] for p in plan["pages"]] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_recto_never_pads_inside_a_section(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=3, marker="A")
    packet = _packet(("A", [_file("i1", "ha", pages=_refs((0, 0)), src_pages=3),
                            _file("i2", "ha", pages=_refs((1, 0), (2, 0)), src_pages=3)]),
                     recto_starts=True)
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    assert _markers(reader) == ["A page 1", "A page 2", "A page 3"]


def test_recto_pads_print_when_numbering_generated(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    b = _pdf(tmp_path / "b.pdf", pages=1)
    packet = _packet(("A", [_file("i1", "ha")]), ("B", [_file("i2", "hb")]),
                     recto_starts=True, number_generated=True)
    reader, _plan = _build(tmp_path, packet, {"ha": a, "hb": b})
    assert [n for page in reader.pages for n, _m in _numbers(page)] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Page numbers
# ---------------------------------------------------------------------------
def test_numbers_count_from_start_number(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=2)
    packet = _packet(("", [_file("i1", "ha"), _blank("b1")]), start_number=7)
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    assert [n for page in reader.pages for n, _m in _numbers(page)] == [7, 8, 9]


def test_numbers_off_stamps_nothing(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=2)
    reader, _plan = _build(tmp_path, _packet(("", [_file("i1", "ha")]), page_numbers=False),
                           {"ha": a})
    assert [_numbers(page) for page in reader.pages] == [[], []]


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
@pytest.mark.parametrize("position, x_from_right", [("bottom-right", 50), ("bottom-left", None)])
def test_number_is_upright_in_the_displayed_page(tmp_path, rot, position, x_from_right):
    a = _pdf(tmp_path / "a.pdf", pages=1, size=LETTER)
    packet = _packet(("", [_file("i1", "ha", pages=_refs((0, rot)), src_pages=1)]),
                     number_position=position)
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    page = reader.pages[0]
    # The page keeps its /Rotate and its content; only the number is turned.
    assert page.rotation == rot
    assert _size(page) == (612.0, 792.0)
    width, height = _shown_size(page)
    assert (width, height) == ((792.0, 612.0) if rot in (90, 270) else (612.0, 792.0))
    [(number, m)] = _numbers(page)
    assert number == 1
    a_, b_, c_, d_, x, y = _shown(page, m)
    assert (round(a_, 3), round(b_, 3), round(c_, 3), round(d_, 3)) == (1, 0, 0, 1)
    assert y == pytest.approx(30)
    assert x == pytest.approx(width - x_from_right if x_from_right else 50)
    # The source text turned with the page: as shown, its direction follows
    # the rotation, and in user space it hasn't moved.
    [(_text, src)] = [(t, mm) for t, mm in _texts(page) if not _DIGITS.match(t)]
    assert src[4:] == pytest.approx((72, 720))
    shown = _shown(page, src)
    direction = {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}[rot]
    assert (round(shown[0], 3), round(shown[1], 3)) == direction


def test_source_rotation_is_also_upright(tmp_path):
    """A page whose source already carries /Rotate gets its number upright too."""
    a = _pdf(tmp_path / "a.pdf", pages=1, rotate=90)
    reader, _plan = _build(tmp_path, _packet(("", [_file("i1", "ha")])), {"ha": a})
    page = reader.pages[0]
    assert page.rotation == 90
    assert _shown_size(page) == (792.0, 612.0)
    [(_n, m)] = _numbers(page)
    shown = _shown(page, m)
    assert shown[:4] == pytest.approx((1, 0, 0, 1))
    assert shown[4] == pytest.approx(742) and shown[5] == pytest.approx(30)


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
def test_number_sits_in_an_offset_crop_box(tmp_path, rot):
    path = tmp_path / "crop.pdf"
    _pdf(path, pages=1)
    reader = PdfReader(str(path))
    writer = PdfWriter()
    page = writer.add_page(reader.pages[0])
    page.cropbox.lower_left = (100, 150)
    page.cropbox.upper_right = (500, 700)
    with open(path, "wb") as fh:
        writer.write(fh)
    packet = _packet(("", [_file("i1", "hc", pages=_refs((0, rot)), src_pages=1)]))
    out, _plan = _build(tmp_path, packet, {"hc": str(path)})
    page = out.pages[0]
    if rot == 0:   # in user space, without the display mapping
        [(_n, m)] = _numbers(page)
        assert m[4] == pytest.approx(500 - 50) and m[5] == pytest.approx(150 + 30)
    width, _height = _shown_size(page)
    assert width == (550 if rot in (90, 270) else 400)
    [(_n, m)] = _numbers(page)
    shown = _shown(page, m)
    assert shown[:4] == pytest.approx((1, 0, 0, 1))
    assert shown[4] == pytest.approx(width - 50) and shown[5] == pytest.approx(30)


def _linked_pdf(path, rotate=0):
    """One Letter page whose text "Src page 1" (at 72, 720) carries a link."""
    _pdf(path, pages=1, rotate=rotate)
    writer = PdfWriter(clone_from=str(path))
    writer.add_annotation(page_number=0, annotation=Link(
        rect=(70, 714, 200, 736), url="https://example.com/"))
    with open(path, "wb") as fh:
        writer.write(fh)
    return str(path)


@pytest.mark.parametrize("rot, source_rot", [(90, 0), (180, 0), (270, 0), (0, 90)])
def test_links_stay_on_their_text_when_a_rotated_page_is_numbered(tmp_path, rot, source_rot):
    """Stamping a number on a turned page moves no annotation: the link still
    covers its text and lies inside the page, and the number is upright."""
    a = _linked_pdf(tmp_path / "a.pdf", rotate=source_rot)
    packet = _packet(("", [_file("i1", "ha", pages=_refs((0, rot)), src_pages=1)]))
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    page = reader.pages[0]
    assert page.rotation == (rot + source_rot) % 360

    [annot] = [ref.get_object() for ref in page["/Annots"]]
    assert annot["/A"]["/URI"] == "https://example.com/"
    left, bottom, right, top = (float(v) for v in annot["/Rect"])
    assert (left, bottom, right, top) == (70, 714, 200, 736)
    box = page.mediabox
    assert box.left <= left < right <= box.right and box.bottom <= bottom < top <= box.top
    [(_t, src)] = [(t, m) for t, m in _texts(page) if not _DIGITS.match(t)]
    assert left <= src[4] <= right and bottom <= src[5] <= top

    [(number, m)] = _numbers(page)
    assert number == 1
    shown = _shown(page, m)
    assert shown[:4] == pytest.approx((1, 0, 0, 1))
    assert shown[4] == pytest.approx(_shown_size(page)[0] - 50)
    assert shown[5] == pytest.approx(30)


def _used_fonts(page):
    """[(font name, font dict)] for every Tf in ``page``'s own content, each
    name resolved through the page's /Font resources (None if it is missing)."""
    fonts = page["/Resources"].get_object().get("/Font")
    fonts = fonts.get_object() if fonts is not None else {}
    content = ContentStream(page.get_contents(), page.pdf)
    used = []
    for operands, op in content.operations:
        if op == b"Tf":
            name = operands[0]
            used.append((name, fonts[name].get_object() if name in fonts else None))
    return used


def test_mixed_converted_docx_and_scanned_pdf_keep_sound_fonts(tmp_path, builders):
    """Font-dict safety (design section 4.8): numbers stamped on a converted
    DOCX and an image-only scan, interleaved and with one DOCX page used
    twice, leave every font whole and every font the content selects in place.

    Each page's Tf operators are resolved through its own /Font resources:
    every name must resolve to a dict with /Subtype and /BaseFont, the page's
    own text must keep the source page's fonts in order, and the number must
    get the overlay's. A clash between the overlay's Helvetica and the
    converter's (both reportlab /F1) or resources shared between copies of
    one page would show here; removing _detach fails this test.

    This is still a smoke check for Merge's corruption (a font dict losing
    /Subtype when the overlay is stamped on raw converter output): no input on
    hand reproduces it, and a single-phase build passes this test. What pins
    the two-phase order is test_phase1_is_a_file_under_work_dir_and_nothing_
    else_is_written, which fails when numbers are stamped before phase 1 is
    written.
    """
    docx = builders.docx(tmp_path / "doc.docx")
    converted = tmp_path / "doc.pdf"
    docx_converter.convert(str(docx), str(converted))
    scan = _scan(tmp_path / "scan.pdf", pages=2)
    n_docx = len(PdfReader(str(converted)).pages)
    # The fonts each source page selects, in order. The converter draws in
    # Helvetica too, so the overlay's font can collide with the page's own.
    source_fonts = {
        (h, i): [str(f["/BaseFont"]) for _n, f in _used_fonts(page)]
        for h, path in (("hd", str(converted)), ("hs", scan))
        for i, page in enumerate(PdfReader(path).pages)}
    assert any(source_fonts.values())
    overlay = [str(f["/BaseFont"]) for _n, f in _used_fonts(_number_overlay(1, 612, "bottom-right"))]
    assert overlay and set(overlay) == {"/Helvetica"}

    packet = _packet(("", [
        _file("i1", "hd", label="doc.docx"),
        _file("i2", "hs", label="scan.pdf", pages=_refs((0, 90)), src_pages=2),
        _file("i3", "hd", label="doc.docx", pages=_refs((0, 0)), src_pages=n_docx),
        _file("i4", "hs", label="scan.pdf", pages=_refs((1, 0)), src_pages=2),
    ]))
    reader, plan = _build(tmp_path, packet, {"hd": str(converted), "hs": scan})
    total = len(plan["pages"])
    assert len(reader.pages) == total == 2 * n_docx + 2

    for index, page in enumerate(reader.pages):
        fonts = page["/Resources"].get_object().get("/Font")
        assert fonts is not None   # at least the stamped number's font
        for ref in fonts.get_object().values():
            font = ref.get_object()
            assert "/Subtype" in font and "/BaseFont" in font
        used = _used_fonts(page)
        assert all(font is not None for _n, font in used), (index, used)
        base = [str(font["/BaseFont"]) for _n, font in used]
        # The page's own text keeps its fonts, and the number, drawn last,
        # gets the overlay's.
        info = plan["pages"][index]
        assert base == source_fonts[(info["hash"], info["src"])] + overlay, (index, base)
    assert [n for page in reader.pages for n, _m in _numbers(page)] == list(range(1, total + 1))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert builders.DOCX_BODY in text


# ---------------------------------------------------------------------------
# Bookmarks, phase 1, files written
# ---------------------------------------------------------------------------
def test_flat_bookmarks_point_at_each_file_items_first_page(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=2)
    b = _pdf(tmp_path / "b.pdf", pages=1)
    packet = _packet(("A", [_file("i1", "ha", label="resume.docx"), _blank("x")]),
                     ("B", [_file("i2", "hb", label="refs.pdf"),
                            _file("i3", "hx", label="not-ready.pdf", pages=[], src_pages=1)]),
                     recto_starts=True)
    reader, _plan = _build(tmp_path, packet, {"ha": a, "hb": b, "hx": b})
    outline = [(item.title, reader.get_destination_page_number(item)) for item in reader.outline]
    # A, A, blank, pad (recto), refs.pdf; the emptied item gets no bookmark.
    assert outline == [("resume.docx", 0), ("refs.pdf", 4)]


def test_bookmarks_off_adds_no_outline(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=2)
    reader, _plan = _build(tmp_path, _packet(("", [_file("i1", "ha")]), bookmarks=False),
                           {"ha": a})
    assert reader.outline == []


def test_source_bookmarks_are_not_carried(tmp_path):
    path = tmp_path / "marked.pdf"
    _pdf(path, pages=2)
    writer = PdfWriter(clone_from=str(path))
    writer.add_outline_item("Inner", 1)
    with open(path, "wb") as fh:
        writer.write(fh)
    reader, _plan = _build(tmp_path, _packet(("", [_file("i1", "hm", label="m.pdf")])),
                           {"hm": str(path)})
    assert [item.title for item in reader.outline] == ["m.pdf"]


def test_phase1_is_a_file_under_work_dir_and_nothing_else_is_written(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=2)
    before = set(os.listdir(tmp_path))
    reader, _plan = _build(tmp_path, _packet(("", [_file("i1", "ha"), _blank("b")])), {"ha": a})
    assert os.listdir(tmp_path / "work") == [PHASE1_NAME]
    phase1 = PdfReader(str(tmp_path / "work" / PHASE1_NAME))
    assert len(phase1.pages) == len(reader.pages) == 3
    assert [_numbers(p) for p in phase1.pages] == [[], [], []]   # numbers come in phase 2
    assert set(os.listdir(tmp_path)) - before == {"work", "out.pdf"}


def test_options_argument_wins_over_the_packets_own(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    packet = _packet(("", [_file("i1", "ha")]), page_numbers=True)
    reader, _plan = _build(tmp_path, packet, {"ha": a}, options={"page_numbers": False})
    assert _numbers(reader.pages[0]) == []


# ---------------------------------------------------------------------------
# Refusals: every one is a PacketError with a code
# ---------------------------------------------------------------------------
def _refusal(tmp_path, packet, staged, options=None):
    with pytest.raises(PacketError) as info:
        _build(tmp_path, packet, staged, options)
    assert not (tmp_path / "out.pdf").exists()
    return info.value


def test_packet_error_is_a_value_error():
    err = PacketError("stale", "message", [{"id": "i1", "code": "stale"}])
    assert isinstance(err, ValueError)
    assert (err.code, err.message) == ("stale", "message")
    assert err.items == [{"id": "i1", "code": "stale"}]


def test_output_page_cap(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    refs = _refs(*[(0, 0)] * (MAX_OUTPUT_PAGES + 1))
    err = _refusal(tmp_path, _packet(("", [_file("i1", "ha", pages=refs, src_pages=1)])),
                   {"ha": a})
    assert MAX_OUTPUT_PAGES == 2000
    assert err.code == "too_large" and "2001" in err.message
    assert os.listdir(tmp_path / "work") == []   # refused before any work


def test_output_page_cap_counts_generated_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(packet_mod, "MAX_OUTPUT_PAGES", 3)
    a = _pdf(tmp_path / "a.pdf", pages=1)
    b = _pdf(tmp_path / "b.pdf", pages=1)
    ok = _packet(("", [_file("i1", "ha"), _blank("x"), _file("i2", "hb")]))
    assert len(_build(tmp_path, ok, {"ha": a, "hb": b})[0].pages) == 3
    os.remove(tmp_path / "out.pdf")
    over = _packet(("A", [_file("i1", "ha")]), ("B", [_file("i2", "hb"), _blank("x")]),
                   recto_starts=True)   # a pad makes it 4
    assert _refusal(tmp_path, over, {"ha": a, "hb": b}).code == "too_large"


def test_corrupt_staged_pdf_is_unreadable(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\n" + b"\x00garbage" * 200)
    err = _refusal(tmp_path, _packet(("", [_file("i1", "hb"), _file("i2", "hb")])),
                   {"hb": str(bad)})
    assert err.code == "unreadable"
    assert err.items == [{"id": "i1", "code": "unreadable"}, {"id": "i2", "code": "unreadable"}]


def test_truncated_staged_pdf_is_unreadable(tmp_path):
    path = tmp_path / "t.pdf"
    _pdf(path, pages=3)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    err = _refusal(tmp_path, _packet(("", [_file("i1", "ht")])), {"ht": str(path)})
    assert err.code == "unreadable"


def test_encrypted_staged_pdf_is_refused(tmp_path):
    path = tmp_path / "locked.pdf"
    _pdf(path, pages=1)
    writer = PdfWriter(clone_from=str(path))
    writer.encrypt(user_password="secret", owner_password="owner")
    with open(path, "wb") as fh:
        writer.write(fh)
    err = _refusal(tmp_path, _packet(("", [_file("i1", "hl")])), {"hl": str(path)})
    assert err.code == "encrypted" and err.items == [{"id": "i1", "code": "encrypted"}]


def test_owner_only_encryption_builds(tmp_path):
    path = tmp_path / "owner.pdf"
    _pdf(path, pages=2, marker="O")
    writer = PdfWriter(clone_from=str(path))
    writer.encrypt(user_password="", owner_password="owner")
    with open(path, "wb") as fh:
        writer.write(fh)
    reader, _plan = _build(tmp_path, _packet(("", [_file("i1", "ho")])), {"ho": str(path)})
    assert _markers(reader) == ["O page 1", "O page 2"]


def test_unstaged_hash_is_expired(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    err = _refusal(tmp_path, _packet(("", [_file("i1", "ha"), _file("i2", "hz"),
                                           _file("i3", "hz")])), {"ha": a})
    assert err.code == "expired"
    assert err.items == [{"id": "i2", "code": "expired"}, {"id": "i3", "code": "expired"}]


def test_missing_staged_file_is_expired(tmp_path):
    err = _refusal(tmp_path, _packet(("", [_file("i1", "ha")])),
                   {"ha": str(tmp_path / "gone.pdf")})
    assert err.code == "expired"


def test_out_of_range_ref(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=2)
    packet = _packet(("", [_file("i1", "ha", pages=_refs((0, 0), (2, 0)), src_pages=2),
                           _file("i2", "ha", pages=_refs((1, 0)), src_pages=2)]))
    err = _refusal(tmp_path, packet, {"ha": a})
    assert err.code == "out_of_range" and err.items == [{"id": "i1", "code": "out_of_range"}]


def test_refs_made_on_another_page_count_are_stale(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=2)
    packet = _packet(("", [_file("i1", "ha", pages=_refs((0, 0)), src_pages=9)]))
    err = _refusal(tmp_path, packet, {"ha": a})
    assert err.code == "stale" and err.items == [{"id": "i1", "code": "stale"}]


@pytest.mark.parametrize("bad_item", [
    {"id": "i1", "kind": "file", "hash": "ha", "src_pages": 1, "pages": [{"src": 0, "rot": 45}]},
    {"id": "i1", "kind": "file", "hash": "ha", "src_pages": 1,
     "pages": [{"src": 0, "rot": 90.0}]},
    {"id": "i1", "kind": "file", "hash": "ha", "src_pages": 1, "pages": [{"src": "0", "rot": 0}]},
    {"id": "i1", "kind": "file", "hash": "ha", "src_pages": 1, "pages": [{"src": True, "rot": 0}]},
    # Explicit refs without the page count they were made on can't be fit.
    {"id": "i1", "kind": "file", "hash": "ha", "pages": [{"src": 0, "rot": 0}]},
    {"id": "i1", "kind": "file", "hash": "", "pages": None},
    {"id": "i1", "kind": "file", "hash": "ha", "pages": "all"},
    {"id": "i1", "kind": "text"},
])
def test_malformed_items_are_invalid(tmp_path, bad_item):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    err = _refusal(tmp_path, _packet(("", [bad_item])), {"ha": a})
    assert err.code == "invalid" and err.items == [{"id": "i1", "code": "invalid"}]


def test_duplicate_item_ids_are_invalid(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    packet = _packet(("A", [_file("i1", "ha")]), ("B", [_blank("i1")]))
    err = _refusal(tmp_path, packet, {"ha": a})
    assert err.code == "invalid" and err.items == [{"id": "i1", "code": "invalid"}]


@pytest.mark.parametrize("staged", [None, [], "ha"])
def test_staged_paths_that_are_not_a_map_are_invalid(tmp_path, staged):
    assert _refusal(tmp_path, _packet(("", [_blank("b")])), staged).code == "invalid"


@pytest.mark.parametrize("error", [OSError("disk full"), ValueError("pypdf broke")])
def test_a_failed_final_write_leaves_no_out_path(tmp_path, monkeypatch, error):
    """The finished PDF is written under work_dir and moved into place, so a
    failure leaves neither a partial out_path nor the part file."""
    a = _pdf(tmp_path / "a.pdf", pages=2)
    real_write = PdfWriter.write
    calls = []

    def write(self, stream):
        calls.append(stream)
        if len(calls) == 2:   # phase 1 succeeds, the final write fails part way
            stream.write(b"%PDF-1.7 partial")
            raise error
        return real_write(self, stream)

    monkeypatch.setattr(PdfWriter, "write", write)
    with pytest.raises((OSError, PacketError)):
        _build(tmp_path, _packet(("", [_file("i1", "ha")])), {"ha": a})
    assert len(calls) == 2
    assert not (tmp_path / "out.pdf").exists()
    assert os.listdir(tmp_path / "work") == [PHASE1_NAME]


@pytest.mark.parametrize("packet", [
    None, {}, {"sections": "s1"}, {"sections": [{"id": "s1"}]},
    {"sections": [{"id": "s1", "items": ["i1"]}]}, {"sections": [], "cover": "yes"},
])
def test_malformed_packets_are_invalid(tmp_path, packet):
    assert _refusal(tmp_path, packet, {}).code == "invalid"


@pytest.mark.parametrize("options", [
    {"paper": "legal"}, {"number_position": "top"}, {"start_number": 0},
    {"start_number": "1"}, {"start_number": True}, {"page_numbers": "yes"}, "on",
    {"paper": ["a4"]},
])
def test_bad_options_are_invalid(tmp_path, options):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    assert _refusal(tmp_path, _packet(("", [_file("i1", "ha")])), {"ha": a},
                    options=options).code == "invalid"


def test_nothing_to_build_is_empty(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    packet = _packet(("", [_file("i1", "ha", pages=[], src_pages=1)]))
    assert _refusal(tmp_path, packet, {"ha": a}).code == "empty"


@pytest.mark.parametrize("packet", [
    _packet(("", [_file("i1", "ha")]),
            cover={"title": "T", "subtitle": "", "date": "", "note": ""}),
    _packet(("A", [_file("i1", "ha")], True)),
    _packet(("", [_file("i1", "ha")]), contents=True),
])
def test_generated_pages_without_a_renderer_are_unsupported(tmp_path, packet):
    """Cover and divider renderers come with B3, the contents page with B5."""
    a = _pdf(tmp_path / "a.pdf", pages=1)
    assert _refusal(tmp_path, packet, {"ha": a}).code == "unsupported"


def test_a_divider_on_an_empty_section_needs_no_renderer(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    packet = _packet(("A", [_file("i1", "ha")]), ("Empty", [], True))
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    assert len(reader.pages) == 1


def test_pipeline_imports_no_job_or_limit_module_and_no_tempfile():
    """Design section 4.8: B1 runs the pipeline in its job, so it brings none."""
    source = Path(packet_mod.__file__).read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, re.M)
    assert not [m for m in imports if m in ("pdf_ops.jobs", "pdf_ops.limits", "tempfile")]
    assert "pdf_ops.merge" in imports


def test_copies_of_one_source_page_are_stamped_and_turned_apart(tmp_path):
    """One reader per staged PDF makes pypdf share a source page's content
    stream between its copies; stamping or rotating one must not touch another."""
    a = _pdf(tmp_path / "a.pdf", pages=2, marker="A")
    b = _pdf(tmp_path / "b.pdf", pages=1, marker="B")
    packet = _packet(("", [
        _file("i1", "ha", pages=_refs((0, 0), (0, 90)), src_pages=2),
        _file("i2", "hb"),
        _file("i3", "ha", pages=_refs((0, 180), (0, 0)), src_pages=2),
    ]))
    reader, _plan = _build(tmp_path, packet, {"ha": a, "hb": b})
    assert [[n for n, _m in _numbers(p)] for p in reader.pages] == [[1], [2], [3], [4], [5]]
    assert [p.rotation for p in reader.pages] == [0, 90, 0, 180, 0]
    directions = []
    for page in reader.pages:
        [(_t, m)] = [(t, mm) for t, mm in _texts(page) if not _DIGITS.match(t)]
        shown = _shown(page, m)
        directions.append((round(shown[0]), round(shown[1])))
        [(_n, nm)] = _numbers(page)
        assert _shown(page, nm)[:4] == pytest.approx((1, 0, 0, 1))
    assert directions == [(1, 0), (0, -1), (1, 0), (-1, 0), (1, 0)]
