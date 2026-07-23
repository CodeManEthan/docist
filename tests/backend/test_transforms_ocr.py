"""Tests for the OCR image-to-text transform plugin (transforms/ocr_text.py).

Fixtures are generated programmatically: text is drawn onto a high-contrast
raster with Pillow (large font, black on white) and recovered via Tesseract.
All OCR here is real -- Tesseract 5.x is expected to be installed system-wide.
A handful of OCR calls keeps runtime modest.

These tests also lock in the *no-shadowing* contract: this plugin registers
only ``(image_ext, '.txt')`` pairs, so ``('.pdf', '.txt')`` must still resolve
to the bridge's embedded-text extraction (no OCR), and the two plugins' pair
sets must be disjoint.
"""
import os
import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

import transforms
from transforms import TransformError, ocr_text, pdf_bridge
from pdf_ops.ocr import is_available


# Skip the OCR-execution tests when Tesseract is not installed; registry and
# gate tests below run regardless (they never actually invoke Tesseract).
requires_tesseract = pytest.mark.skipif(
    not is_available(), reason="Tesseract/Ghostscript not installed"
)


# --------------------------------------------------------------------------
# Fixture builders
# --------------------------------------------------------------------------
_FONT_CANDIDATES = [
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/google-noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]


def _load_font(size=60):
    """A real TrueType font at ``size`` px; skip the test if none is present."""
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    # Fall back to fontconfig's default sans if the well-known paths miss.
    try:
        import subprocess
        path = subprocess.check_output(
            ["fc-match", "-f", "%{file}", "sans"], text=True
        ).strip()
        if path and os.path.exists(path):
            return ImageFont.truetype(path, size)
    except Exception:
        pass
    pytest.skip("no usable TrueType font found for OCR fixtures")


def _draw_text_image(mode="RGB", background="white", text="OcrHello123",
                     size=(1200, 400)):
    """High-contrast raster with ``text`` drawn in large black glyphs."""
    if mode == "RGBA":
        # Transparent background -> exercises the alpha-flatten path.
        bg = (255, 255, 255, 0) if background == "transparent" else background
        img = Image.new("RGBA", size, bg)
        fill = (0, 0, 0, 255)
    else:
        img = Image.new(mode, size, background)
        fill = (0, 0, 0)
    draw = ImageDraw.Draw(img)
    draw.text((40, 150), text, fill=fill, font=_load_font(60))
    return img


def _run(src_ext, dst_ext, in_path, out_path):
    fn = transforms.get_transform(src_ext, dst_ext)
    assert fn is not None, f"no transform for {src_ext} -> {dst_ext}"
    return fn(str(in_path), str(out_path))


# --------------------------------------------------------------------------
# OCR round-trips
# --------------------------------------------------------------------------
@requires_tesseract
def test_png_to_txt_recovers_text(tmp_path):
    text = "OcrHelloPNG"
    src = tmp_path / "in.png"
    _draw_text_image(text=text).save(src, "PNG")
    out = tmp_path / "out.txt"
    result = _run(".png", ".txt", src, out)
    assert result == str(out)
    assert text in out.read_text(encoding="utf-8").replace(" ", "")


@requires_tesseract
def test_jpg_to_txt_recovers_text(tmp_path):
    text = "OcrHelloJPG"
    src = tmp_path / "in.jpg"
    _draw_text_image(text=text).save(src, "JPEG", quality=95)
    out = tmp_path / "out.txt"
    _run(".jpg", ".txt", src, out)
    assert text in out.read_text(encoding="utf-8").replace(" ", "")


@requires_tesseract
def test_rgba_transparent_background_ocrs_after_flatten(tmp_path):
    """Transparent-background RGBA still recovers text once flattened to white."""
    text = "OcrAlphaFlat"
    src = tmp_path / "in.png"
    _draw_text_image(mode="RGBA", background="transparent", text=text).save(
        src, "PNG"
    )
    out = tmp_path / "out.txt"
    _run(".png", ".txt", src, out)
    assert text in out.read_text(encoding="utf-8").replace(" ", "")


