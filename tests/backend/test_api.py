"""Integration tests for the Flask endpoints using the test client.

No real server is started. The ``client`` fixture (see conftest.py) points the
app's UPLOAD/OUTPUT folders at tmp_path subdirs, so these tests never touch the
real uploads/ or output/ directories.
"""
import io

import pytest
from PyPDF2 import PdfReader

from converters import get_converter, supported_extensions


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _upload(client, files):
    """POST a list of (filename, bytes) as multipart files[]."""
    data = {
        "files[]": [(io.BytesIO(content), name) for name, content in files],
    }
    return client.post("/upload", data=data, content_type="multipart/form-data")


def _download_reader(client, filename):
    resp = client.get(f"/download?filename={filename}")
    assert resp.status_code == 200, resp.data
    return PdfReader(io.BytesIO(resp.data))


def _merged_pages(client, filename):
    reader = _download_reader(client, filename)
    return len(reader.pages), "\n".join(p.extract_text() or "" for p in reader.pages)


def _expected_pages_for(sources, tmp_path):
    """Replicate the app's blank-page logic to compute the expected page count.

    ``sources`` is a list of (name, bytes). PDFs are counted as-is; convertible
    files are converted the same way the app does. Each doc contributes its own
    page count plus one trailing blank page when that count is odd.
    """
    total = 0
    for i, (name, content) in enumerate(sources):
        ext = "." + name.rsplit(".", 1)[1].lower()
        src = tmp_path / f"exp_{i}_{name}"
        src.write_bytes(content)
        if ext == ".pdf":
            pdf_path = src
        else:
            pdf_path = tmp_path / f"exp_{i}.pdf"
            get_converter(ext)(str(src), str(pdf_path))
        pages = len(PdfReader(str(pdf_path)).pages)
        total += pages + (1 if pages % 2 == 1 else 0)
    return total


# --------------------------------------------------------------------------
# GET /formats
# --------------------------------------------------------------------------
def test_formats_lists_pdf_and_all_converters(client):
    resp = client.get("/formats")
    assert resp.status_code == 200
    exts = resp.get_json()["extensions"]
    assert ".pdf" in exts
    for ext in supported_extensions():
        assert ext in exts


# --------------------------------------------------------------------------
# POST /upload -- two PDFs
# --------------------------------------------------------------------------
def test_upload_two_pdfs_merges_with_blanks_and_numbers(client, tmp_path, builders):
    p1 = builders.pdf(tmp_path / "a.pdf", pages=1, marker=builders.PDF_MARKER_A)
    p2 = builders.pdf(tmp_path / "b.pdf", pages=1, marker=builders.PDF_MARKER_B)
    sources = [("a.pdf", p1.read_bytes()), ("b.pdf", p2.read_bytes())]

    resp = _upload(client, sources)
    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert body["success"] is True
    filename = body["filename"]
    assert filename == "a-merged.pdf"  # named after the first file's basename

    pages, text = _merged_pages(client, filename)
    # Two 1-page (odd) PDFs -> each gets a trailing blank -> 1+1 + 1+1 = 4.
    assert pages == _expected_pages_for(sources, tmp_path) == 4
    assert builders.PDF_MARKER_A in text
    assert builders.PDF_MARKER_B in text
    # Page numbers are stamped bottom-right; page "1" should appear.
    assert "1" in text


# --------------------------------------------------------------------------
# POST /upload -- mixed types
# --------------------------------------------------------------------------
def test_upload_mixed_types_page_count_and_content(client, tmp_path, builders):
    pdf = builders.pdf(tmp_path / "src.pdf", pages=2, marker=builders.PDF_MARKER_A)
    md = builders.markdown(tmp_path / "src.md")
    png = builders.png(tmp_path / "src.png")
    txt = builders.text_rich(tmp_path / "src.txt")
    html = builders.html(tmp_path / "src.html")
    docx = builders.docx(tmp_path / "src.docx")

    sources = [
        ("src.pdf", pdf.read_bytes()),
        ("src.md", md.read_bytes()),
        ("src.png", png.read_bytes()),
        ("src.txt", txt.read_bytes()),
        ("src.html", html.read_bytes()),
        ("src.docx", docx.read_bytes()),
    ]
    resp = _upload(client, sources)
    assert resp.status_code == 200, resp.data
    filename = resp.get_json()["filename"]

    pages, text = _merged_pages(client, filename)
    assert pages == _expected_pages_for(sources, tmp_path)

    # Per-source content spot checks (image content isn't text-extractable).
    assert builders.PDF_MARKER_A in text
    assert builders.MD_HEADING in text
    assert builders.TXT_MARKER in text
    assert builders.HTML_HEADING in text
    assert builders.DOCX_HEADING in text


# --------------------------------------------------------------------------
# REGRESSION: distinct files sharing a basename must all appear
# --------------------------------------------------------------------------
def test_same_basename_different_types_all_present(client, tmp_path, builders):
    md = builders.markdown(tmp_path / "test.md")
    txt = builders.text_rich(tmp_path / "test.txt")
    html = builders.html(tmp_path / "test.html")
    sources = [
        ("test.md", md.read_bytes()),
        ("test.txt", txt.read_bytes()),
        ("test.html", html.read_bytes()),
    ]
    resp = _upload(client, sources)
    assert resp.status_code == 200, resp.data
    filename = resp.get_json()["filename"]

    _, text = _merged_pages(client, filename)
    # Each distinct file's unique marker must appear -- not one file repeated.
    assert builders.MD_HEADING in text
    assert builders.TXT_MARKER in text
    assert builders.HTML_HEADING in text


# --------------------------------------------------------------------------
# POST /upload -- error cases
# --------------------------------------------------------------------------
def test_upload_no_files_field_returns_400(client):
    resp = client.post("/upload", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_upload_empty_filename_returns_400(client):
    resp = _upload(client, [("", b"")])
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_upload_only_unsupported_extension_returns_400(client):
    resp = _upload(client, [("data.xyz", b"nonsense payload")])
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_upload_failing_converter_reports_filename(client, tmp_path):
    # A garbage .docx makes the docx converter raise -> 400 naming the file.
    resp = _upload(client, [("broken.docx", b"\x00\x01 not a real docx " * 30)])
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert "broken.docx" in err


# --------------------------------------------------------------------------
# GET /download -- missing file
# --------------------------------------------------------------------------
def test_download_missing_file_returns_404(client):
    resp = client.get("/download?filename=does-not-exist.pdf")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


# --------------------------------------------------------------------------
# Filename safety
# --------------------------------------------------------------------------
def test_path_traversal_filename_is_sanitized(client, tmp_path, builders):
    pdf = builders.pdf(tmp_path / "clean.pdf", pages=2, marker=builders.PDF_MARKER_A)
    resp = _upload(client, [("../evil.pdf", pdf.read_bytes())])
    assert resp.status_code == 200, resp.data

    # secure_filename strips the traversal -> "evil.pdf" inside the upload dir.
    upload_dir = client.upload_dir
    assert (upload_dir / "evil.pdf").exists()
    # Nothing was written outside the upload dir.
    assert not (upload_dir.parent / "evil.pdf").exists()
