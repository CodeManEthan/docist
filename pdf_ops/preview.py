"""Page-thumbnail rendering: small PNG previews of a PDF's first pages.

Pure PDF-processing helpers with no Flask dependency (mirrors pdf_ops/export.py,
which shows the same ``pypdfium2`` rendering pattern). Thumbnails are returned
as ``data:image/png;base64,...`` URLs so the frontend can drop them straight
into an ``<img src>`` without a second round-trip or any temporary files.

Only the first :data:`MAX_THUMBS` pages are rendered — a preview strip is a
navigation aid, not an export, and rendering a 500-page document would cost far
more than it is worth. The total page count is always reported so the UI can
say "+N more pages".
"""
import base64
import io

import pypdfium2 as pdfium

from pdf_ops.pdfium_lock import PDFIUM_LOCK

# How many pages we are willing to rasterize for one preview request.
MAX_THUMBS = 24

# Target width, in CSS pixels, of a rendered thumbnail. Pages are rendered at
# ``scale = TARGET_WIDTH / page_width_in_points`` so portrait and landscape
# pages come out the same width (and differing heights), which is what a
# thumbnail strip wants.
TARGET_WIDTH = 120

# Guard rails on the derived scale: a degenerate page box must never make us
# render a 1-pixel or a 20-megapixel bitmap.
_MIN_SCALE = 0.02
_MAX_SCALE = 4.0

# Fallback page width (US Letter, in points) when a page reports a
# non-positive width.
_FALLBACK_WIDTH_PT = 612.0


def _scale_for(width_pt, target_width=TARGET_WIDTH):
    """Render scale that maps ``width_pt`` points onto ``target_width`` pixels."""
    if not width_pt or width_pt <= 0:
        width_pt = _FALLBACK_WIDTH_PT
    scale = float(target_width) / float(width_pt)
    return max(_MIN_SCALE, min(_MAX_SCALE, scale))


def _to_data_url(image):
    """Encode a PIL image as a base64 ``data:image/png`` URL."""
    buf = io.BytesIO()
    image.save(buf, "PNG", optimize=True)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return "data:image/png;base64," + encoded


def render_thumbnails(input_path, max_pages=MAX_THUMBS, width=TARGET_WIDTH):
    """Render the first pages of a PDF as small PNG data URLs.

    Returns ``{'pages': <total pages in the document>,
    'rendered': <how many thumbnails were produced>,
    'thumbs': ['data:image/png;base64,...', ...]}``.

    ``max_pages`` caps the work (clamped to at least 1, defaults to
    :data:`MAX_THUMBS`); ``width`` is the target thumbnail width in pixels.
    A document with no pages yields an empty ``thumbs`` list rather than an
    error — an empty preview is a legitimate answer.

    Raises whatever ``pypdfium2`` raises for an unreadable or password-locked
    document; callers turn that into a user-facing message.
    """
    try:
        max_pages = int(max_pages)
    except (TypeError, ValueError):
        max_pages = MAX_THUMBS
    max_pages = max(1, max_pages)

    try:
        width = int(width)
    except (TypeError, ValueError):
        width = TARGET_WIDTH
    width = max(16, min(600, width))

    thumbs = []
    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(input_path)
        try:
            total = len(doc)
            for i in range(min(total, max_pages)):
                page = doc[i]
                image = page.render(scale=_scale_for(page.get_width(), width)).to_pil()
                thumbs.append(_to_data_url(image.convert("RGB")))
        finally:
            doc.close()

    return {"pages": total, "rendered": len(thumbs), "thumbs": thumbs}
