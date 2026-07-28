"""Backend tests for the SVG and HEIC/HEIF converter plugins.

All source documents are built programmatically into ``tmp_path`` -- no binary
sample files are committed. SVGs are written by hand as XML; HEIC files are
generated with Pillow + pillow_heif.
"""
import io

import pytest
from pypdf import PdfReader

import converters
from converters import ConversionError

# US-letter geometry, in PostScript points, matching conftest conventions.
LETTER_W, LETTER_H = 612.0, 792.0
SIZE_TOL = 2.0


# --------------------------------------------------------------------------
# Programmatic builders
# --------------------------------------------------------------------------
def _svg(width, height, body, viewbox=None):
    vb = f' viewBox="{viewbox}"' if viewbox else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}"{vb}>{body}</svg>'
    )


def build_svg_large(path):
    """A big (3000x2000) SVG that must be scaled *down* to fit the page."""
    body = (
        '<rect width="3000" height="2000" fill="#2244aa"/>'
        '<circle cx="1500" cy="1000" r="600" fill="#ffcc00"/>'
    )
    path.write_text(_svg(3000, 2000, body, viewbox="0 0 3000 2000"), encoding="utf-8")
    return path


def build_svg_tiny(path):
    """A tiny (12x12) SVG that exercises the capped upscale policy."""
    body = '<rect width="12" height="12" fill="#cc2222"/>'
    path.write_text(_svg(12, 12, body, viewbox="0 0 12 12"), encoding="utf-8")
    return path


def build_svg_simple(path):
    """A modest SVG with a couple of shapes."""
    body = (
        '<rect x="10" y="10" width="180" height="120" fill="#33aa55"/>'
        '<line x1="0" y1="0" x2="200" y2="150" stroke="#000000" stroke-width="3"/>'
    )
    path.write_text(_svg(200, 150, body, viewbox="0 0 200 150"), encoding="utf-8")
    return path


def build_svg_malformed(path):
    """Truncated / malformed XML that is not a parseable SVG."""
    path.write_text('<svg><rect width="10"', encoding="utf-8")
    return path


def build_svg_not_svg(path):
    """Well-formed XML that is not an SVG document."""
    path.write_text('<root><child>hello</child></root>', encoding="utf-8")
    return path


# HEIC saving is only available in some pillow-heif builds; probe once.
try:
    import pillow_heif

    pillow_heif.register_heif_opener()

    def _probe_heif_save():
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, format="HEIF")
        return True

    HEIF_SAVE_OK = _probe_heif_save()
    HEIF_SKIP_REASON = ""
except Exception as exc:  # pragma: no cover - depends on build
    HEIF_SAVE_OK = False
    HEIF_SKIP_REASON = f"pillow-heif cannot save HEIC in this build: {exc}"

requires_heif_save = pytest.mark.skipif(
    not HEIF_SAVE_OK, reason=HEIF_SKIP_REASON or "HEIC saving unavailable"
)


def build_heic(path, size=(400, 300), color=(120, 20, 200)):
    """Generate a real HEIC image via Pillow + pillow_heif."""
    from PIL import Image

    Image.new("RGB", size, color).save(str(path), format="HEIF")
    return path


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def assert_letter_pdf(pdf_path, min_pages=1):
    reader = PdfReader(str(pdf_path))
    assert len(reader.pages) >= min_pages
    for page in reader.pages:
        box = page.mediabox
        assert abs(float(box.width) - LETTER_W) <= SIZE_TOL
        assert abs(float(box.height) - LETTER_H) <= SIZE_TOL
    return reader


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
def test_registry_includes_new_extensions():
    exts = converters.supported_extensions()
    for ext in (".svg", ".heic", ".heif"):
        assert ext in exts
        assert converters.get_converter(ext) is not None


# --------------------------------------------------------------------------
# SVG converter
# --------------------------------------------------------------------------
def test_svg_simple_produces_letter_pdf(tmp_path):
    src = build_svg_simple(tmp_path / "simple.svg")
    out = tmp_path / "simple.pdf"
    converters.get_converter(".svg")(str(src), str(out))
    assert_letter_pdf(out, min_pages=1)


def test_svg_large_scales_down_to_fit(tmp_path):
    src = build_svg_large(tmp_path / "large.svg")
    out = tmp_path / "large.pdf"
    converters.get_converter(".svg")(str(src), str(out))
    reader = assert_letter_pdf(out, min_pages=1)
    # A single letter page holds the whole (scaled-down) drawing.
    assert len(reader.pages) == 1


def test_svg_tiny_upscales_within_page(tmp_path):
    src = build_svg_tiny(tmp_path / "tiny.svg")
    out = tmp_path / "tiny.pdf"
    converters.get_converter(".svg")(str(src), str(out))
    # Must still be a valid, correctly-sized letter page (no explosion / crash).
    assert_letter_pdf(out, min_pages=1)


def test_svg_malformed_raises(tmp_path):
    src = build_svg_malformed(tmp_path / "bad.svg")
    out = tmp_path / "bad.pdf"
    with pytest.raises(ConversionError):
        converters.get_converter(".svg")(str(src), str(out))


def test_svg_not_svg_raises(tmp_path):
    src = build_svg_not_svg(tmp_path / "notsvg.svg")
    out = tmp_path / "notsvg.pdf"
    with pytest.raises(ConversionError):
        converters.get_converter(".svg")(str(src), str(out))


def test_svg_missing_file_raises(tmp_path):
    out = tmp_path / "missing.pdf"
    with pytest.raises(ConversionError):
        converters.get_converter(".svg")(str(tmp_path / "nope.svg"), str(out))


# --------------------------------------------------------------------------
# HEIC / HEIF converter
# --------------------------------------------------------------------------
@requires_heif_save
def test_heic_produces_letter_pdf(tmp_path):
    src = build_heic(tmp_path / "photo.heic")
    out = tmp_path / "photo.pdf"
    converters.get_converter(".heic")(str(src), str(out))
    assert_letter_pdf(out, min_pages=1)


@requires_heif_save
def test_heif_extension_produces_letter_pdf(tmp_path):
    src = build_heic(tmp_path / "photo.heif", size=(250, 400), color=(20, 180, 90))
    out = tmp_path / "photo_heif.pdf"
    converters.get_converter(".heif")(str(src), str(out))
    assert_letter_pdf(out, min_pages=1)


def test_heic_corrupt_raises(tmp_path):
    src = tmp_path / "corrupt.heic"
    src.write_bytes(b"this is definitely not a HEIC file")
    out = tmp_path / "corrupt.pdf"
    with pytest.raises(ConversionError):
        converters.get_converter(".heic")(str(src), str(out))


# --------------------------------------------------------------------------
# Route-level check: uploading an SVG through the Flask app succeeds.
# --------------------------------------------------------------------------
def test_upload_svg_route(client, tmp_path):
    src = build_svg_simple(tmp_path / "upload.svg")
    data = {
        "files[]": (io.BytesIO(src.read_bytes()), "upload.svg"),
    }
    resp = client.post("/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    payload = resp.get_json()
    assert payload["success"] is True
