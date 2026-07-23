"""Tests for the interleave merge mode (front/back scan zipping).

Reuses the ``builders`` / ``client`` fixtures from conftest.py. Source PDFs are
built with distinguishable per-page text ("<marker> page <n>") so the exact
interleaved page order can be asserted by text extraction.
"""
import io
import re

import pytest
from PyPDF2 import PdfReader

from pdf_ops.merge import merge_pipeline, parse_options, OptionsError


# Same page-number overlay matcher as test_merge_options: numbers are drawn at
# the y=30 baseline, isolating them from source-document digits.
_NUM_RE = re.compile(rb'1 0 0 1 ([0-9.]+) 30 Tm\s*\((\d+)\) Tj')


def _stamped(reader):
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


def _page_texts(reader):
    return [(p.extract_text() or "") for p in reader.pages]


def _sources(tmp_path, builders, spec):
    """spec: list of (title, page_count, marker) -> list of (path, title)."""
    out = []
    for title, pages, marker in spec:
        p = builders.pdf(tmp_path / f"{title}.pdf", pages=pages, marker=marker)
        out.append((str(p), title))
    return out


def _interleave(sources, **overrides):
    opts = {"mode": "interleave", "page_numbers": False}
    opts.update(overrides)
    return merge_pipeline(sources, opts)


# ---------------------------------------------------------------------------
# Core interleave ordering
# ---------------------------------------------------------------------------
def test_even_even_reverse_off_zips_in_order(tmp_path, builders):
    sources = _sources(
        tmp_path, builders, [("fronts", 2, "FRONT"), ("backs", 2, "BACK")]
    )
    writer = _interleave(sources, reverse_second=False)
    reader = _render(writer, tmp_path)
    texts = _page_texts(reader)

    assert len(texts) == 4
    assert "FRONT page 1" in texts[0]
    assert "BACK page 1" in texts[1]
    assert "FRONT page 2" in texts[2]
    assert "BACK page 2" in texts[3]


def test_even_even_reverse_on_flips_back_stack(tmp_path, builders):
    sources = _sources(
        tmp_path, builders, [("fronts", 2, "FRONT"), ("backs", 2, "BACK")]
    )
    writer = _interleave(sources, reverse_second=True)
    reader = _render(writer, tmp_path)
    texts = _page_texts(reader)

    assert len(texts) == 4
    # Backs are reversed before pairing: BACK page 2 pairs with FRONT page 1.
    assert "FRONT page 1" in texts[0]
    assert "BACK page 2" in texts[1]
    assert "FRONT page 2" in texts[2]
    assert "BACK page 1" in texts[3]


def test_reverse_second_defaults_to_true(tmp_path, builders):
    # Omitting reverse_second should behave like reverse_second=True.
    sources = _sources(
        tmp_path, builders, [("fronts", 2, "FRONT"), ("backs", 2, "BACK")]
    )
    reader = _render(_interleave(sources), tmp_path)
    texts = _page_texts(reader)
    assert "BACK page 2" in texts[1]  # reversed default


def test_odd_a_one_extra_front_lands_last(tmp_path, builders):
    # 3 fronts, 2 backs: the extra final front has no back partner.
    sources = _sources(
        tmp_path, builders, [("fronts", 3, "FRONT"), ("backs", 2, "BACK")]
    )
    writer = _interleave(sources, reverse_second=False)
    reader = _render(writer, tmp_path)
    texts = _page_texts(reader)

    assert len(texts) == 5
    assert "FRONT page 1" in texts[0]
    assert "BACK page 1" in texts[1]
    assert "FRONT page 2" in texts[2]
    assert "BACK page 2" in texts[3]
    assert "FRONT page 3" in texts[4]  # leftover front, no back


def test_odd_a_reversed_backs(tmp_path, builders):
    sources = _sources(
        tmp_path, builders, [("fronts", 3, "FRONT"), ("backs", 2, "BACK")]
    )
    writer = _interleave(sources, reverse_second=True)
    reader = _render(writer, tmp_path)
    texts = _page_texts(reader)

    assert len(texts) == 5
    assert "FRONT page 1" in texts[0]
    assert "BACK page 2" in texts[1]
    assert "FRONT page 2" in texts[2]
    assert "BACK page 1" in texts[3]
    assert "FRONT page 3" in texts[4]


