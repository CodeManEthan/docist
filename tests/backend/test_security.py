"""Tests for watermark & security: pdf_ops units plus the /security route.

Reuses conftest.py's ``client`` fixture (folders redirected into tmp_path) and
its ``build_pdf`` helper via the ``builders`` fixture.
"""
import io

import pytest
from PyPDF2 import PdfReader

from pdf_ops.security import protect_pdf, unlock_pdf
from pdf_ops.watermark import apply_text_watermark

from conftest import build_pdf

WATERMARK_TEXT = "ConfidentialMarkerXi"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _page_content_len(pdf_path):
    """Total byte length of every page's decoded content stream."""
    reader = PdfReader(str(pdf_path))
    total = 0
    for page in reader.pages:
        total += len(page.get_contents().get_data())
    return total


def _run(client, filename, content, form):
    data = {"file": (io.BytesIO(content), filename)}
    data.update(form)
    return client.post("/security/run", data=data,
                       content_type="multipart/form-data")


def _download_bytes(client, filename):
    resp = client.get(f"/download?filename={filename}")
    assert resp.status_code == 200, resp.data
    return resp.data


# ==========================================================================
# Watermark — pdf_ops unit
# ==========================================================================
def test_watermark_adds_content_to_every_page(tmp_path, builders):
    src = build_pdf(tmp_path / "src.pdf", pages=3, marker=builders.PDF_MARKER_A)
    before = _page_content_len(src)

    out = tmp_path / "out.pdf"
    apply_text_watermark(str(src), str(out), WATERMARK_TEXT)

    reader = PdfReader(str(out))
    assert len(reader.pages) == 3
    # Every page's content stream grew after the merge.
    assert _page_content_len(out) > before


def test_watermark_text_is_extractable(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=2)
    out = tmp_path / "out.pdf"
    apply_text_watermark(str(src), str(out), WATERMARK_TEXT, position="header")

    reader = PdfReader(str(out))
    all_text = "\n".join(p.extract_text() or "" for p in reader.pages)
    # The watermark text appears on every page.
    assert all_text.count(WATERMARK_TEXT) == 2


@pytest.mark.parametrize("position", ["center", "header", "footer"])
def test_watermark_positions_all_valid(tmp_path, position):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    out = tmp_path / f"out_{position}.pdf"
    apply_text_watermark(str(src), str(out), WATERMARK_TEXT, position=position)
    assert PdfReader(str(out)).pages  # produced a readable PDF


