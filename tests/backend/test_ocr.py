"""Tests for the OCR "Make Searchable" feature: pure ``make_searchable`` +
the /pages/run route (operation=ocr).

Fixtures are generated programmatically in ``tmp_path`` -- an image-only
(scanned-looking) PDF is drawn with Pillow at ~300 DPI and wrapped with img2pdf
(an OCRmyPDF dependency), so it carries no text layer until OCR adds one. Real
OCR runs take a couple of seconds each, so the expensive image-only OCR result
is produced once per module and shared across the assertions that need it.
"""
import glob
import io
import os

import pytest
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

from pdf_ops import ocr as ocr_ops


OCR_WORDS = ["TESSERACT", "SEARCHABLE"]


# --------------------------------------------------------------------------
# Fixture builders
# --------------------------------------------------------------------------
def _big_font(size=120):
    for pat in (
        "/usr/share/fonts/**/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/**/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/**/DejaVuSans.ttf",
    ):
        hits = glob.glob(pat, recursive=True)
        if hits:
            return ImageFont.truetype(hits[0], size)
    return ImageFont.load_default()


def _image_only_pdf(path, words=OCR_WORDS):
    """A single-page image-only PDF: crisp black words on white at ~300 DPI,
    wrapped with img2pdf so it has NO text layer until OCR adds one."""
    img = Image.new("RGB", (2550, 3300), "white")
    draw = ImageDraw.Draw(img)
    font = _big_font()
    y = 400
    for word in words:
        draw.text((300, y), word, fill="black", font=font)
        y += 300
    png_path = str(path) + ".png"
    img.save(png_path, dpi=(300, 300))
    try:
        import img2pdf
        with open(str(path), "wb") as fh:
            fh.write(img2pdf.convert(png_path))
    finally:
        os.remove(png_path)
    return path


def _all_text(pdf_path):
    reader = PdfReader(str(pdf_path))
    return "\n".join(p.extract_text() or "" for p in reader.pages)


# --------------------------------------------------------------------------
# Shared (expensive) OCR result — run once per module.
# --------------------------------------------------------------------------
pytestmark = pytest.mark.skipif(
    not ocr_ops.is_available(),
    reason="OCR requires Tesseract and Ghostscript",
)


@pytest.fixture(scope="module")
def ocr_result(tmp_path_factory):
    """Build an image-only PDF and OCR it once; reuse across asserts."""
    d = tmp_path_factory.mktemp("ocr")
    src = _image_only_pdf(d / "scan.pdf")
    out = d / "scan_searchable.pdf"
    stats = ocr_ops.make_searchable(str(src), str(out))
    return {"src": src, "out": out, "stats": stats}


# --------------------------------------------------------------------------
# make_searchable — core behaviour
# --------------------------------------------------------------------------
def test_source_has_no_text_layer(ocr_result):
    # Sanity: the fixture really is image-only before OCR.
    assert _all_text(ocr_result["src"]).strip() == ""


def test_make_searchable_adds_text(ocr_result):
    text = _all_text(ocr_result["out"])
    for word in OCR_WORDS:
        assert word in text


def test_make_searchable_stats_dict(ocr_result):
    stats = ocr_result["stats"]
    assert stats == {"pages": 1, "language": "eng"}


def test_text_pdf_passes_through_with_skip_text(tmp_path, builders):
    # A fully-text PDF must survive OCR (skip_text) without error and keep text.
    src = builders.pdf(tmp_path / "text.pdf", pages=2, marker="KeepThisText")
    out = tmp_path / "text_searchable.pdf"
    stats = ocr_ops.make_searchable(str(src), str(out))
    assert stats["pages"] == 2
    assert "KeepThisText" in _all_text(out)


def test_unknown_language_raises(tmp_path, builders):
    src = builders.pdf(tmp_path / "text.pdf", pages=1)
    out = tmp_path / "out.pdf"
    with pytest.raises(ValueError) as exc:
        ocr_ops.make_searchable(str(src), str(out), language="zzz")
    # Error message lists what IS installed.
    assert "eng" in str(exc.value)


def test_empty_language_raises(tmp_path, builders):
    src = builders.pdf(tmp_path / "text.pdf", pages=1)
    out = tmp_path / "out.pdf"
    with pytest.raises(ValueError):
        ocr_ops.make_searchable(str(src), str(out), language="   ")


# --------------------------------------------------------------------------
# Availability gate (works regardless of whether OCR is actually installed).
# --------------------------------------------------------------------------
def test_make_searchable_gate_raises_when_unavailable(tmp_path, monkeypatch):
    # Simulate missing binaries: shutil.which returns None for everything.
    monkeypatch.setattr(ocr_ops.shutil, "which", lambda name: None)
    assert ocr_ops.is_available() is False
    with pytest.raises(RuntimeError):
        ocr_ops.make_searchable(str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf"))


def test_gate_raises_when_only_tesseract_present(tmp_path, monkeypatch):
    # Ghostscript missing -> still unavailable.
    monkeypatch.setattr(
        ocr_ops.shutil, "which",
        lambda name: "/usr/bin/tesseract" if name == "tesseract" else None,
    )
    assert ocr_ops.is_available() is False
    with pytest.raises(RuntimeError):
        ocr_ops.make_searchable(str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf"))


# --------------------------------------------------------------------------
# Route: POST /pages/run  (operation=ocr)
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def image_only_bytes(tmp_path_factory):
    d = tmp_path_factory.mktemp("ocr_route")
    src = _image_only_pdf(d / "scan.pdf")
    return src.read_bytes()


def test_run_ocr_success(client, image_only_bytes):
    resp = client.post("/pages/run", data={
        "operation": "ocr",
        "file": (io.BytesIO(image_only_bytes), "scan.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    assert payload["filename"].endswith(".pdf")
    assert "searchable" in payload["message"]
    assert "(eng)" in payload["message"]

    out = client.output_dir / payload["filename"]
    assert out.exists()
    text = _all_text(out)
    for word in OCR_WORDS:
        assert word in text


def test_run_ocr_unavailable_is_400(client, tmp_path, builders, monkeypatch):
    monkeypatch.setattr(ocr_ops, "is_available", lambda: False)
    src = builders.pdf(tmp_path / "input.pdf", pages=1)
    resp = client.post("/pages/run", data={
        "operation": "ocr",
        "file": (io.BytesIO(src.read_bytes()), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()
    assert "Tesseract" in resp.get_json()["error"]


def test_run_ocr_bad_language_is_400(client, tmp_path, builders):
    src = builders.pdf(tmp_path / "input.pdf", pages=1)
    resp = client.post("/pages/run", data={
        "operation": "ocr",
        "language": "zzz",
        "file": (io.BytesIO(src.read_bytes()), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()