# ---------------------------------------------------------------------------
# blank_pages is ignored in interleave mode
# ---------------------------------------------------------------------------
def test_blank_pages_ignored_in_interleave(tmp_path, builders):
    # Two 1-page (odd) sources: standard mode would pad to 4; interleave must
    # NOT pad (padding would desync fronts from backs) -> exactly 2 pages.
    sources = _sources(
        tmp_path, builders, [("fronts", 1, "FRONT"), ("backs", 1, "BACK")]
    )
    writer = _interleave(sources, blank_pages=True)
    reader = _render(writer, tmp_path)
    assert len(reader.pages) == 2


# ---------------------------------------------------------------------------
# Page-count mismatch
# ---------------------------------------------------------------------------
def test_mismatch_beyond_one_page_raises(tmp_path, builders):
    sources = _sources(
        tmp_path, builders, [("fronts", 3, "FRONT"), ("backs", 1, "BACK")]
    )
    with pytest.raises(OptionsError):
        _interleave(sources)


def test_wrong_source_count_raises(tmp_path, builders):
    one = _sources(tmp_path, builders, [("solo", 2, "SOLO")])
    with pytest.raises(OptionsError):
        _interleave(one)

    three = _sources(
        tmp_path,
        builders,
        [("a", 1, "A"), ("b", 1, "B"), ("c", 1, "C")],
    )
    with pytest.raises(OptionsError):
        _interleave(three)


# ---------------------------------------------------------------------------
# Bookmarks in interleave mode
# ---------------------------------------------------------------------------
def test_bookmarks_target_first_page_of_each_source(tmp_path, builders):
    sources = _sources(
        tmp_path, builders, [("fronts", 2, "FRONT"), ("backs", 2, "BACK")]
    )
    writer = _interleave(sources, bookmarks=True)
    reader = _render(writer, tmp_path)

    outline = reader.outline
    titles = [item.title for item in outline]
    targets = [reader.get_destination_page_number(item) for item in outline]
    assert titles == ["fronts", "backs"]
    # Fronts' first page is index 0; backs' first page is index 1.
    assert targets == [0, 1]


def test_bookmarks_backs_target_is_index_1_even_when_reversed(tmp_path, builders):
    sources = _sources(
        tmp_path, builders, [("fronts", 2, "FRONT"), ("backs", 2, "BACK")]
    )
    writer = _interleave(sources, bookmarks=True, reverse_second=True)
    reader = _render(writer, tmp_path)
    targets = [
        reader.get_destination_page_number(item) for item in reader.outline
    ]
    assert targets == [0, 1]


def test_bookmarks_false_yields_empty_outline(tmp_path, builders):
    sources = _sources(
        tmp_path, builders, [("fronts", 2, "FRONT"), ("backs", 2, "BACK")]
    )
    writer = _interleave(sources, bookmarks=False)
    reader = _render(writer, tmp_path)
    assert len(reader.outline) == 0


# ---------------------------------------------------------------------------
# Page numbering across the interleaved sequence
# ---------------------------------------------------------------------------
def test_numbering_applies_across_interleaved_sequence(tmp_path, builders):
    sources = _sources(
        tmp_path, builders, [("fronts", 2, "FRONT"), ("backs", 2, "BACK")]
    )
    writer = merge_pipeline(
        sources, {"mode": "interleave", "page_numbers": True, "reverse_second": False}
    )
    reader = _render(writer, tmp_path)
    nums = sorted(n for _x, n in _stamped(reader))
    assert nums == [1, 2, 3, 4]


def test_numbering_start_offset_in_interleave(tmp_path, builders):
    sources = _sources(
        tmp_path, builders, [("fronts", 2, "FRONT"), ("backs", 2, "BACK")]
    )
    writer = merge_pipeline(
        sources,
        {"mode": "interleave", "page_numbers": True, "start_number": 10},
    )
    reader = _render(writer, tmp_path)
    nums = sorted(n for _x, n in _stamped(reader))
    assert nums == [10, 11, 12, 13]


# ---------------------------------------------------------------------------
# Option parsing
# ---------------------------------------------------------------------------
def test_parse_options_interleave_includes_mode_and_reverse():
    opts = parse_options({"mode": "interleave"})
    assert opts["mode"] == "interleave"
    assert opts["reverse_second"] is True  # default


