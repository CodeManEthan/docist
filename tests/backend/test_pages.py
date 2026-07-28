"""Tests for the Page Tools feature: pure ops + Flask route.

Reuses fixtures from tests/backend/conftest.py (``client``, ``builders``).
"""
import io
import os
import zipfile

import pytest
from pypdf import PdfReader

from pdf_ops.pages import (
    parse_page_ranges,
    extract_pages,
    remove_pages,
    rotate_pages,
    split_pdf,
)


# --------------------------------------------------------------------------
# parse_page_ranges
# --------------------------------------------------------------------------
def test_parse_single_pages():
    assert parse_page_ranges("1", 10) == [0]
    assert parse_page_ranges("5", 10) == [4]


def test_parse_simple_range():
    assert parse_page_ranges("1-3", 10) == [0, 1, 2]


def test_parse_mixed_spec():
    assert parse_page_ranges("1-3,5,8-10", 10) == [0, 1, 2, 4, 7, 8, 9]


def test_parse_whitespace_tolerant():
    assert parse_page_ranges("  1 - 3 , 5 ", 10) == [0, 1, 2, 4]


def test_parse_overlapping_dedupes_and_sorts():
    assert parse_page_ranges("3,1-3,2", 10) == [0, 1, 2]


def test_parse_out_of_order_sorts():
    assert parse_page_ranges("8,2,5", 10) == [1, 4, 7]


def test_parse_full_range_at_boundary():
    assert parse_page_ranges("1-10", 10) == list(range(10))


@pytest.mark.parametrize("spec", ["", "   ", None])
def test_parse_empty_raises(spec):
    with pytest.raises(ValueError):
        parse_page_ranges(spec, 10)


@pytest.mark.parametrize("spec", ["0", "0-3", "1-11", "11", "12-14"])
def test_parse_out_of_range_raises(spec):
    with pytest.raises(ValueError):
        parse_page_ranges(spec, 10)


@pytest.mark.parametrize("spec", ["abc", "1-", "-3", "1-2-3", "1,,3", "1.5", "1-a"])
def test_parse_garbage_raises(spec):
    with pytest.raises(ValueError):
        parse_page_ranges(spec, 10)


def test_parse_backwards_range_raises():
    with pytest.raises(ValueError):
        parse_page_ranges("5-2", 10)


def test_parse_zero_page_count_raises():
    with pytest.raises(ValueError):
        parse_page_ranges("1", 0)


# --------------------------------------------------------------------------
# extract_pages / remove_pages / rotate_pages
# --------------------------------------------------------------------------
def test_extract_pages(tmp_path, builders):
    src = builders.pdf(tmp_path / "src.pdf", pages=5)
    out = tmp_path / "out.pdf"
    extract_pages(str(src), str(out), [0, 2, 4])
    reader = PdfReader(str(out))
    assert len(reader.pages) == 3


def test_extract_preserves_order(tmp_path, builders):
    src = builders.pdf(tmp_path / "src.pdf", pages=4, marker="Mark")
    out = tmp_path / "out.pdf"
    extract_pages(str(src), str(out), [2, 0])
    reader = PdfReader(str(out))
    assert len(reader.pages) == 2
    assert "page 3" in (reader.pages[0].extract_text() or "")
    assert "page 1" in (reader.pages[1].extract_text() or "")


def test_remove_pages(tmp_path, builders):
    src = builders.pdf(tmp_path / "src.pdf", pages=5)
    out = tmp_path / "out.pdf"
    remove_pages(str(src), str(out), [0, 1])
    reader = PdfReader(str(out))
    assert len(reader.pages) == 3


def test_remove_all_pages_raises(tmp_path, builders):
    src = builders.pdf(tmp_path / "src.pdf", pages=3)
    out = tmp_path / "out.pdf"
    with pytest.raises(ValueError):
        remove_pages(str(src), str(out), [0, 1, 2])


def test_rotate_all_pages(tmp_path, builders):
    src = builders.pdf(tmp_path / "src.pdf", pages=3)
    out = tmp_path / "out.pdf"
    rotate_pages(str(src), str(out), 90, None)
    reader = PdfReader(str(out))
    for page in reader.pages:
        assert page.get("/Rotate", 0) == 90


def test_rotate_selected_pages(tmp_path, builders):
    src = builders.pdf(tmp_path / "src.pdf", pages=3)
    out = tmp_path / "out.pdf"
    rotate_pages(str(src), str(out), 180, [1])
    reader = PdfReader(str(out))
    assert reader.pages[0].get("/Rotate", 0) == 0
    assert reader.pages[1].get("/Rotate", 0) == 180
    assert reader.pages[2].get("/Rotate", 0) == 0


def test_rotate_invalid_angle_raises(tmp_path, builders):
    src = builders.pdf(tmp_path / "src.pdf", pages=2)
    out = tmp_path / "out.pdf"
    with pytest.raises(ValueError):
        rotate_pages(str(src), str(out), 45, None)


# --------------------------------------------------------------------------
# split_pdf
# --------------------------------------------------------------------------
def test_split_every_n(tmp_path, builders):
    src = builders.pdf(tmp_path / "doc.pdf", pages=7)
    outdir = tmp_path / "parts"
    outdir.mkdir()
    parts = split_pdf(str(src), str(outdir), "every_n", 3)
    assert len(parts) == 3  # 3 + 3 + 1
    counts = [len(PdfReader(p).pages) for p in parts]
    assert counts == [3, 3, 1]
    assert os.path.basename(parts[0]) == "doc_part1.pdf"
    assert os.path.basename(parts[2]) == "doc_part3.pdf"


