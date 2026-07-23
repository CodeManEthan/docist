"""Tests for the PDF bridge transform plugin (``transforms/pdf_bridge.py``).

The bridge supplies both halves of the ``.pdf`` pivot: ``X -> .pdf`` for every
convertible extension, and ``.pdf -> {png, jpg, txt}``. These tests exercise the
plugin *through the public transform registry interface* wherever possible, plus
the pivot composition that is the whole reason the bridge exists.

Ownership note: concurrent agents own ``transforms/{images,documents,data}.py``.
These tests never assert anything that *requires* those modules -- pivot-target
assertions use subset checks, and the pivot pairs chosen (``.svg -> .png``,
``.md -> .jpg``) are ones ONLY the bridge can enable via the ``.pdf`` pivot.
"""
import zipfile

import pytest
from PIL import Image
from PyPDF2 import PdfReader

import transforms
from transforms import TransformError
import transforms.pdf_bridge as bridge


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _read_pdf_text(path):
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _write_svg(path):
    """A minimal valid SVG (positive dimensions so svg_converter accepts it)."""
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="90">'
        '<rect x="0" y="0" width="120" height="90" fill="#2288cc"/>'
        '<circle cx="60" cy="45" r="30" fill="#ffcc00"/>'
        "</svg>",
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------
# Bridge surface / registration
# --------------------------------------------------------------------------
def test_bridge_registers_every_convertible_extension_to_pdf():
    """Every converters.supported_extensions() ext has an (ext, .pdf) pair."""
    import converters

    exts = set(converters.supported_extensions())
    assert exts, "expected at least one convertible extension"
    for ext in exts:
        assert (ext, ".pdf") in bridge.TRANSFORMS, f"missing {ext} -> .pdf"


def test_bridge_registers_pdf_export_pairs():
    for pair in ((".pdf", ".png"), (".pdf", ".jpg"), (".pdf", ".txt")):
        assert pair in bridge.TRANSFORMS


# --------------------------------------------------------------------------
# X -> PDF spot checks (through the transform interface)
# --------------------------------------------------------------------------
def test_md_to_pdf_via_transform(tmp_path, builders):
    src = builders.markdown(tmp_path / "doc.md")
    out = tmp_path / "doc.pdf"
    fn = transforms.get_transform(".md", ".pdf")
    assert fn is not None
    result = fn(str(src), str(out))
    assert result == str(out)
    reader = PdfReader(str(out))
    assert len(reader.pages) >= 1


def test_png_to_pdf_via_transform(tmp_path, builders):
    src = builders.png(tmp_path / "img.png")
    out = tmp_path / "img.pdf"
    fn = transforms.get_transform(".png", ".pdf")
    assert fn is not None
    fn(str(src), str(out))
    reader = PdfReader(str(out))
    assert len(reader.pages) >= 1


def test_csv_to_pdf_via_transform(tmp_path):
    src = tmp_path / "data.csv"
    src.write_text("name,score\nalice,10\nbob,20\n", encoding="utf-8")
    out = tmp_path / "data.pdf"
    fn = transforms.get_transform(".csv", ".pdf")
    assert fn is not None
    fn(str(src), str(out))
    reader = PdfReader(str(out))
    assert len(reader.pages) >= 1


def test_to_pdf_wraps_conversion_error(tmp_path):
    """A converter ConversionError surfaces as TransformError (message chained)."""
    bad = tmp_path / "broken.svg"
    bad.write_text("this is not svg at all", encoding="utf-8")
    out = tmp_path / "broken.pdf"
    fn = transforms.get_transform(".svg", ".pdf")
    assert fn is not None
    with pytest.raises(TransformError):
        fn(str(bad), str(out))


# --------------------------------------------------------------------------
# PDF -> images: single vs multi page policy
# --------------------------------------------------------------------------
def test_pdf_to_png_single_page(tmp_path, builders):
    src = builders.pdf(tmp_path / "one.pdf", pages=1)
    out = tmp_path / "one.png"
    fn = transforms.get_transform(".pdf", ".png")
    result = fn(str(src), str(out))
    assert result == str(out)
    assert out.exists()
    with Image.open(out) as im:
        assert im.format == "PNG"


