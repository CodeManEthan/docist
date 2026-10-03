"""Tests for the packet page plan and pipeline (design sections 3.3 and 4.8).

Stage B6's parts of the design section 6 list, B1's build-route cases at the
end, B3's cover, divider and outline cases, B5's contents page, and B7's
number placement. The shared page-plan vectors
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
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from converters import docx_converter
from pdf_ops import packet as packet_mod
from pdf_ops.merge import _number_overlay, _number_xy
from pdf_ops.packet import (
    DEFAULT_OPTIONS, MAX_OUTPUT_PAGES, PHASE1_NAME, PacketError, packet_pipeline, page_plan,
)

# This file's stage: B6 wrote it, B1 added the build route's byte-cap and
# stale cases, B3 the cover, divider and nested-outline cases, and B5 the
# contents page, and B7 the number placement (its vectors under
# placement_vectors). The fixture's stage order decides which vectors are
# later; every page-plan vector is tagged B6 for this consumer, so all of
# them run.
STAGE = "B7"
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


def _placement_params():
    order = PLAN_FIXTURE["stage_order"]
    params = []
    for vector in PLAN_FIXTURE["placement_vectors"]:
        tag = vector["stage"][CONSUMER]
        marks = []
        if order.index(tag) > order.index(STAGE):
            marks.append(pytest.mark.skip(reason=f"vector tagged {tag}, later than {STAGE}"))
        params.append(pytest.param(vector, id=vector["name"], marks=marks))
    return params


@pytest.mark.parametrize("vector", _placement_params())
def test_number_placement_vectors(vector):
    """Design section 3.3 rule 7: the stamp's placement, as the page script's."""
    x, y, align = _number_xy(vector["number_position"], vector["width"])
    want = vector["expect"]
    assert x == pytest.approx(want["x"], abs=1e-6)
    assert (y, align) == (want["y"], want["align"])


def test_placement_vectors_are_tagged_like_the_plan_vectors():
    order = PLAN_FIXTURE["stage_order"]
    assert "B7" in order
    for vector in PLAN_FIXTURE["placement_vectors"]:
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
            assert font is not None and "/Subtype" in font and "/BaseFont" in font
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
def _outline(reader, items=None):
    """The outline as [(title, page index, [children])]."""
    out = []
    for entry in reader.outline if items is None else items:
        if isinstance(entry, list):
            title, page, _kids = out[-1]
            out[-1] = (title, page, _outline(reader, entry))
        else:
            out.append((entry.title, reader.get_destination_page_number(entry), []))
    return out


def test_one_section_has_flat_bookmarks_at_each_file_items_first_page(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=2)
    b = _pdf(tmp_path / "b.pdf", pages=1)
    packet = _packet(("", [_file("i1", "ha", label="resume.docx"), _blank("x"),
                           _file("i2", "hb", label="refs.pdf"),
                           _file("i3", "hx", label="not-ready.pdf", pages=[], src_pages=1)]))
    reader, _plan = _build(tmp_path, packet, {"ha": a, "hb": b, "hx": b})
    # A, A, blank, refs.pdf; the emptied item and the blank get no bookmark.
    assert _outline(reader) == [("resume.docx", 0, []), ("refs.pdf", 3, [])]


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


def test_a_generated_page_without_its_renderer_is_unsupported(tmp_path, monkeypatch):
    """Since B5 every generated kind the plan makes has a renderer; the rule
    still refuses one that has none, before any work."""
    monkeypatch.delitem(packet_mod._RENDERERS, "contents")
    a = _pdf(tmp_path / "a.pdf", pages=1)
    packet = _packet(("", [_file("i1", "ha")]), contents=True)
    err = _refusal(tmp_path, packet, {"ha": a})
    assert (err.code, err.message, err.items) == (
        "unsupported", "Contents pages are not available yet.", [])
    assert os.listdir(tmp_path / "work") == []


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


# ---------------------------------------------------------------------------
# Stage B1: the build route's byte cap and fit check (design sections 4.3, 4.6)
# ---------------------------------------------------------------------------
def test_build_route_refuses_a_packet_over_the_byte_cap(client, monkeypatch):
    """A build reads no more staged PDF bytes than one Merge request may carry:
    the tier's request limit, refused up front with too_large and the number."""
    import packet_helpers as ph
    from routes import packet_build
    a = ph.staged(client, ph.pdf_bytes(2, marker="A"))
    b = ph.staged(client, ph.pdf_bytes(3, marker="B"))
    cap = a["bytes"] + 10   # less than both staged PDFs together
    monkeypatch.setattr(packet_build, "packet_limit_bytes", lambda user: cap)
    response = ph.build(client, ph.packet(ph.file_item("i1", a), ph.file_item("i2", b)), [a, b])
    body = ph.assert_shape(response, "build", "too_large")
    assert body["limit_bytes"] == cap and body["items"] == []
    assert os.listdir(client.output_dir) == []


