"""Converter plugin: SVG (.svg) -> PDF.

Parses the SVG into a reportlab ``Drawing`` with :func:`svglib.svglib.svg2rlg`
and renders it onto a single page of the chosen paper (Letter, 612 x 792 pt,
by default).

Scaling policy
--------------
The drawing is fit inside the printable area (the page minus a 0.5 in margin on
every side) while preserving its aspect ratio, then centred on the page:

* If the drawing is larger than the printable area it is scaled *down* to fit.
* If it is smaller it may be scaled *up*, but never beyond ``MAX_UPSCALE`` (2x),
  so tiny icons become legible without a small graphic exploding into a blurry,
  wildly-magnified full-page image.

Malformed / non-SVG XML is handled defensively: ``svg2rlg`` is lenient and,
rather than raising, tends to return either ``None`` or an empty zero-sized
drawing for junk input -- both are treated as a :class:`ConversionError`.
"""
import io

from reportlab.graphics import renderPDF
from reportlab.pdfgen import canvas
from svglib.svglib import svg2rlg

from . import ConversionError
from .options import paper_size


EXTENSIONS = ['.svg']

# Page geometry is the chosen paper (Letter by default), in PostScript points.
MARGIN = 0.5 * 72                     # 36 pt margin on every side

# Never magnify a small drawing beyond this factor (see module docstring).
MAX_UPSCALE = 2.0


def convert(input_path, output_path, opts=None):
    """Convert the SVG at ``input_path`` into a PDF written to ``output_path``.

    Raises :class:`ConversionError` with a helpful message on failure.
    """
    try:
        with open(input_path, 'rb') as fh:
            data = fh.read()
    except OSError as exc:
        raise ConversionError(
            f"Could not read SVG file '{input_path}': {exc}"
        ) from exc

    try:
        drawing = svg2rlg(io.BytesIO(data))
    except Exception as exc:
        raise ConversionError(
            f"Could not parse SVG '{input_path}': {exc}"
        ) from exc

    # svg2rlg is lenient: junk / non-SVG XML yields None or an empty, zero-sized
    # drawing rather than raising. Reject anything without positive dimensions.
    if drawing is None or not drawing.width or not drawing.height \
            or drawing.width <= 0 or drawing.height <= 0:
        raise ConversionError(
            f"'{input_path}' is not a valid SVG (no renderable content found)."
        )

    # Fit inside the printable area, preserving aspect ratio; allow limited
    # upscaling of small drawings.
    page_w, page_h = paper_size(opts)
    scale = min((page_w - 2 * MARGIN) / drawing.width,
                (page_h - 2 * MARGIN) / drawing.height)
    scale = min(scale, MAX_UPSCALE)

    scaled_w = drawing.width * scale
    scaled_h = drawing.height * scale

    drawing.scale(scale, scale)
    drawing.width = scaled_w
    drawing.height = scaled_h

    # Centre on the page.
    x = (page_w - scaled_w) / 2.0
    y = (page_h - scaled_h) / 2.0

    try:
        c = canvas.Canvas(output_path, pagesize=(page_w, page_h))
        renderPDF.draw(drawing, c, x, y)
        c.showPage()
        c.save()
    except Exception as exc:
        raise ConversionError(
            f"Could not render SVG '{input_path}' to PDF: {exc}"
        ) from exc