def test_parse_options_interleave_reverse_false():
    opts = parse_options({"mode": "interleave", "reverse_second": "false"})
    assert opts["reverse_second"] is False


def test_parse_options_standard_omits_interleave_keys():
    # Standard mode must not leak the extra keys (keeps the historical shape).
    opts = parse_options({"mode": "standard"})
    assert "mode" not in opts
    assert "reverse_second" not in opts


def test_parse_options_rejects_bad_mode():
    with pytest.raises(OptionsError):
        parse_options({"mode": "sideways"})


# ---------------------------------------------------------------------------
# Route integration
# ---------------------------------------------------------------------------
def _upload(client, files, form=None):
    data = {"files[]": [(io.BytesIO(content), name) for name, content in files]}
    if form:
        data.update(form)
    return client.post("/upload", data=data, content_type="multipart/form-data")


def _two_pdfs(tmp_path, builders):
    a = builders.pdf(tmp_path / "fronts.pdf", pages=2, marker="FRONT")
    b = builders.pdf(tmp_path / "backs.pdf", pages=2, marker="BACK")
    return [
        ("fronts.pdf", a.read_bytes()),
        ("backs.pdf", b.read_bytes()),
    ]


def test_route_interleave_happy_path(client, tmp_path, builders):
    resp = _upload(
        client,
        _two_pdfs(tmp_path, builders),
        {"mode": "interleave", "reverse_second": "false", "page_numbers": "false"},
    )
    assert resp.status_code == 200, resp.data
    filename = resp.get_json()["filename"]
    reader = PdfReader(str(client.output_dir / filename))
    texts = _page_texts(reader)
    assert len(texts) == 4
    assert "FRONT page 1" in texts[0]
    assert "BACK page 1" in texts[1]
    assert "FRONT page 2" in texts[2]
    assert "BACK page 2" in texts[3]


def test_route_interleave_one_file_returns_400(client, tmp_path, builders):
    a = builders.pdf(tmp_path / "solo.pdf", pages=2, marker="SOLO")
    resp = _upload(
        client,
        [("solo.pdf", a.read_bytes())],
        {"mode": "interleave"},
    )
    assert resp.status_code == 400
    assert "2 files" in resp.get_json()["error"]


def test_route_interleave_three_files_returns_400(client, tmp_path, builders):
    files = _two_pdfs(tmp_path, builders)
    c = builders.pdf(tmp_path / "extra.pdf", pages=2, marker="EXTRA")
    files.append(("extra.pdf", c.read_bytes()))
    resp = _upload(client, files, {"mode": "interleave"})
    assert resp.status_code == 400
    assert "2 files" in resp.get_json()["error"]


def test_route_interleave_page_mismatch_returns_400(client, tmp_path, builders):
    a = builders.pdf(tmp_path / "fronts.pdf", pages=3, marker="FRONT")
    b = builders.pdf(tmp_path / "backs.pdf", pages=1, marker="BACK")
    resp = _upload(
        client,
        [("fronts.pdf", a.read_bytes()), ("backs.pdf", b.read_bytes())],
        {"mode": "interleave"},
    )
    assert resp.status_code == 400
    assert "pages" in resp.get_json()["error"].lower()


def test_route_invalid_mode_returns_400(client, tmp_path, builders):
    resp = _upload(
        client,
        _two_pdfs(tmp_path, builders),
        {"mode": "diagonal"},
    )
    assert resp.status_code == 400
    assert "mode" in resp.get_json()["error"].lower()


def test_route_standard_mode_still_default(client, tmp_path, builders):
    # No mode field -> standard concatenation with the historical blank padding.
    a = builders.pdf(tmp_path / "a.pdf", pages=1, marker="A")
    b = builders.pdf(tmp_path / "b.pdf", pages=1, marker="B")
    resp = _upload(
        client,
        [("a.pdf", a.read_bytes()), ("b.pdf", b.read_bytes())],
    )
    assert resp.status_code == 200, resp.data
    filename = resp.get_json()["filename"]
    reader = PdfReader(str(client.output_dir / filename))
    # Two odd (1-page) sources each get a blank -> 4 pages in standard mode.
    assert len(reader.pages) == 4
