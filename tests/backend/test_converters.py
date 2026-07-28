"""Unit tests for each converter plugin and the converter registry."""
import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import letter

LETTER_W, LETTER_H = letter  # (612.0, 792.0)
SIZE_TOL = 2.0

from converters import (
    ConversionError,
    get_converter,
    supported_extensions,
)
from converters import (
    docx_converter,
    html_converter,
    image_converter,
    markdown_converter,
    text_converter,
)


def _read(pdf_path):
    return PdfReader(str(pdf_path))


def _all_text(pdf_path):
    return "\n".join(p.extract_text() or "" for p in _read(pdf_path).pages)


# --------------------------------------------------------------------------
# Valid conversions produce a valid, >=1 page PDF
# --------------------------------------------------------------------------
def test_markdown_produces_valid_pdf(tmp_path, builders):
    src = builders.markdown(tmp_path / "doc.md")
    out = tmp_path / "doc.pdf"
    markdown_converter.convert(str(src), str(out))
    reader = _read(out)
    assert len(reader.pages) >= 1
    text = _all_text(out)
    assert builders.MD_HEADING in text
    assert builders.MD_PARAGRAPH in text
    assert builders.MD_CODE in text
    assert builders.MD_TABLE_CELL in text


def test_text_produces_valid_pdf_and_spans_pages(tmp_path, builders):
    src = builders.text_rich(tmp_path / "doc.txt")
    out = tmp_path / "doc.pdf"
    text_converter.convert(str(src), str(out))
    reader = _read(out)
    # 300 short lines + a 6000-char wrapped line => comfortably more than 2 pages.
    assert len(reader.pages) > 2
    assert builders.TXT_MARKER in _all_text(out)


def test_text_long_unbroken_line_wraps_across_pages(tmp_path):
    # One single 6000-char line, no newlines: must wrap onto multiple pages.
    src = tmp_path / "long.txt"
    src.write_text("W" * 6000 + "\n", encoding="utf-8")
    out = tmp_path / "long.pdf"
    text_converter.convert(str(src), str(out))
    assert len(_read(out).pages) >= 2


def test_html_produces_valid_pdf(tmp_path, builders):
    src = builders.html(tmp_path / "doc.html")
    out = tmp_path / "doc.pdf"
    html_converter.convert(str(src), str(out))
    reader = _read(out)
    assert len(reader.pages) >= 1
    text = _all_text(out)
    assert builders.HTML_HEADING in text
    assert builders.HTML_BODY in text


def test_docx_produces_valid_pdf(tmp_path, builders):
    src = builders.docx(tmp_path / "doc.docx")
    out = tmp_path / "doc.pdf"
    docx_converter.convert(str(src), str(out))
    reader = _read(out)
    assert len(reader.pages) >= 1
    text = _all_text(out)
    assert builders.DOCX_HEADING in text
    assert builders.DOCX_BODY in text
    assert builders.DOCX_SECOND in text


# --------------------------------------------------------------------------
# Image converter: letter-sized pages, multi-frame -> multiple pages
# --------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["png", "jpg"])
def test_image_single_frame_is_letter_sized(tmp_path, builders, kind):
    src = getattr(builders, kind)(tmp_path / f"img.{kind}")
    out = tmp_path / f"img_{kind}.pdf"
    image_converter.convert(str(src), str(out))
    reader = _read(out)
    assert len(reader.pages) == 1
    page = reader.pages[0]
    assert abs(float(page.mediabox.width) - LETTER_W) <= SIZE_TOL
    assert abs(float(page.mediabox.height) - LETTER_H) <= SIZE_TOL


def test_multiframe_tiff_produces_multiple_pages(tmp_path, builders):
    src = builders.tiff(tmp_path / "multi.tiff", frames=3)
    out = tmp_path / "multi.pdf"
    image_converter.convert(str(src), str(out))
    assert len(_read(out).pages) == 3


# --------------------------------------------------------------------------
# Error paths: garbage bytes
# --------------------------------------------------------------------------
def test_docx_garbage_raises_conversion_error(tmp_path):
    src = tmp_path / "bad.docx"
    src.write_bytes(b"\x00\x01 this is not a zip / docx " * 20)
    with pytest.raises(ConversionError):
        docx_converter.convert(str(src), str(tmp_path / "out.pdf"))


def test_image_garbage_raises_conversion_error(tmp_path):
    src = tmp_path / "bad.png"
    src.write_bytes(b"\x89PNG not really an image at all " * 20)
    with pytest.raises(ConversionError):
        image_converter.convert(str(src), str(tmp_path / "out.pdf"))


# NOTE (app behavior): the HTML and Markdown converters are intentionally
# lenient -- xhtml2pdf/markdown will render arbitrary bytes as document text
# rather than failing, so garbage input does NOT raise ConversionError for
# them. We therefore only assert the garbage-raise contract for docx/image
# (which explicitly validate their input), matching current behavior.
def test_html_garbage_does_not_raise(tmp_path):
    src = tmp_path / "bad.html"
    src.write_bytes(b"\x01\x02 arbitrary bytes rendered as text " * 20)
    out = tmp_path / "out.pdf"
    # Documents current behavior: renders rather than raising.
    html_converter.convert(str(src), str(out))
    assert out.exists() and out.stat().st_size > 0


# --------------------------------------------------------------------------
# Error paths: missing input file -> every converter raises ConversionError
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "module, name",
    [
        (markdown_converter, "missing.md"),
        (text_converter, "missing.txt"),
        (html_converter, "missing.html"),
        (docx_converter, "missing.docx"),
        (image_converter, "missing.png"),
    ],
)
def test_missing_input_raises_conversion_error(tmp_path, module, name):
    missing = tmp_path / name
    with pytest.raises(ConversionError):
        module.convert(str(missing), str(tmp_path / "out.pdf"))


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
def test_supported_extensions_contains_all_expected():
    exts = set(supported_extensions())
    expected = {
        ".md",
        ".markdown",
        ".txt",
        ".html",
        ".htm",
        ".docx",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
        ".tiff",
        ".tif",
    }
    assert expected <= exts
    # .pdf is handled directly by the app, not by a converter plugin.
    assert ".pdf" not in exts


def test_get_converter_is_case_insensitive():
    assert get_converter(".PNG") is get_converter(".png")
    assert get_converter(".Md") is get_converter(".md")
    assert get_converter(".DOCX") is not None


def test_get_converter_unknown_extension_returns_none():
    assert get_converter(".xyz") is None
    assert get_converter(".pdf") is None