def test_split_every_n_exact(tmp_path, builders):
    src = builders.pdf(tmp_path / "doc.pdf", pages=6)
    outdir = tmp_path / "parts"
    outdir.mkdir()
    parts = split_pdf(str(src), str(outdir), "every_n", 2)
    assert [len(PdfReader(p).pages) for p in parts] == [2, 2, 2]


def test_split_ranges(tmp_path, builders):
    src = builders.pdf(tmp_path / "doc.pdf", pages=10)
    outdir = tmp_path / "parts"
    outdir.mkdir()
    parts = split_pdf(str(src), str(outdir), "ranges", ["1-3", "4-6", "7-10"])
    assert len(parts) == 3
    assert [len(PdfReader(p).pages) for p in parts] == [3, 3, 4]


def test_split_bad_mode_raises(tmp_path, builders):
    src = builders.pdf(tmp_path / "doc.pdf", pages=4)
    outdir = tmp_path / "parts"
    outdir.mkdir()
    with pytest.raises(ValueError):
        split_pdf(str(src), str(outdir), "bogus", 2)


def test_split_every_n_zero_raises(tmp_path, builders):
    src = builders.pdf(tmp_path / "doc.pdf", pages=4)
    outdir = tmp_path / "parts"
    outdir.mkdir()
    with pytest.raises(ValueError):
        split_pdf(str(src), str(outdir), "every_n", 0)


# --------------------------------------------------------------------------
# Route: GET /pages
# --------------------------------------------------------------------------
def test_get_pages_renders(client):
    resp = client.get("/pages")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "/static/pages.js" in body
    assert 'class="active"' in body
    assert "Page Tools" in body


# --------------------------------------------------------------------------
# Route: POST /pages/run
# --------------------------------------------------------------------------
def _upload(tmp_path, builders, pages=5):
    src = builders.pdf(tmp_path / "input.pdf", pages=pages)
    return src.read_bytes()


def test_run_extract(client, tmp_path, builders):
    data = _upload(tmp_path, builders, pages=5)
    resp = client.post("/pages/run", data={
        "operation": "extract",
        "ranges": "1-2,4",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    out = client.output_dir / payload["filename"]
    assert out.exists()
    assert len(PdfReader(str(out)).pages) == 3
    assert payload["download_url"] == "/download?filename=" + payload["filename"]


def test_run_remove(client, tmp_path, builders):
    data = _upload(tmp_path, builders, pages=5)
    resp = client.post("/pages/run", data={
        "operation": "remove",
        "ranges": "1",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    payload = resp.get_json()
    out = client.output_dir / payload["filename"]
    assert len(PdfReader(str(out)).pages) == 4


def test_run_rotate(client, tmp_path, builders):
    data = _upload(tmp_path, builders, pages=3)
    resp = client.post("/pages/run", data={
        "operation": "rotate",
        "angle": "90",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    payload = resp.get_json()
    out = client.output_dir / payload["filename"]
    reader = PdfReader(str(out))
    assert all(p.get("/Rotate", 0) == 90 for p in reader.pages)


def test_run_split_every_n_returns_zip(client, tmp_path, builders):
    data = _upload(tmp_path, builders, pages=7)
    resp = client.post("/pages/run", data={
        "operation": "split",
        "split_mode": "every_n",
        "split_value": "3",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["filename"].endswith(".zip")
    out = client.output_dir / payload["filename"]
    with zipfile.ZipFile(str(out)) as zf:
        names = zf.namelist()
    assert len(names) == 3
    assert "input_part1.pdf" in names
    assert "input_part3.pdf" in names


def test_run_split_ranges_returns_zip(client, tmp_path, builders):
    data = _upload(tmp_path, builders, pages=10)
    resp = client.post("/pages/run", data={
        "operation": "split",
        "split_mode": "ranges",
        "split_value": "1-3;4-6;7-10",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    payload = resp.get_json()
    out = client.output_dir / payload["filename"]
    with zipfile.ZipFile(str(out)) as zf:
        names = sorted(zf.namelist())
        # verify page counts inside the zip
        counts = []
        for n in sorted(names):
            with zf.open(n) as fh:
                counts.append(len(PdfReader(io.BytesIO(fh.read())).pages))
    assert len(names) == 3
    assert counts == [3, 3, 4]


def test_run_rejects_non_pdf(client):
    resp = client.post("/pages/run", data={
        "operation": "extract",
        "ranges": "1",
        "file": (io.BytesIO(b"not a pdf"), "notes.txt"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_run_bad_range_is_400(client, tmp_path, builders):
    data = _upload(tmp_path, builders, pages=3)
    resp = client.post("/pages/run", data={
        "operation": "extract",
        "ranges": "9-12",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_run_unknown_operation_is_400(client, tmp_path, builders):
    data = _upload(tmp_path, builders, pages=3)
    resp = client.post("/pages/run", data={
        "operation": "frobnicate",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400


def test_run_missing_file_is_400(client):
    resp = client.post("/pages/run", data={"operation": "extract", "ranges": "1"},
                       content_type="multipart/form-data")
    assert resp.status_code == 400