def test_pdf_to_png_multi_page_returns_zip(tmp_path, builders):
    src = builders.pdf(tmp_path / "many.pdf", pages=3)
    out = tmp_path / "many.png"
    fn = transforms.get_transform(".pdf", ".png")
    result = fn(str(src), str(out))
    # Multi-page => a .zip next to output_path with the same stem.
    assert result == str(tmp_path / "many.zip")
    assert zipfile.is_zipfile(result)
    with zipfile.ZipFile(result) as zf:
        assert len(zf.namelist()) == 3
        assert all(name.endswith(".png") for name in zf.namelist())


def test_pdf_to_jpg_single_page(tmp_path, builders):
    src = builders.pdf(tmp_path / "one.pdf", pages=1)
    out = tmp_path / "one.jpg"
    fn = transforms.get_transform(".pdf", ".jpg")
    result = fn(str(src), str(out))
    assert result == str(out)
    with Image.open(out) as im:
        assert im.format == "JPEG"


def test_pdf_to_jpg_multi_page_returns_zip(tmp_path, builders):
    src = builders.pdf(tmp_path / "many.pdf", pages=4)
    out = tmp_path / "many.jpg"
    fn = transforms.get_transform(".pdf", ".jpg")
    result = fn(str(src), str(out))
    assert result == str(tmp_path / "many.zip")
    with zipfile.ZipFile(result) as zf:
        assert len(zf.namelist()) == 4
        assert all(name.endswith(".jpg") for name in zf.namelist())


def test_pdf_to_txt_has_text_and_page_separators(tmp_path, builders):
    marker = "BridgeTxtMarker"
    src = builders.pdf(tmp_path / "doc.pdf", pages=2, marker=marker)
    out = tmp_path / "doc.txt"
    fn = transforms.get_transform(".pdf", ".txt")
    result = fn(str(src), str(out))
    assert result == str(out)
    text = out.read_text(encoding="utf-8")
    assert marker in text
    assert "--- Page 1 ---" in text
    assert "--- Page 2 ---" in text
    assert "\f" in text  # form-feed page separator


# --------------------------------------------------------------------------
# PIVOT integration -- the whole point of the bridge
# --------------------------------------------------------------------------
def test_pivot_md_to_png(tmp_path, builders):
    """.md -> .png is only reachable by composing md->pdf and pdf->png."""
    src = builders.markdown(tmp_path / "doc.md")
    out = tmp_path / "doc.png"
    fn = transforms.get_transform(".md", ".png")
    assert fn is not None
    result = fn(str(src), str(out))
    # A short markdown doc renders to a single page -> a real PNG.
    assert result == str(out)
    with Image.open(out) as im:
        assert im.format == "PNG"


def test_pivot_md_to_jpg(tmp_path, builders):
    src = builders.markdown(tmp_path / "doc.md")
    out = tmp_path / "doc.jpg"
    fn = transforms.get_transform(".md", ".jpg")
    assert fn is not None
    result = fn(str(src), str(out))
    with Image.open(result) as im:
        assert im.format == "JPEG"


def test_pivot_svg_to_png(tmp_path):
    """.svg -> .png: a pivot ONLY the bridge enables (svg->pdf + pdf->png)."""
    src = _write_svg(tmp_path / "pic.svg")
    out = tmp_path / "pic.png"
    fn = transforms.get_transform(".svg", ".png")
    assert fn is not None
    result = fn(str(src), str(out))
    with Image.open(result) as im:
        assert im.format == "PNG"


def test_targets_for_md_includes_pivot_image_and_text(tmp_path):
    """png/jpg/txt are reachable from .md (png/jpg only via the pivot)."""
    targets = set(transforms.targets_for(".md"))
    assert {".png", ".jpg", ".txt"} <= targets


def test_matrix_lists_pdf_as_a_source():
    m = transforms.matrix()
    assert ".pdf" in m
    assert {".png", ".jpg", ".txt"} <= set(m[".pdf"])


# --------------------------------------------------------------------------
# Corrupt / invalid PDF input
# --------------------------------------------------------------------------
def test_corrupt_pdf_to_png_raises_transform_error(tmp_path):
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"%PDF-1.4 not really a pdf \x00\x01\x02 garbage")
    out = tmp_path / "corrupt.png"
    fn = transforms.get_transform(".pdf", ".png")
    with pytest.raises(TransformError):
        fn(str(bad), str(out))


def test_corrupt_pdf_to_txt_raises_transform_error(tmp_path):
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"not a pdf at all")
    out = tmp_path / "corrupt.txt"
    fn = transforms.get_transform(".pdf", ".txt")
    with pytest.raises(TransformError):
        fn(str(bad), str(out))
