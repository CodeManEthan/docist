"""Tests for the Export feature: pure ops + Flask route.

Reuses fixtures from tests/backend/conftest.py (``client``, ``builders``).
"""
import io
import os
import zipfile

import pytest
from PIL import Image
from pypdf import PdfReader

from pdf_ops.export import pdf_to_images, pdf_to_text


# --------------------------------------------------------------------------
# pdf_to_images
# --------------------------------------------------------------------------
def test_images_one_per_page(tmp_path, builders):
    src = builders.pdf(tmp_path / "src.pdf", pages=3)
    outdir = tmp_path / "imgs"
    outdir.mkdir()
    paths = pdf_to_images(str(src), str(outdir), fmt="png", dpi=100)
    assert len(paths) == 3
    for p in paths:
        assert os.path.exists(p)
    assert os.path.basename(paths[0]) == "page_001.png"
    assert os.path.basename(paths[2]) == "page_003.png"


def test_images_png_format(tmp_path, builders):
    src = builders.pdf(tmp_path / "src.pdf", pages=1)
    outdir = tmp_path / "imgs"
    outdir.mkdir()
    paths = pdf_to_images(str(src), str(outdir), fmt="png", dpi=72)
    with Image.open(paths[0]) as im:
        assert im.format == "PNG"


def test_images_jpg_format_is_rgb(tmp_path, builders):
    src = builders.pdf(tmp_path / "src.pdf", pages=1)
    outdir = tmp_path / "imgs"
    outdir.mkdir()
    paths = pdf_to_images(str(src), str(outdir), fmt="jpg", dpi=72)
    assert paths[0].endswith(".jpg")
    with Image.open(paths[0]) as im:
        assert im.format == "JPEG"
        assert im.mode == "RGB"


def test_images_dimensions_scale_with_dpi(tmp_path, builders):
    src = builders.pdf(tmp_path / "src.pdf", pages=1)
    low_dir = tmp_path / "low"
    high_dir = tmp_path / "high"
    low_dir.mkdir()
    high_dir.mkdir()
    low = pdf_to_images(str(src), str(low_dir), fmt="png", dpi=72)
    high = pdf_to_images(str(src), str(high_dir), fmt="png", dpi=144)
    with Image.open(low[0]) as lo, Image.open(high[0]) as hi:
        # Doubling DPI roughly doubles each dimension.
        assert hi.width == pytest.approx(lo.width * 2, rel=0.02)
        assert hi.height == pytest.approx(lo.height * 2, rel=0.02)


def test_images_dpi_72_is_page_point_size(tmp_path, builders):
    # letter is 612x792 pt; at 72 DPI scale is 1.0 -> ~612x792 px.
    src = builders.pdf(tmp_path / "src.pdf", pages=1)
    outdir = tmp_path / "imgs"
    outdir.mkdir()
    paths = pdf_to_images(str(src), str(outdir), fmt="png", dpi=72)
    with Image.open(paths[0]) as im:
        assert im.width == pytest.approx(612, abs=2)
        assert im.height == pytest.approx(792, abs=2)


@pytest.mark.parametrize("fmt", ["gif", "bmp", "tiff", "", "PDF"])
def test_images_invalid_format_raises(tmp_path, builders, fmt):
    src = builders.pdf(tmp_path / "src.pdf", pages=1)
    outdir = tmp_path / "imgs"
    outdir.mkdir()
    with pytest.raises(ValueError):
        pdf_to_images(str(src), str(outdir), fmt=fmt, dpi=100)


@pytest.mark.parametrize("dpi", [0, 10, 29, 601, 5000, -100])
def test_images_invalid_dpi_raises(tmp_path, builders, dpi):
    src = builders.pdf(tmp_path / "src.pdf", pages=1)
    outdir = tmp_path / "imgs"
    outdir.mkdir()
    with pytest.raises(ValueError):
        pdf_to_images(str(src), str(outdir), fmt="png", dpi=dpi)


def test_images_non_numeric_dpi_raises(tmp_path, builders):
    src = builders.pdf(tmp_path / "src.pdf", pages=1)
    outdir = tmp_path / "imgs"
    outdir.mkdir()
    with pytest.raises(ValueError):
        pdf_to_images(str(src), str(outdir), fmt="png", dpi="lots")


def test_images_jpeg_alias_accepted(tmp_path, builders):
    src = builders.pdf(tmp_path / "src.pdf", pages=1)
    outdir = tmp_path / "imgs"
    outdir.mkdir()
    paths = pdf_to_images(str(src), str(outdir), fmt="jpeg", dpi=72)
    assert paths[0].endswith(".jpg")