@requires_tesseract
def test_blank_image_yields_empty_text_no_error(tmp_path):
    src = tmp_path / "blank.png"
    Image.new("RGB", (800, 400), "white").save(src, "PNG")
    out = tmp_path / "out.txt"
    result = _run(".png", ".txt", src, out)
    assert result == str(out)
    # Blank -> no recognisable glyphs; whitespace/form-feed only, not an error.
    assert out.read_text(encoding="utf-8").strip() == ""


@requires_tesseract
def test_multiframe_tiff_has_frame_separators(tmp_path):
    """A 2-frame TIFF OCRs each frame and joins with form-feed + headers."""
    f1 = _draw_text_image(text="ALPHA")
    f2 = _draw_text_image(text="BRAVO")
    src = tmp_path / "multi.tiff"
    f1.save(src, "TIFF", save_all=True, append_images=[f2])
    out = tmp_path / "out.txt"
    _run(".tiff", ".txt", src, out)
    body = out.read_text(encoding="utf-8")
    assert "--- Frame 1 ---" in body
    assert "--- Frame 2 ---" in body
    assert "\f" in body  # form-feed frame separator
    collapsed = body.replace(" ", "")
    assert "ALPHA" in collapsed
    assert "BRAVO" in collapsed


def test_corrupt_image_raises_transformerror(tmp_path, monkeypatch):
    """Undecodable bytes with an image extension -> TransformError (not crash)."""
    # Guarantee the availability gate is open so we reach the decode step even
    # on machines without Tesseract installed.
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"this is not a real PNG file at all")
    out = tmp_path / "out.txt"
    fn = transforms.get_transform(".png", ".txt")
    with pytest.raises(TransformError):
        fn(str(bad), str(out))


# --------------------------------------------------------------------------
# Availability gate (call time)
# --------------------------------------------------------------------------
def test_missing_tesseract_gate_raises(tmp_path, monkeypatch):
    """With no tesseract binary, a conversion attempt raises TransformError."""
    src = tmp_path / "in.png"
    Image.new("RGB", (400, 200), "white").save(src, "PNG")
    out = tmp_path / "out.txt"
    # is_available() consults shutil.which -> force it to report 'not found'.
    monkeypatch.setattr(shutil, "which", lambda name: None)
    fn = transforms.get_transform(".png", ".txt")
    with pytest.raises(TransformError, match="OCR requires Tesseract"):
        fn(str(src), str(out))


def test_module_imports_and_registers_without_tesseract(monkeypatch):
    """Registration happens at import; gating is deferred to call time."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    # Pairs are present regardless of binary availability.
    assert (".png", ".txt") in ocr_text.TRANSFORMS
    assert transforms.get_transform(".png", ".txt") is not None


# --------------------------------------------------------------------------
# Registry integration + no-shadowing contract
# --------------------------------------------------------------------------
def test_all_image_sources_register_to_txt():
    expected = ['.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp',
                '.gif', '.heic', '.heif']
    for ext in expected:
        assert (ext, ".txt") in ocr_text.TRANSFORMS
        assert transforms.get_transform(ext, ".txt") is not None


def test_ocr_does_not_register_pdf_to_txt():
    """OCR plugin must NOT own any pair the bridge owns -- notably .pdf -> .txt."""
    assert (".pdf", ".txt") not in ocr_text.TRANSFORMS


def test_ocr_and_bridge_pair_sets_are_disjoint():
    overlap = set(ocr_text.TRANSFORMS) & set(pdf_bridge.TRANSFORMS)
    assert overlap == set(), f"OCR plugin shadows bridge pairs: {overlap}"


def test_pdf_to_txt_still_resolves_to_bridge_text_layer(tmp_path):
    """.pdf -> .txt must use the bridge's embedded-text extraction, not OCR.

    We build a real text-layer PDF with reportlab. Its text comes through even
    though no OCR is involved -- proving ocr_text does not shadow the bridge.
    """
    marker = "TextLayerNoOcrMarker"
    src = tmp_path / "doc.pdf"
    c = canvas.Canvas(str(src), pagesize=letter)
    c.setFont("Helvetica", 14)
    c.drawString(72, 720, marker)
    c.showPage()
    c.save()

    out = tmp_path / "doc.txt"
    result = _run(".pdf", ".txt", src, out)
    assert result == str(out)
    body = out.read_text(encoding="utf-8")
    assert marker in body            # embedded text layer extracted
    assert "--- Page 1 ---" in body  # bridge's pdf_to_text page-header style