def test_build_route_refuses_refs_that_dont_fit_their_stage_as_stale(client):
    """Refs made on one conversion don't fit another: the paper changed (a new
    key for the same hash) or the page count did. Both are stale; all pages is
    never stale."""
    import packet_helpers as ph
    md = b"# Notes\n\n" + b"Some packet notes.\n\n" * 5
    letter = ph.staged(client, md, "notes.md", paper="letter")
    a4 = ph.staged(client, md, "notes.md", paper="a4")
    assert letter["key"] != a4["key"]
    edited = ph.file_item("i1", letter, pages=[{"src": 0, "rot": 90}])
    response = ph.build(client, ph.packet(edited), [a4])
    body = ph.assert_shape(response, "build", "bad_items", code="stale")
    assert body["items"] == [{"id": "i1", "code": "stale"}]
    miscounted = dict(ph.file_item("i1", a4, pages=[{"src": 0, "rot": 0}]),
                      src_pages=a4["pages"] + 1)
    body = ph.build(client, ph.packet(miscounted), [a4]).get_json()
    assert body["items"] == [{"id": "i1", "code": "stale"}]
    whole = ph.file_item("i1", letter)
    assert ph.build(client, ph.packet(whole), [a4]).status_code == 200


# ---------------------------------------------------------------------------
# Stage B3: cover and divider pages, the nested outline (design sections 2.1,
# 3.3, 4.8)
# ---------------------------------------------------------------------------
def _cover(title="Application", subtitle="J. Pluto", date="October 3, 2026", note=""):
    return {"title": title, "subtitle": subtitle, "date": date, "note": note}


def _lines(page):
    """The page's text shows, numbers left out, in order."""
    return [text for text, _m in _texts(page) if not _DIGITS.match(text)]


