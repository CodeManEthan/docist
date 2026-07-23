"""Tests for PDF compression: pure ``compress_pdf`` + the /pages/run route.

The bloated fixture is generated programmatically in ``tmp_path`` (reportlab +
a large, hard-to-compress Pillow raster) so no binary samples are committed.
Reuses the shared ``client`` / ``builders`` fixtures from conftest.py.
"""
import io
import os
import random

import pytest
from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from PyPDF2 import PdfReader

from pdf_ops.optimize import compress_pdf


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
def _make_bloated_pdf(path, pages=2, marker="CompressMarker"):
    """A large PDF: a high-res noisy raster (stored FlateDecode) on each page
    plus repeated extractable text. Noisy pixels defeat lossless compression,
    so the image pass has real work to do."""
    rng = random.Random(1234)
    w, h = 1200, 900
    img = Image.new("RGB", (w, h))
    img.putdata([
        (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
        for _ in range(w * h)
    ])
    img_path = str(path) + ".src.png"
    img.save(img_path, "PNG")

    c = canvas.Canvas(str(path), pagesize=letter)
    for i in range(pages):
        c.drawImage(img_path, 50, 300, width=500, height=375)
        c.setFont("Helvetica", 12)
        c.drawString(72, 100, f"{marker} page {i + 1} " + ("lorem ipsum " * 15))
        c.showPage()
    c.save()
    os.remove(img_path)
    return path


def _make_minimal_pdf(path):
    """A hand-written, already-tiny single-page PDF that PdfWriter cannot beat
    (exercises the larger-output fallback)."""
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        (b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
         b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>"),
        b"<</Length 33>>\nstream\nBT /F1 24 Tf 20 100 Td (Hi) Tj ET\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_off = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<</Size %d/Root 1 0 R>>\n" % (len(objs) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref_off
    path.write_bytes(bytes(out))
    return path


# --------------------------------------------------------------------------
# compress_pdf — core behaviour
# --------------------------------------------------------------------------
def test_compress_shrinks_and_stays_valid(tmp_path):
    src = _make_bloated_pdf(tmp_path / "bloat.pdf", pages=2)
    out = tmp_path / "out.pdf"
    original = os.path.getsize(str(src))

    stats = compress_pdf(str(src), str(out))

    # Output is a valid, readable PDF with the same page count.
    reader = PdfReader(str(out))
    assert len(reader.pages) == 2
    # Not larger than the input.
    assert os.path.getsize(str(out)) <= original


def test_compress_preserves_text(tmp_path):
    src = _make_bloated_pdf(tmp_path / "bloat.pdf", pages=2, marker="KeepMe")
    out = tmp_path / "out.pdf"
    compress_pdf(str(src), str(out))

    reader = PdfReader(str(out))
    text = "\n".join(p.extract_text() or "" for p in reader.pages)
    assert "KeepMe" in text


def test_compress_recompresses_images(tmp_path):
    src = _make_bloated_pdf(tmp_path / "bloat.pdf", pages=2)
    out = tmp_path / "out.pdf"
    stats = compress_pdf(str(src), str(out))
    # Both page rasters should be recompressed.
    assert stats["images_recompressed"] >= 1


def test_compress_stats_dict_is_sane(tmp_path):
    src = _make_bloated_pdf(tmp_path / "bloat.pdf", pages=1)
    out = tmp_path / "out.pdf"
    stats = compress_pdf(str(src), str(out))

    assert set(stats) == {
        "original_bytes", "compressed_bytes", "ratio", "images_recompressed",
    }
    assert stats["original_bytes"] == os.path.getsize(str(src))
    assert stats["compressed_bytes"] == os.path.getsize(str(out))
    assert 0.0 < stats["ratio"] <= 1.0
    assert stats["compressed_bytes"] <= stats["original_bytes"]
    assert isinstance(stats["images_recompressed"], int)
    assert stats["images_recompressed"] >= 0


def test_compress_dpi_downsamples_images(tmp_path):
    # A 1200x900 image displayed at 500x375pt is ~173 DPI; capping at 96 must
    # shrink the stored pixel dimensions.
    src = _make_bloated_pdf(tmp_path / "bloat.pdf", pages=1)
    out = tmp_path / "out.pdf"
    compress_pdf(str(src), str(out), image_quality=50, image_max_dpi=96)

    reader = PdfReader(str(out))
    xobjs = reader.pages[0]["/Resources"]["/XObject"]
    widths = [int(xobjs[n].get_object()["/Width"]) for n in xobjs
              if xobjs[n].get_object().get("/Subtype") == "/Image"]
    assert widths and all(w < 1200 for w in widths)


def test_compress_quality_affects_size(tmp_path):
    src = _make_bloated_pdf(tmp_path / "bloat.pdf", pages=2)
    low = tmp_path / "low.pdf"
    high = tmp_path / "high.pdf"
    compress_pdf(str(src), str(low), image_quality=20)
    compress_pdf(str(src), str(high), image_quality=90)
    # Lower JPEG quality yields a smaller file.
    assert os.path.getsize(str(low)) < os.path.getsize(str(high))


# --------------------------------------------------------------------------
# compress_pdf — larger-output fallback
# --------------------------------------------------------------------------
def test_compress_fallback_keeps_original_when_larger(tmp_path):
    src = _make_minimal_pdf(tmp_path / "mini.pdf")
    out = tmp_path / "out.pdf"
    original_bytes = src.read_bytes()

    stats = compress_pdf(str(src), str(out))

    assert stats["ratio"] == 1.0
    assert stats["compressed_bytes"] == stats["original_bytes"]
    # The output is the original bytes, byte-for-byte, and still readable.
    assert out.read_bytes() == original_bytes
    assert len(PdfReader(str(out)).pages) == 1


# --------------------------------------------------------------------------
# Route: POST /pages/run  (operation=compress)
# --------------------------------------------------------------------------
def _bloated_bytes(tmp_path):
    src = _make_bloated_pdf(tmp_path / "input.pdf", pages=2)
    return src.read_bytes()


def test_run_compress_success(client, tmp_path):
    data = _bloated_bytes(tmp_path)
    resp = client.post("/pages/run", data={
        "operation": "compress",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    assert payload["filename"].endswith(".pdf")
    # Message carries human-readable sizes.
    assert "MB" in payload["message"] or "KB" in payload["message"] or "B" in payload["message"]

    out = client.output_dir / payload["filename"]
    assert out.exists()
    assert payload["download_url"] == "/download?filename=" + payload["filename"]
    assert len(PdfReader(str(out)).pages) == 2


def test_run_compress_with_params(client, tmp_path):
    data = _bloated_bytes(tmp_path)
    resp = client.post("/pages/run", data={
        "operation": "compress",
        "image_quality": "40",
        "image_max_dpi": "100",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


@pytest.mark.parametrize("quality", ["5", "99", "abc", "0"])
def test_run_compress_bad_quality_is_400(client, tmp_path, quality):
    data = _bloated_bytes(tmp_path)
    resp = client.post("/pages/run", data={
        "operation": "compress",
        "image_quality": quality,
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


@pytest.mark.parametrize("dpi", ["10", "999", "xyz"])
def test_run_compress_bad_dpi_is_400(client, tmp_path, dpi):
    data = _bloated_bytes(tmp_path)
    resp = client.post("/pages/run", data={
        "operation": "compress",
        "image_max_dpi": dpi,
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()
