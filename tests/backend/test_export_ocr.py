"""Tests for the OCR fallback added to the Export PDF->text feature.

The default (no-OCR) code path is exercised elsewhere in test_export.py; here
we focus on the ``ocr_fallback=True`` behavior of ``pdf_ops.export`` and the
matching route wiring in ``routes/export.py``.

OCR is genuinely slow, so the actual Tesseract run happens exactly once: a
module-scoped fixture builds a mixed PDF (one text page + one image-only page),
runs the OCR extraction a single time, and every OCR assertion reads from that
cached result. Cheap paths (default extraction, availability gating) build
their own tiny inputs per test.
"""
import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from pdf_ops import export as export_mod
from pdf_ops.export import pdf_to_text, pdf_to_text_report
import routes.export as export_routes


# Tokens embedded in the fixture PDF. TEXT_TOKEN lives in a real text layer;
# SCAN_TOKEN is only drawn as pixels (no text layer) and must be recovered by
# OCR. All-caps + spaced letters read most reliably through Tesseract.
TEXT_TOKEN = "TEXTLAYERTOKEN"
SCAN_TOKEN = "SCANNEDTOKEN"


def _big_font(size):
    for path in (
        "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Regular.ttf",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def _make_mixed_pdf(path):
    """Build a 2-page PDF: page 1 has a text layer, page 2 is image-only."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    w, h = letter
    c = canvas.Canvas(str(path), pagesize=letter)

    # Page 1 -- real, extractable text.
    c.setFont("Helvetica", 24)
    c.drawString(72, 700, TEXT_TOKEN)
    c.showPage()

    # Page 2 -- rasterized text drawn onto the page as an image (no text layer).
    img = Image.new("RGB", (1700, 2200), "white")
    draw = ImageDraw.Draw(img)
    draw.text((150, 950), SCAN_TOKEN, fill="black", font=_big_font(160))
    c.drawImage(ImageReader(img), 0, 0, width=w, height=h)
    c.showPage()

    c.save()
    return path


def _sections(text):
    """Split a pdf_to_text body into its per-page section strings."""
    return text.split("\f")


# --------------------------------------------------------------------------
# Default (no-OCR) behavior on the same fixture: page 2 stays empty.
# --------------------------------------------------------------------------
def test_default_leaves_image_page_empty(tmp_path):
    src = _make_mixed_pdf(tmp_path / "mixed.pdf")
    out = tmp_path / "out.txt"
    pdf_to_text(str(src), str(out))
    secs = _sections(out.read_text(encoding="utf-8"))
    assert len(secs) == 2
    assert TEXT_TOKEN in secs[0]
    # Page 2 has no text layer -> its section is just the header, no body text.
    body2 = secs[1].split("---\n", 1)[1]
    assert body2.strip() == ""


# --------------------------------------------------------------------------
# Module-scoped OCR run (the single real Tesseract invocation).
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def ocr_run(tmp_path_factory):
    d = tmp_path_factory.mktemp("ocr")
    src = _make_mixed_pdf(d / "mixed.pdf")

    plain_out = d / "plain.txt"
    pdf_to_text(str(src), str(plain_out))  # no OCR

    ocr_out = d / "ocr.txt"
    report = pdf_to_text_report(str(src), str(ocr_out), ocr_fallback=True, language="eng")

    return {
        "plain_sections": _sections(plain_out.read_text(encoding="utf-8")),
        "ocr_sections": _sections(ocr_out.read_text(encoding="utf-8")),
        "report": report,
    }


def test_ocr_recovers_image_page(ocr_run):
    page2 = ocr_run["ocr_sections"][1]
    # Tesseract can wobble on spacing; check the distinctive token loosely.
    assert "SCANNED" in page2.upper().replace(" ", "")


def test_ocr_does_not_touch_text_page(ocr_run):
    # Page 1 already had a text layer, so the OCR run must leave it byte-identical
    # to the plain run -- proving text pages are never re-OCR'd.
    assert ocr_run["ocr_sections"][0] == ocr_run["plain_sections"][0]
    assert TEXT_TOKEN in ocr_run["ocr_sections"][0]


def test_ocr_report_shape(ocr_run):
    report = ocr_run["report"]
    assert report["pages"] == 2
    assert report["ocr_pages"] == [2]  # only the image-only page needed OCR
    assert report["output_path"].endswith("ocr.txt")


# --------------------------------------------------------------------------
# Availability gating.
# --------------------------------------------------------------------------
def test_gate_raises_when_unavailable(tmp_path, monkeypatch):
    src = _make_mixed_pdf(tmp_path / "mixed.pdf")
    out = tmp_path / "out.txt"
    monkeypatch.setattr(export_mod, "_ocr_is_available", lambda: False)
    with pytest.raises(ValueError, match="OCR requires Tesseract"):
        pdf_to_text_report(str(src), str(out), ocr_fallback=True)


def test_gate_off_ignores_availability(tmp_path, monkeypatch):
    # With ocr_fallback False, unavailability must not matter at all.
    src = _make_mixed_pdf(tmp_path / "mixed.pdf")
    out = tmp_path / "out.txt"
    monkeypatch.setattr(export_mod, "_ocr_is_available", lambda: False)
    assert pdf_to_text(str(src), str(out)) == str(out)


def test_tesseract_error_mapping():
    exc = Exception("Failed loading language 'zzz'")
    msg = export_mod._map_tesseract_error(exc, "zzz")
    assert "zzz" in msg
    assert "not available" in msg


# --------------------------------------------------------------------------
# Route wiring.
# --------------------------------------------------------------------------
def _post_text(client, pdf_bytes, **fields):
    data = {"operation": "text", "file": (io.BytesIO(pdf_bytes), "input.pdf")}
    data.update(fields)
    return client.post("/export/run", data=data, content_type="multipart/form-data")


def test_get_export_exposes_ocr_flag(client):
    resp = client.get("/export")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'id="ocrFallback"' in body
    assert "data-ocr-available" in body


def test_route_ocr_happy_path_reports_count(client, tmp_path):
    pdf_bytes = _make_mixed_pdf(tmp_path / "mixed.pdf").read_bytes()
    resp = _post_text(client, pdf_bytes, ocr_fallback="true", language="eng")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    assert "via OCR" in payload["message"]
    out = client.output_dir / payload["filename"]
    text = out.read_text(encoding="utf-8")
    assert "SCANNED" in text.upper().replace(" ", "")


def test_route_ocr_unavailable_is_400(client, tmp_path, monkeypatch):
    pdf_bytes = _make_mixed_pdf(tmp_path / "mixed.pdf").read_bytes()
    monkeypatch.setattr(export_routes, "ocr_is_available", lambda: False)
    resp = _post_text(client, pdf_bytes, ocr_fallback="1")
    assert resp.status_code == 400
    assert "Tesseract" in resp.get_json()["error"]


def test_route_bad_language_is_400(client, tmp_path):
    pdf_bytes = _make_mixed_pdf(tmp_path / "mixed.pdf").read_bytes()
    resp = _post_text(client, pdf_bytes, ocr_fallback="true", language="zzz")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_route_no_ocr_still_works(client, tmp_path):
    # Plain text export (no ocr_fallback) is unchanged and never mentions OCR.
    pdf_bytes = _make_mixed_pdf(tmp_path / "mixed.pdf").read_bytes()
    resp = _post_text(client, pdf_bytes)
    assert resp.status_code == 200
    assert "via OCR" not in resp.get_json()["message"]
