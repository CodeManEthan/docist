"""Text watermarking: stamp a repeated label onto every page of a PDF.

A reportlab canvas builds a transparent stamp page sized to match each source
page, then pypdf merges the stamp onto the page. The stamp is drawn with a
reduced fill alpha so the underlying content stays legible.
"""
import io
import re

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas

# Positions that draw the text centred horizontally with no rotation.
_FLAT_POSITIONS = ('header', 'footer')
VALID_POSITIONS = ('center',) + _FLAT_POSITIONS

_HEX_RE = re.compile(r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$')


def _validate(text, position, opacity, color):
    """Validate watermark inputs, raising ValueError on anything malformed."""
    if text is None or str(text).strip() == '':
        raise ValueError('Watermark text is required')
    if position not in VALID_POSITIONS:
        raise ValueError(
            f'Invalid position {position!r}; expected one of {VALID_POSITIONS}'
        )
    try:
        opacity = float(opacity)
    except (TypeError, ValueError):
        raise ValueError('Opacity must be a number between 0 and 1')
    if not 0 <= opacity <= 1:
        raise ValueError('Opacity must be between 0 and 1')
    if not isinstance(color, str) or not _HEX_RE.match(color):
        raise ValueError(f'Invalid hex color {color!r}; expected e.g. #888888')
    return opacity


def _make_stamp(width, height, text, position, opacity, font_size, rotation, color):
    """Build a single-page stamp PDF of the given size and return a PdfReader."""
    packet = io.BytesIO()
    can = canvas.Canvas(packet, pagesize=(width, height))
    can.setFont('Helvetica-Bold', font_size)
    can.setFillColor(HexColor(color))
    can.setFillAlpha(opacity)

    if position == 'header':
        can.drawCentredString(width / 2, height - font_size - 20, text)
    elif position == 'footer':
        can.drawCentredString(width / 2, 20 + font_size / 2, text)
    else:  # center — diagonal rotation applies
        can.saveState()
        can.translate(width / 2, height / 2)
        can.rotate(rotation)
        can.drawCentredString(0, 0, text)
        can.restoreState()

    can.save()
    packet.seek(0)
    return PdfReader(packet)


def apply_text_watermark(input_path, output_path, text, position='center',
                         opacity=0.15, font_size=48, rotation=45,
                         color='#888888'):
    """Stamp ``text`` onto every page of the PDF at ``input_path``.

    Positions:
      * ``center`` — centred, rotated by ``rotation`` degrees (diagonal).
      * ``header`` — top-centre, no rotation.
      * ``footer`` — bottom-centre, no rotation.

    ``opacity`` is a 0..1 fill alpha; ``color`` is a ``#rgb``/``#rrggbb`` hex
    string. Invalid inputs raise ``ValueError``.
    """
    opacity = _validate(text, position, opacity, color)
    font_size = float(font_size)
    rotation = float(rotation)

    reader = PdfReader(input_path)
    writer = PdfWriter()

    for page in reader.pages:
        # Attach the page to the writer *first*; pypdf only supports merging
        # onto pages that already belong to a writer.
        new_page = writer.add_page(page)
        width = float(new_page.mediabox.width)
        height = float(new_page.mediabox.height)
        stamp = _make_stamp(width, height, text, position, opacity,
                            font_size, rotation, color)
        new_page.merge_page(stamp.pages[0])

    with open(output_path, 'wb') as fh:
        writer.write(fh)

    return output_path