# --------------------------------------------------------------------------
# pdf_to_text
# --------------------------------------------------------------------------
def test_text_contains_markers(tmp_path, builders):
    src = builders.pdf(tmp_path / "src.pdf", pages=2, marker="ExportMark")
    out = tmp_path / "out.txt"
    pdf_to_text(str(src), str(out))
    text = out.read_text(encoding="utf-8")
    assert "ExportMark page 1" in text
    assert "ExportMark page 2" in text


def test_text_has_page_separators(tmp_path, builders):
    src = builders.pdf(tmp_path / "src.pdf", pages=3)
    out = tmp_path / "out.txt"
    pdf_to_text(str(src), str(out))
    text = out.read_text(encoding="utf-8")
    assert "--- Page 1 ---" in text
    assert "--- Page 2 ---" in text
    assert "--- Page 3 ---" in text
    # A form-feed separates each page section (n pages -> n-1 form-feeds).
    assert text.count("\f") == 2


def test_text_returns_output_path(tmp_path, builders):
    src = builders.pdf(tmp_path / "src.pdf", pages=1)
    out = tmp_path / "out.txt"
    assert pdf_to_text(str(src), str(out)) == str(out)


# --------------------------------------------------------------------------
# Route: GET /export
# --------------------------------------------------------------------------
def test_get_export_renders(client):
    resp = client.get("/export")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "/static/export.js" in body
    assert 'class="active"' in body
    assert "Export" in body


# --------------------------------------------------------------------------
# Route: POST /export/run
# --------------------------------------------------------------------------
def _pdf_bytes(tmp_path, builders, pages=3):
    src = builders.pdf(tmp_path / "input.pdf", pages=pages)
    return src.read_bytes()


def test_run_images_returns_zip(client, tmp_path, builders):
    data = _pdf_bytes(tmp_path, builders, pages=4)
    resp = client.post("/export/run", data={
        "operation": "images",
        "fmt": "png",
        "dpi": "100",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    assert payload["filename"].endswith(".zip")
    assert payload["download_url"] == "/download?filename=" + payload["filename"]
    out = client.output_dir / payload["filename"]
    assert out.exists()
    with zipfile.ZipFile(str(out)) as zf:
        names = sorted(zf.namelist())
    assert len(names) == 4
    assert names[0] == "page_001.png"
    assert names[3] == "page_004.png"


def test_run_images_jpg(client, tmp_path, builders):
    data = _pdf_bytes(tmp_path, builders, pages=2)
    resp = client.post("/export/run", data={
        "operation": "images",
        "fmt": "jpg",
        "dpi": "72",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    out = client.output_dir / resp.get_json()["filename"]
    with zipfile.ZipFile(str(out)) as zf:
        names = sorted(zf.namelist())
    assert names == ["page_001.jpg", "page_002.jpg"]


def test_run_text_returns_txt(client, tmp_path, builders):
    data = _pdf_bytes(tmp_path, builders, pages=2)
    resp = client.post("/export/run", data={
        "operation": "text",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["filename"].endswith(".txt")
    out = client.output_dir / payload["filename"]
    text = out.read_text(encoding="utf-8")
    assert "--- Page 1 ---" in text
    assert "--- Page 2 ---" in text


def test_run_rejects_non_pdf(client):
    resp = client.post("/export/run", data={
        "operation": "text",
        "file": (io.BytesIO(b"not a pdf"), "notes.txt"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_run_bad_dpi_is_400(client, tmp_path, builders):
    data = _pdf_bytes(tmp_path, builders, pages=1)
    resp = client.post("/export/run", data={
        "operation": "images",
        "fmt": "png",
        "dpi": "9000",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_run_bad_format_is_400(client, tmp_path, builders):
    data = _pdf_bytes(tmp_path, builders, pages=1)
    resp = client.post("/export/run", data={
        "operation": "images",
        "fmt": "gif",
        "dpi": "100",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_run_unknown_operation_is_400(client, tmp_path, builders):
    data = _pdf_bytes(tmp_path, builders, pages=1)
    resp = client.post("/export/run", data={
        "operation": "frobnicate",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400


def test_run_missing_file_is_400(client):
    resp = client.post("/export/run", data={"operation": "text"},
                       content_type="multipart/form-data")
    assert resp.status_code == 400