def _centres(page):
    """[(text, x centre)] for every non-number text show on ``page``."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    found = []

    def visit(text, cm, tm, font, size):
        text = text.strip()
        if not text or _DIGITS.match(text):
            return
        m = _mult(tm, cm)
        name = font.get("/BaseFont", "/Helvetica")[1:] if font else "Helvetica"
        found.append((text, m[4] + stringWidth(text, name, size * m[0]) / 2, size * m[0]))

    page.extract_text(visitor_text=visit)
    return found


def test_cover_and_dividers_in_plan_order_with_recto_starts(tmp_path):
    """The packet the brief asks for: a cover, two sections with dividers and
    recto starts on. Order, labels, printing and sizes follow the plan."""
    a = _pdf(tmp_path / "a.pdf", pages=3, marker="A")
    b = _pdf(tmp_path / "b.pdf", pages=2, marker="B")
    packet = _packet(("Resume", [_file("i1", "ha")], True),
                     ("References", [_file("i2", "hb"), _blank("x")], True),
                     cover=_cover(), recto_starts=True)
    reader, plan = _build(tmp_path, packet, {"ha": a, "hb": b})
    kinds = [p["kind"] for p in plan["pages"]]
    assert kinds == ["cover", "pad", "divider", "file", "file", "file",
                     "divider", "file", "file", "blank"]
    assert _markers(reader) == ["Application", None, "Resume", "A page 1", "A page 2",
                                "A page 3", "References", "B page 1", "B page 2", None]
    # Front matter has no number; dividers use numbers but don't print them.
    assert [[n for n, _m in _numbers(p)] for p in reader.pages] == [
        [], [], [], [2], [3], [4], [], [6], [7], [8]]
    assert [p["label"] for p in plan["pages"]] == [None, None, 1, 2, 3, 4, 5, 6, 7, 8]
    assert all(_size(p) == LETTER for p in reader.pages)


@pytest.mark.parametrize("paper, size", [("letter", (612.0, 792.0)), ("a4", (595.28, 841.89))])
def test_divider_is_the_section_title_centred_on_the_packet_paper(tmp_path, paper, size):
    a4 = _pdf(tmp_path / "a4.pdf", pages=1, size=A4, marker="A")
    packet = _packet(("Exhibit A", [_file("i1", "h4")], True), paper=paper)
    reader, _plan = _build(tmp_path, packet, {"h4": a4})
    divider = reader.pages[0]
    assert _size(divider) == size and divider.rotation == 0
    [(text, centre, font_size)] = _centres(divider)
    assert text == "Exhibit A"
    assert centre == pytest.approx(size[0] / 2, abs=1)
    assert font_size == 36
    [(_t, m)] = [(t, mm) for t, mm in _texts(divider) if t == "Exhibit A"]
    assert m[5] == pytest.approx(size[1] / 2, abs=40)   # in the middle of the page


def test_an_untitled_sections_divider_reads_untitled_section(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1, marker="A")
    b = _pdf(tmp_path / "b.pdf", pages=1, marker="B")
    packet = _packet(("", [_file("i1", "ha")], True), ("Two", [_file("i2", "hb")], True))
    reader, _plan = _build(tmp_path, packet, {"ha": a, "hb": b})
    assert _markers(reader) == ["Untitled section", "A page 1", "Two", "B page 1"]


def test_dividers_print_their_number_upright_when_numbering_generated(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1, marker="A")
    packet = _packet(("One", [_file("i1", "ha")], True), number_generated=True,
                     number_position="bottom-center")
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    [(n, m)] = _numbers(reader.pages[0])
    assert n == 1
    assert _shown(reader.pages[0], m)[:4] == pytest.approx((1, 0, 0, 1))
    assert [t for t, _m in _texts(reader.pages[0])] == ["One", "1"]


def test_cover_draws_each_field_centred_and_is_never_numbered(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1, marker="A")
    packet = _packet(("", [_file("i1", "ha")]), page_numbers=True, number_generated=True,
                     cover=_cover(note="Prepared for Acme Corp."))
    reader, plan = _build(tmp_path, packet, {"ha": a})
    cover = reader.pages[0]
    shown = _centres(cover)
    assert [t for t, _c, _s in shown] == ["Application", "J. Pluto", "October 3, 2026",
                                          "Prepared for Acme Corp."]
    assert all(c == pytest.approx(LETTER[0] / 2, abs=1) for _t, c, _s in shown)
    sizes = [s for _t, _c, s in shown]
    assert sizes == sorted(sizes, reverse=True)   # the title is the largest
    assert _numbers(cover) == []
    assert plan["pages"][0] == {"kind": "cover", "label": None, "print": False}
    assert [n for n, _m in _numbers(reader.pages[1])] == [1]


def test_cover_leaves_out_empty_fields(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    packet = _packet(("", [_file("i1", "ha")]), cover={"title": "Only a title"})
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    assert _lines(reader.pages[0]) == ["Only a title"]


def test_a_cover_with_nothing_on_it_is_a_plain_page(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    packet = _packet(("", [_file("i1", "ha")]), cover=_cover("", "", "", ""))
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    assert len(reader.pages) == 2 and _lines(reader.pages[0]) == []


def test_long_text_wraps_inside_the_margins_and_is_cut_with_an_ellipsis(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    long_title = " ".join(["Considerably"] * 60)
    unbroken = "X" * 400
    packet = _packet((unbroken, [_file("i1", "ha")], True),
                     cover=_cover(title=long_title, subtitle="Line\nbreaks\tand  tabs"))
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    from reportlab.pdfbase.pdfmetrics import stringWidth
    for page in reader.pages[:2]:
        for text, centre, size in _centres(page):
            half = stringWidth(text, "Helvetica-Bold", size) / 2
            assert centre - half >= 72 - 1 and centre + half <= LETTER[0] - 72 + 1, text
    cover_lines = _lines(reader.pages[0])
    title_lines = [t for t in cover_lines if t.startswith("Considerably")]
    assert len(title_lines) == 4 and title_lines[-1].endswith("…")
    assert "Line breaks and tabs" in cover_lines
    divider_lines = _lines(reader.pages[1])
    assert len(divider_lines) == 4 and divider_lines[-1].endswith("…")
    assert all(set(line.rstrip("…")) == {"X"} for line in divider_lines)


def test_cover_and_divider_text_is_cut_before_it_is_fitted(tmp_path, monkeypatch):
    """Verification-b3 n3: a crafted megabyte title is cut to _TEXT_CUT
    characters before the wrap-and-shrink loop, and still ends in an
    ellipsis on the page."""
    a = _pdf(tmp_path / "a.pdf", pages=1)
    huge = "word " * 200_000
    fitted = []
    real_fit = packet_mod._fit

    def fit(text, *args):
        fitted.append(len(text))
        return real_fit(text, *args)

    monkeypatch.setattr(packet_mod, "_fit", fit)
    packet = _packet((huge, [_file("i1", "ha")], True), cover=_cover(title=huge, note=huge))
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    assert fitted and max(fitted) <= 2000
    assert _lines(reader.pages[0])[3].endswith("\u2026")
    assert _lines(reader.pages[1])[-1].endswith("\u2026")


def test_the_cut_never_changes_the_page(tmp_path):
    """The cut is past what any block can show: the narrowest glyph the fonts
    draw, in the smallest block (the note), gives the same page whole or cut."""
    a = _pdf(tmp_path / "a.pdf", pages=1)
    narrow = "\u2758" * 5000

    def note_stream(note, name):
        packet = _packet(("", [_file("i1", "ha")]), cover=_cover("", "", "", note))
        reader, _plan = _build(tmp_path, packet, {"ha": a}, name=name)
        return reader.pages[0].get_contents().get_data()

    whole = note_stream(narrow, "whole.pdf")
    assert whole == note_stream(narrow[:packet_mod._TEXT_CUT], "cut.pdf")
    assert b"\\205" in whole or "\u2026".encode("cp1252") in whole   # the ellipsis is drawn


def _reportlab_prints():
    """The code points ReportLab draws (not as a box) in the cover and divider
    fonts, after the server's cleaning: Helvetica, then its Symbol and
    ZapfDingbats fallbacks."""
    from reportlab.pdfbase import pdfmetrics
    encodings = None
    for name in ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique"):
        font = pdfmetrics.getFont(name)
        these = [font.encName] + [f.encName for f in font.substitutionFonts]
        assert encodings in (None, these)
        encodings = these
    prints = set()
    for cp in range(0x110000):
        if 0xD800 <= cp < 0xE000 or not chr(cp).isprintable():
            continue
        for enc in encodings:
            try:
                chr(cp).encode(enc)
            except UnicodeEncodeError:
                continue
            prints.add(cp)
            break
    return prints


def test_the_page_scripts_printable_table_is_what_reportlab_prints():
    """Verification-b3 m2: the preview draws a box for each character the PDF
    can't print, from static/packet-model.js's PDF_TEXT_RANGES. That table
    must be exactly the set ReportLab prints."""
    source = (Path(__file__).resolve().parents[2] / "static" / "packet-model.js").read_text(encoding="utf-8")
    block = re.search(r"const PDF_TEXT_RANGES = \[(.*?)\n\];", source, re.S)
    assert block, "PDF_TEXT_RANGES not found in packet-model.js"
    table = set()
    for lo, hi in re.findall(r"\[0x([0-9A-F]+), 0x([0-9A-F]+)\]", block.group(1)):
        table.update(range(int(lo, 16), int(hi, 16) + 1))
    assert table == _reportlab_prints()


def test_a_character_the_fonts_cannot_draw_prints_as_a_box(tmp_path):
    """What PDF_BOX in the page script stands for: ReportLab draws ZapfDingbats
    "n" (a filled square) for ł and the like, and Greek through Symbol."""
    a = _pdf(tmp_path / "a.pdf", pages=1)
    packet = _packet(("", [_file("i1", "ha")]), cover=_cover("\u0141\u00f3d\u017a \u0395\u03bb", "", "", ""))
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    data = reader.pages[0].get_contents().get_data()
    fonts = {str(f["/BaseFont"]) for f in reader.pages[0]["/Resources"]["/Font"].values()}
    assert {"/Helvetica-Bold", "/ZapfDingbats", "/Symbol"} <= fonts
    # Ł and ź are boxes; ó is WinAnsi \363; Ε and λ print through Symbol.
    shows = re.findall(rb"\((.*?)\) Tj", data)
    assert shows == [b"n", b"\\363d", b"n", b" ", b"El"]


@pytest.mark.parametrize("cover", [
    {"title": 3}, {"title": "T", "subtitle": None}, {"date": ["2026"]}, {"note": {"a": 1}},
])
def test_cover_fields_that_are_not_text_are_invalid(tmp_path, cover):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    packet = _packet(("", [_file("i1", "ha")]), cover=cover)
    assert _refusal(tmp_path, packet, {"ha": a}).code == "invalid"


def test_outline_nests_files_under_their_sections(tmp_path):
    """Design section 3.3 rule 6: each section points at its divider when it
    has one, else its first page; its file items nest under it; blanks, empty
    items and sections that give no page get no bookmark."""
    a = _pdf(tmp_path / "a.pdf", pages=2)
    b = _pdf(tmp_path / "b.pdf", pages=1)
    packet = _packet(("Resume", [_file("i1", "ha", label="resume.docx"), _blank("x")], True),
                     ("", [_file("i2", "hb", label="refs.pdf"),
                           _file("i3", "hx", label="not-ready.pdf", pages=[], src_pages=1),
                           _file("i4", "ha", label="again.pdf", pages=_refs((1, 0)),
                                 src_pages=2)]),
                     ("Empty", [], True),
                     cover=_cover(), recto_starts=True)
    reader, _plan = _build(tmp_path, packet, {"ha": a, "hb": b, "hx": b})
    # cover, pad, divider, A1, A2, blank, refs, again
    assert _outline(reader) == [
        ("Resume", 2, [("resume.docx", 3, [])]),
        ("Untitled section", 6, [("refs.pdf", 6, []), ("again.pdf", 7, [])]),
    ]


def test_bookmarks_off_adds_no_outline_with_sections(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    packet = _packet(("A", [_file("i1", "ha")], True), ("B", [_blank("x")]), bookmarks=False)
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    assert reader.outline == []


def test_a_blank_after_a_divider_matches_the_body_page_before_the_divider(tmp_path):
    """Design section 2.1: a blank skips any cover or divider to size itself."""
    a4 = _pdf(tmp_path / "a4.pdf", pages=1, size=A4)
    packet = _packet(("One", [_file("i1", "h4")]), ("Two", [_blank("x")], True),
                     paper="letter")
    reader, _plan = _build(tmp_path, packet, {"h4": a4})
    assert [_size(p) for p in reader.pages] == [(595.28, 841.89), (612.0, 792.0),
                                                (595.28, 841.89)]


def test_a_blank_after_only_a_cover_takes_the_packet_paper(tmp_path):
    a4 = _pdf(tmp_path / "a4.pdf", pages=1, size=A4)
    packet = _packet(("", [_blank("x"), _file("i1", "h4")]), paper="letter",
                     cover=_cover())
    reader, _plan = _build(tmp_path, packet, {"h4": a4})
    assert [_size(p) for p in reader.pages] == [(612.0, 792.0)] * 2 + [(595.28, 841.89)]


def test_generated_pages_keep_sound_fonts_beside_a_converted_docx(tmp_path, builders):
    """Font-dict safety (design section 4.8): a cover and a numbered divider
    (reportlab Helvetica, as the converter and the overlay use) built beside
    converted DOCX pages leave every font the content selects whole."""
    docx = builders.docx(tmp_path / "doc.docx")
    converted = tmp_path / "doc.pdf"
    docx_converter.convert(str(docx), str(converted))
    packet = _packet(("Letter", [_file("i1", "hd", label="doc.docx")], True),
                     ("Again", [_file("i2", "hd", label="doc.docx")], True),
                     cover=_cover(), number_generated=True)
    reader, plan = _build(tmp_path, packet, {"hd": str(converted)})
    assert [p["kind"] for p in plan["pages"]][:3] == ["cover", "divider", "file"]
    for page in reader.pages:
        used = _used_fonts(page)
        assert used
        for _name, font in used:
            assert font is not None and "/Subtype" in font and "/BaseFont" in font
    assert _markers(reader)[:2] == ["Application", "Letter"]


# ---------------------------------------------------------------------------
# Stage B5: the contents page (design sections 2.1, 3.3, 4.8)
# ---------------------------------------------------------------------------
def _rows(page):
    """The contents page's lines as [(text, label)], top to bottom: the text
    shows on one baseline joined, and the label (the last show when it is a
    number) apart. The heading is a line with no label."""
    lines = {}
    for text, m in _texts(page):
        lines.setdefault(round(m[5], 1), []).append((m[4], text))
    out = []
    for y in sorted(lines, reverse=True):
        shows = [t for _x, t in sorted(lines[y])]
        label = int(shows.pop()) if len(shows) > 1 and _DIGITS.match(shows[-1]) else None
        out.append(("".join(shows), label))
    return out


def _links(reader, page):
    """[(rect, target page index)] for each internal link on ``page``."""
    out = []
    for ref in page.get("/Annots") or []:
        annot = ref.get_object()
        assert annot["/Subtype"] == "/Link"
        dest = annot["/Dest"]
        # A link within the file names the page object, not a number.
        assert hasattr(dest[0], "idnum"), dest
        out.append((tuple(float(v) for v in annot["/Rect"]),
                    reader.get_page_number(dest[0].get_object())))
    return out


def _stamped(page):
    """The page numbers stamped in the bottom margin (the contents page's own
    labels sit above it)."""
    return [n for n, m in _numbers(page) if m[5] < 72]


def _files(tmp_path, n, pages=1):
    return {f"h{k}": _pdf(tmp_path / f"f{k}.pdf", pages=pages, marker=f"F{k}") for k in range(n)}


def test_contents_lists_sections_and_files_and_links_each_to_its_first_page(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=2, marker="A")
    b = _pdf(tmp_path / "b.pdf", pages=3, marker="B")
    c = _pdf(tmp_path / "c.pdf", pages=1, marker="C")
    packet = _packet(("Resume", [_file("i1", "ha", label="resume.docx"), _blank("x")], True),
                     ("References", [_file("i2", "hb", label="refs.pdf"),
                                     _file("i3", "hc", label="letter.pdf")]),
                     cover=_cover(), contents=True)
    reader, plan = _build(tmp_path, packet, {"ha": a, "hb": b, "hc": c})
    kinds = [p["kind"] for p in plan["pages"]]
    assert kinds == ["cover", "contents", "divider", "file", "file", "blank",
                     "file", "file", "file", "file"]
    assert _rows(reader.pages[1]) == [
        ("Contents", None),
        ("Resume", 1), ("resume.docx", 2),
        ("References", 5), ("refs.pdf", 5), ("letter.pdf", 8),
    ]
    assert [t for _r, t in _links(reader, reader.pages[1])] == [2, 3, 6, 6, 9]
    assert _markers(reader)[3] == "A page 1" and _markers(reader)[9] == "C page 1"
    # Front matter: the contents page is not numbered, and the cover and the
    # contents carry no link but the contents rows.
    assert _stamped(reader.pages[1]) == []
    assert "/Annots" not in reader.pages[0]


def test_each_link_covers_its_row_inside_the_margins(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    packet = _packet(("A", [_file("i1", "ha")]), ("B", [_file("i2", "ha")]), contents=True)
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    page = reader.pages[0]
    links = _links(reader, page)
    rows = [(t, m) for t, m in _texts(page) if t != "Contents" and not _DIGITS.match(t)]
    assert len(links) == len(rows) == 4
    for ((left, bottom, right, top), _target), (_text, m) in zip(links, rows):
        assert (left, right) == (72, 612 - 72) and top - bottom == 20
        assert bottom < m[5] < top and left <= m[4] < right


def test_one_section_lists_its_files_alone_as_the_outline_does(tmp_path):
    """The page's heading rule: one section shows no heading, so the contents
    lists its files, flat, like the bookmarks."""
    a = _pdf(tmp_path / "a.pdf", pages=2)
    b = _pdf(tmp_path / "b.pdf", pages=1)
    packet = _packet(("", [_file("i1", "ha", label="one.pdf"), _file("i2", "hb", label="two.pdf")]),
                     contents=True, start_number=3)
    reader, _plan = _build(tmp_path, packet, {"ha": a, "hb": b})
    assert _rows(reader.pages[0]) == [("Contents", None), ("one.pdf", 3), ("two.pdf", 5)]
    texts = dict((t, m) for t, m in _texts(reader.pages[0]))
    assert texts["one.pdf"][4] == 72   # not indented
    assert [t for _r, t in _links(reader, reader.pages[0])] == [1, 3]
    assert _outline(reader) == [("one.pdf", 1, []), ("two.pdf", 3, [])]


def test_an_empty_section_still_makes_headings_and_untitled_reads_untitled(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    packet = _packet(("", [_file("i1", "ha", label="a.pdf")]), ("Empty", [], True), contents=True)
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    assert _rows(reader.pages[0]) == [("Contents", None), ("Untitled section", 1), ("a.pdf", 1)]
    texts = dict((t, m) for t, m in _texts(reader.pages[0]))
    assert texts["a.pdf"][4] == 72 + 18   # under its section


def test_a_long_contents_runs_to_two_pages_with_recto_and_a_start_number(tmp_path):
    """31 rows or more is two pages. The length is measured before the plan is
    fixed, so the recto pads and every link land where the plan says."""
    staged = _files(tmp_path, 40, pages=2)
    sections = [(f"Part {s}", [_file(f"i{s}_{k}", f"h{s * 10 + k}", label=f"doc-{s * 10 + k}.pdf")
                               for k in range(10)], s == 2) for s in range(4)]
    packet = _packet(*sections, cover=_cover(), contents=True, recto_starts=True,
                     start_number=7)
    reader, plan = _build(tmp_path, packet, staged)
    kinds = [p["kind"] for p in plan["pages"]]
    # cover, pad, two contents pages, then Part 0 on physical page 5.
    assert kinds[:5] == ["cover", "pad", "contents", "contents", "file"]
    assert len(reader.pages) == len(plan["pages"])
    first, second = _rows(reader.pages[2]), _rows(reader.pages[3])
    assert first[0] == ("Contents", None) and second[0] == ("Contents, continued", None)
    assert len(first) == 31 and len(second) == 44 - 30 + 1
    rows = first[1:] + second[1:]
    entries = plan["entries"]
    assert [r[0] for r in rows] == [e["title"] for e in entries]
    assert [r[1] for r in rows] == [e["label"] for e in entries]
    targets = [t for _r, t in _links(reader, reader.pages[2]) + _links(reader, reader.pages[3])]
    assert targets == [e["index"] for e in entries]
    for entry in entries:
        page = reader.pages[entry["index"]]
        if plan["pages"][entry["index"]]["kind"] == "file":
            item = entry["id"] if entry["kind"] == "item" else None
            if item:
                k = int(item.split("_")[0][1:]) * 10 + int(item.split("_")[1])
                assert _markers(reader)[entry["index"]] == f"F{k} page 1"
        if plan["pages"][entry["index"]]["print"]:
            assert _stamped(page) == [entry["label"]]
    assert entries[0]["label"] == 7
    assert _stamped(reader.pages[2]) == [] and _stamped(reader.pages[3]) == []


def test_links_survive_numbers_stamped_on_turned_pages(tmp_path):
    """Phase 2 stamps numbers through the inverse rotation; the links are
    added after it and name the pages the PDF keeps."""
    a = _pdf(tmp_path / "a.pdf", pages=2)
    packet = _packet(("", [_file("i1", "ha", label="turned.pdf", pages=_refs((1, 90), (0, 270)),
                                 src_pages=2),
                           _file("i2", "ha", label="again.pdf")]),
                     contents=True)
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    assert [p.rotation for p in reader.pages] == [0, 90, 270, 0, 0]
    assert [t for _r, t in _links(reader, reader.pages[0])] == [1, 3]
    assert [n for n, _m in _numbers(reader.pages[1])] == [1]
    # A second read-and-write keeps them (the destination is a page object).
    again = tmp_path / "again.pdf"
    writer = PdfWriter(clone_from=reader)
    with open(again, "wb") as fh:
        writer.write(fh)
    reread = PdfReader(str(again))
    assert [t for _r, t in _links(reread, reread.pages[0])] == [1, 3]


def test_contents_and_outline_agree_and_the_outline_still_nests(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    packet = _packet(("A", [_file("i1", "ha", label="x.pdf")], True),
                     ("B", [_file("i2", "ha", label="y.pdf")]), cover=_cover(), contents=True)
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    assert _outline(reader) == [("A", 2, [("x.pdf", 3, [])]), ("B", 4, [("y.pdf", 4, [])])]
    assert [t for _r, t in _links(reader, reader.pages[1])] == [2, 3, 4, 4]


def test_without_a_cover_the_contents_is_the_first_page(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    packet = _packet(("", [_file("i1", "ha")]), contents=True, bookmarks=False)
    reader, plan = _build(tmp_path, packet, {"ha": a})
    assert [p["kind"] for p in plan["pages"]] == ["contents", "file"]
    assert _rows(reader.pages[0]) == [("Contents", None), ("ha.pdf", 1)]
    assert [t for _r, t in _links(reader, reader.pages[0])] == [1]


@pytest.mark.parametrize("paper, size", [("letter", LETTER), ("a4", A4)])
def test_contents_is_the_packet_paper_and_holds_thirty_rows_on_either(tmp_path, paper, size):
    staged = _files(tmp_path, 31)
    packet = _packet(("", [_file(f"i{k}", f"h{k}") for k in range(31)]), contents=True,
                     paper=paper)
    reader, plan = _build(tmp_path, packet, staged)
    assert [p["kind"] for p in plan["pages"]][:3] == ["contents", "contents", "file"]
    assert _size(reader.pages[0]) == (round(size[0], 2), round(size[1], 2))
    assert len(_rows(reader.pages[0])) == 31 and len(_rows(reader.pages[1])) == 2
    for (left, bottom, right, top), _t in _links(reader, reader.pages[0]):
        assert 72 <= bottom and top <= size[1] - 72 and right == pytest.approx(size[0] - 72)


def test_a_long_title_is_cut_with_an_ellipsis_and_unprintable_text_is_a_box(tmp_path):
    a = _pdf(tmp_path / "a.pdf", pages=1)
    long = "Exhibit " + "word " * 200
    packet = _packet(("Łódź", [_file("i1", "ha", label=long)]),
                     ("B", [_file("i2", "ha", label="x\n\ty.pdf")]), contents=True)
    reader, _plan = _build(tmp_path, packet, {"ha": a})
    rows = _rows(reader.pages[0])
    # Ł and ź are ZapfDingbats boxes and ó is WinAnsi \363, as on the cover.
    assert sorted(rows[1][0]) == sorted("■ód■") and rows[1][1] == 1
    shows = re.findall(rb"\((.*?)\) Tj", reader.pages[0].get_contents().get_data())
    assert shows[1:4] == [b"n", b"\\363d", b"n"]
    title, label = rows[2]
    assert title.startswith("Exhibit word") and title.endswith("…") and label == 1
    from reportlab.pdfbase.pdfmetrics import stringWidth
    assert 90 + stringWidth(title, "Helvetica", 11) <= 612 - 72 - stringWidth("1", "Helvetica", 11)
    assert rows[4] == ("x y.pdf", 2)
    fonts = {str(f["/BaseFont"]) for f in reader.pages[0]["/Resources"]["/Font"].values()}
    assert "/ZapfDingbats" in fonts


def test_the_contents_length_depends_only_on_its_row_count():
    """Design section 3.3: measured before any page is placed. The measuring
    layout, the plan's own measure and the count rule agree for every count."""
    for rows in range(0, 200):
        entries = [{"kind": "item", "id": f"i{k}", "section": "s1", "title": "x" * (k % 50),
                    "label": 10 ** (k % 5), "index": k} for k in range(rows)]
        planned = {"pages": [], "entries": entries}
        packet = {"sections": [{"id": "s1"}]}
        assert packet_mod.contents_page_count(rows) == max(1, -(-rows // 30))
        for paper in ("letter", "a4"):
            assert packet_mod._measure_contents(planned, packet, {"paper": paper}) \
                == packet_mod.contents_page_count(rows)


def test_page_plan_measures_the_contents_when_no_length_is_given():
    vector = next(v for v in PLAN_FIXTURE["vectors"] if v["name"] == "contents-after-cover")
    assert vector["contents_pages"] == 2
    plan = page_plan(vector["packet"], vector["page_counts"])
    assert [p["kind"] for p in plan["pages"]] == ["cover", "contents", "file", "file"]
    counts = {f"h{k}": 1 for k in range(31)}
    packet = _packet(("", [_file(f"i{k}", f"h{k}") for k in range(31)]), contents=True)
    assert [p["kind"] for p in page_plan(packet, counts)["pages"]][:3] == \
        ["contents", "contents", "file"]
    # Two sections: 31 files and two headings are 33 rows.
    packet = _packet(("A", [_file(f"i{k}", f"h{k}") for k in range(29)]),
                     ("B", [_file("j", "h30")]), contents=True)
    assert sum(p["kind"] == "contents" for p in page_plan(packet, counts)["pages"]) == 2


def test_the_page_scripts_contents_rows_per_page_is_the_servers():
    source = (Path(__file__).resolve().parents[2] / "static" / "packet-model.js").read_text(encoding="utf-8")
    found = re.search(r"const CONTENTS_ROWS = (\d+);", source)
    assert found and int(found.group(1)) == packet_mod.CONTENTS_ROWS == 30


def test_the_contents_page_keeps_sound_fonts_beside_a_converted_docx(tmp_path, builders):
    docx = builders.docx(tmp_path / "doc.docx")
    converted = tmp_path / "doc.pdf"
    docx_converter.convert(str(docx), str(converted))
    packet = _packet(("Letter", [_file("i1", "hd", label="doc.docx")]),
                     ("Again", [_file("i2", "hd", label="doc.docx")]), contents=True)
    reader, plan = _build(tmp_path, packet, {"hd": str(converted)})
    assert plan["pages"][0]["kind"] == "contents"
    for page in reader.pages:
        for _name, font in _used_fonts(page):
            assert font is not None and "/Subtype" in font and "/BaseFont" in font


# ---------------------------------------------------------------------------
# B7: where the number sits (design section 3.3 rule 7)
# ---------------------------------------------------------------------------
LANDSCAPE = (792.0, 612.0)


def _extent(page, label):
    """The stamped number's text as displayed: (left, right, baseline), from
    the text matrix pypdf's visitor reports and Helvetica 10's advance widths.
    The number must be upright as displayed."""
    [(number, m)] = _numbers(page)
    assert number == label
    a_, b_, c_, d_, x, y = _shown(page, m)
    assert (round(a_, 3), round(b_, 3), round(c_, 3), round(d_, 3)) == (1, 0, 0, 1)
    return x, x + stringWidth(str(label), "Helvetica", 10), y


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
@pytest.mark.parametrize("size", [LETTER, A4, LANDSCAPE], ids=["letter", "a4", "landscape"])
@pytest.mark.parametrize("position", ["bottom-left", "bottom-center", "bottom-right"])
def test_the_stamped_number_sits_where_rule_7_puts_it(tmp_path, position, size, rot):
    src = _pdf(tmp_path / "s.pdf", pages=1, size=size)
    packet = _packet(("", [_file("i1", "hs", pages=_refs((0, rot)), src_pages=1)]),
                     number_position=position, start_number=128)
    reader, _plan = _build(tmp_path, packet, {"hs": src})
    page = reader.pages[0]
    width, _height = _shown_size(page)
    assert width == pytest.approx(size[1] if rot in (90, 270) else size[0], abs=0.01)
    left, right, baseline = _extent(page, 128)
    assert baseline == pytest.approx(30)
    if position == "bottom-left":
        assert left == pytest.approx(50)
    elif position == "bottom-right":
        assert left == pytest.approx(width - 50, abs=0.01)
    else:
        # [C10]: centred on the midline, within 1 pt.
        assert abs((left + right) / 2 - width / 2) <= 1
        assert left == pytest.approx(width / 2 - stringWidth("128", "Helvetica", 10) / 2,
                                     abs=0.01)


def test_bottom_center_is_centred_on_a_source_rotated_page_and_its_blank(tmp_path):
    src = _pdf(tmp_path / "s.pdf", pages=1, size=A4, rotate=90)
    packet = _packet(("", [_file("i1", "hs"), _blank("b1")]),
                     number_position="bottom-center", start_number=7)
    reader, _plan = _build(tmp_path, packet, {"hs": src})
    for page, label in zip(reader.pages, (7, 8)):
        assert page.rotation == 90
        width, _height = _shown_size(page)
        assert width == 841.89
        left, right, baseline = _extent(page, label)
        assert abs((left + right) / 2 - width / 2) <= 1 and baseline == pytest.approx(30)


def _cropped(path, size, crop, media=None):
    """A one-page PDF of ``size`` with its crop box set to ``crop``
    ((x0, y0, x1, y1)), and its media box moved to ``media`` when given."""
    _pdf(path, pages=1, size=size)
    reader = PdfReader(str(path))
    writer = PdfWriter()
    page = writer.add_page(reader.pages[0])
    if media:
        page.mediabox.lower_left = media[:2]
        page.mediabox.upper_right = media[2:]
    page.cropbox.lower_left = crop[:2]
    page.cropbox.upper_right = crop[2:]
    with open(path, "wb") as fh:
        writer.write(fh)
    return str(path)


def test_a_crop_box_larger_than_the_media_box_is_clipped_to_it(tmp_path):
    """Viewers and pdfium show the crop box clipped to the media box, and the
    stage's sizes say the same (section 4.2), so the stamp places by it."""
    path = _cropped(tmp_path / "c.pdf", LETTER, (-100, -100, 700, 900))
    packet = _packet(("", [_file("i1", "hc")]), number_position="bottom-center")
    reader, _plan = _build(tmp_path, packet, {"hc": path})
    [(_n, m)] = _numbers(reader.pages[0])
    assert m[4] + stringWidth("1", "Helvetica", 10) / 2 == pytest.approx(306)
    assert m[5] == pytest.approx(30)


def test_a_blank_after_a_cropped_page_is_placed_by_the_media_box(tmp_path):
    path = _cropped(tmp_path / "c.pdf", LETTER, (100, 150, 500, 700))
    packet = _packet(("", [_file("i1", "hc"), _blank("b1")]), number_position="bottom-right")
    reader, _plan = _build(tmp_path, packet, {"hc": path})
    blank = reader.pages[1]
    assert _size(blank) == (612.0, 792.0)
    left, _right, baseline = _extent(blank, 2)
    assert left == pytest.approx(562) and baseline == pytest.approx(30)