def test_watermark_rejects_empty_text(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    with pytest.raises(ValueError):
        apply_text_watermark(str(src), str(tmp_path / "o.pdf"), "   ")


def test_watermark_rejects_bad_position(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    with pytest.raises(ValueError):
        apply_text_watermark(str(src), str(tmp_path / "o.pdf"), "x",
                             position="sideways")


@pytest.mark.parametrize("opacity", [-0.1, 1.5, 2, "abc"])
def test_watermark_rejects_bad_opacity(tmp_path, opacity):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    with pytest.raises(ValueError):
        apply_text_watermark(str(src), str(tmp_path / "o.pdf"), "x",
                             opacity=opacity)


@pytest.mark.parametrize("color", ["888888", "#gggggg", "#12", "blue", ""])
def test_watermark_rejects_bad_color(tmp_path, color):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    with pytest.raises(ValueError):
        apply_text_watermark(str(src), str(tmp_path / "o.pdf"), "x", color=color)


# ==========================================================================
# Protect / Unlock — pdf_ops unit
# ==========================================================================
def test_protect_produces_encrypted_pdf(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=2)
    out = tmp_path / "protected.pdf"
    protect_pdf(str(src), str(out), "s3cret")

    reader = PdfReader(str(out))
    assert reader.is_encrypted is True
    # Wrong password fails, correct password succeeds.
    assert not PdfReader(str(out)).decrypt("wrong")
    ok = PdfReader(str(out))
    assert ok.decrypt("s3cret")


def test_protect_rejects_empty_password(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    with pytest.raises(ValueError):
        protect_pdf(str(src), str(tmp_path / "o.pdf"), "")


def test_unlock_roundtrip(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=2)
    protected = tmp_path / "protected.pdf"
    protect_pdf(str(src), str(protected), "pw123")

    unlocked = tmp_path / "unlocked.pdf"
    unlock_pdf(str(protected), str(unlocked), "pw123")

    reader = PdfReader(str(unlocked))
    assert reader.is_encrypted is False
    assert len(reader.pages) == 2
    # Content is readable without a password.
    assert reader.pages[0].extract_text() is not None


def test_unlock_wrong_password_raises(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    protected = tmp_path / "protected.pdf"
    protect_pdf(str(src), str(protected), "right")
    with pytest.raises(ValueError, match="Incorrect password"):
        unlock_pdf(str(protected), str(tmp_path / "o.pdf"), "wrong")


def test_unlock_unencrypted_raises(tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    with pytest.raises(ValueError, match="not password-protected"):
        unlock_pdf(str(src), str(tmp_path / "o.pdf"), "any")


# ==========================================================================
# Route — GET /security
# ==========================================================================
def test_security_page_serves(client):
    resp = client.get("/security")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "/static/security.js" in body
    assert 'class="active"' in body


# ==========================================================================
# Route — POST /security/run
# ==========================================================================
def test_route_watermark(client, tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=2)
    resp = _run(client, "src.pdf", src.read_bytes(), {
        "operation": "watermark",
        "text": WATERMARK_TEXT,
        "position": "footer",
    })
    assert resp.status_code == 200, resp.data
    data = resp.get_json()
    assert data["success"] is True
    assert data["download_url"].startswith("/download?filename=")

    pdf_bytes = _download_bytes(client, data["filename"])
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 2


def test_route_rejects_non_pdf(client):
    resp = _run(client, "notes.txt", b"hello", {
        "operation": "watermark", "text": "x",
    })
    assert resp.status_code == 400
    assert "pdf" in resp.get_json()["error"].lower()


def test_route_watermark_requires_text(client, tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    resp = _run(client, "src.pdf", src.read_bytes(), {
        "operation": "watermark", "text": "  ",
    })
    assert resp.status_code == 400


def test_route_unknown_operation(client, tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    resp = _run(client, "src.pdf", src.read_bytes(), {"operation": "bogus"})
    assert resp.status_code == 400


def test_route_protect_then_download_encrypted(client, tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    resp = _run(client, "src.pdf", src.read_bytes(), {
        "operation": "protect", "password": "letmein",
    })
    assert resp.status_code == 200, resp.data
    pdf_bytes = _download_bytes(client, resp.get_json()["filename"])
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert reader.is_encrypted is True
    assert reader.decrypt("letmein")


def test_route_unlock_wrong_password_400(client, tmp_path):
    # First protect via unit helper, then feed to the unlock route.
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    protected = tmp_path / "protected.pdf"
    protect_pdf(str(src), str(protected), "correct")

    resp = _run(client, "protected.pdf", protected.read_bytes(), {
        "operation": "unlock", "password": "incorrect",
    })
    assert resp.status_code == 400
    assert "password" in resp.get_json()["error"].lower()


def test_route_unlock_unencrypted_400(client, tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=1)
    resp = _run(client, "src.pdf", src.read_bytes(), {
        "operation": "unlock", "password": "whatever",
    })
    assert resp.status_code == 400
    assert "not password-protected" in resp.get_json()["error"].lower()


def test_route_unlock_success(client, tmp_path):
    src = build_pdf(tmp_path / "src.pdf", pages=2)
    protected = tmp_path / "protected.pdf"
    protect_pdf(str(src), str(protected), "openme")

    resp = _run(client, "protected.pdf", protected.read_bytes(), {
        "operation": "unlock", "password": "openme",
    })
    assert resp.status_code == 200, resp.data
    pdf_bytes = _download_bytes(client, resp.get_json()["filename"])
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert reader.is_encrypted is False
    assert len(reader.pages) == 2
