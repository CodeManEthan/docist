"""Header/footer text and Bates numbering: stamp per-page labels onto a PDF.

Both operations follow watermark.py's overlay pattern: a reportlab canvas
builds a transparent stamp page sized to match each source page, then PyPDF2
merges the stamp onto the page.

Header/footer supports six positional slots (left/center/right on top and
bottom) and the ``{page}``/``{pages}`` placeholders, substituted per page.
Bates numbering stamps an incrementing, zero-padded counter (optionally with a
prefix) into one page corner.
"""
import io
import re

from PyPDF2 import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas

_HEX_RE = re.compile(r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$')

BATES_POSITIONS = ('bottom-right', 'bottom-left', 'top-right', 'top-left')


def _validate_color(color):
    if not isinstance(color, str) or not _HEX_RE.match(color):
        raise ValueError(f'Invalid hex color {color!r}; expected e.g. #444444')


def _substitute(text, page_number, page_count):
    """Replace the ``{page}`` / ``{pages}`` placeholders in a slot's text."""
    return (text
            .replace('{page}', str(page_number))
            .replace('{pages}', str(page_count)))


def _make_headerfooter_stamp(width, height, slots, page_number, page_count,
                             font_size, color, margin):
    """Build a single stamp page laying out the six header/footer slots."""
    packet = io.BytesIO()
    can = canvas.Canvas(packet, pagesize=(width, height))
    can.setFont('Helvetica', font_size)
    can.setFillColor(HexColor(color))

    header_y = height - margin
    footer_y = margin - font_size
    if footer_y < 0:
        footer_y = 0

    def draw(text, x_kind, y):
        if not text:
            return
        rendered = _substitute(text, page_number, page_count)
        if x_kind == 'left':
            can.drawString(margin, y, rendered)
        elif x_kind == 'center':
            can.drawCentredString(width / 2, y, rendered)
        else:  # right
            can.drawRightString(width - margin, y, rendered)

    draw(slots['header_left'], 'left', header_y)
    draw(slots['header_center'], 'center', header_y)
    draw(slots['header_right'], 'right', header_y)
    draw(slots['footer_left'], 'left', footer_y)
    draw(slots['footer_center'], 'center', footer_y)
    draw(slots['footer_right'], 'right', footer_y)

    can.save()
    packet.seek(0)
    return PdfReader(packet)


def apply_header_footer(input_path, output_path, header_left='',
                        header_center='', header_right='', footer_left='',
                        footer_center='', footer_right='', font_size=9,
                        color='#444444', margin=36):
    """Stamp header/footer text into the six page-margin slots.

    Any subset of the six slots may be supplied; empty ones are skipped. At
    least one slot must be non-empty, otherwise ``ValueError`` is raised.

    Each slot's text may contain the placeholders ``{page}`` (the current page
    number, 1-based) and ``{pages}`` (the total page count), which are
    substituted per page -- e.g. ``"Page {page} of {pages}"``.

    ``color`` is a ``#rgb``/``#rrggbb`` hex string; ``margin`` is the inset (in
    points) from the page edges. Overlays are sized per page to each page's
    mediabox.
    """
    slots = {
        'header_left': (header_left or ''),
        'header_center': (header_center or ''),
        'header_right': (header_right or ''),
        'footer_left': (footer_left or ''),
        'footer_center': (footer_center or ''),
        'footer_right': (footer_right or ''),
    }
    if not any(str(v).strip() for v in slots.values()):
        raise ValueError('At least one header/footer slot must be non-empty')

    _validate_color(color)
    font_size = float(font_size)
    margin = float(margin)

    reader = PdfReader(input_path)
    writer = PdfWriter()
    page_count = len(reader.pages)

    for index, page in enumerate(reader.pages):
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        stamp = _make_headerfooter_stamp(
            width, height, slots, index + 1, page_count,
            font_size, color, margin,
        )
        page.merge_page(stamp.pages[0])
        writer.add_page(page)

    with open(output_path, 'wb') as fh:
        writer.write(fh)

    return output_path


def _make_bates_stamp(width, height, text, position, font_size, color, margin):
    """Build a single stamp page with a Bates number in one corner."""
    packet = io.BytesIO()
    can = canvas.Canvas(packet, pagesize=(width, height))
    can.setFont('Helvetica', font_size)
    can.setFillColor(HexColor(color))

    top_y = height - margin
    bottom_y = margin - font_size
    if bottom_y < 0:
        bottom_y = 0

    if position == 'bottom-right':
        can.drawRightString(width - margin, bottom_y, text)
    elif position == 'bottom-left':
        can.drawString(margin, bottom_y, text)
    elif position == 'top-right':
        can.drawRightString(width - margin, top_y, text)
    else:  # top-left
        can.drawString(margin, top_y, text)

    can.save()
    packet.seek(0)
    return PdfReader(packet)


def format_bates(prefix, number, digits):
    """The stamped label for a single page: prefix + zero-padded counter."""
    return f'{prefix}{number:0{digits}d}'


def apply_bates_numbers(input_path, output_path, prefix='', start=1, digits=6,
                        position='bottom-right', font_size=9, color='#000000',
                        margin=36):
    """Stamp an incrementing Bates number onto every page.

    Each page is stamped with ``prefix`` followed by a zero-padded counter that
    increments per page (e.g. ``ACME000001`` -> ``ACME000002`` -> ...). The
    counter starts at ``start`` and is padded to ``digits`` places.

    ``position`` is one of ``bottom-right``/``bottom-left``/``top-right``/
    ``top-left``. Returns the last (highest) Bates label used, so the caller can
    report the stamped range.

    ``digits`` must be between 3 and 10; ``start`` must be >= 0. Invalid inputs
    raise ``ValueError``.
    """
    try:
        start = int(start)
    except (TypeError, ValueError):
        raise ValueError('Bates start must be a whole number >= 0')
    try:
        digits = int(digits)
    except (TypeError, ValueError):
        raise ValueError('Bates digits must be a whole number between 3 and 10')

    if start < 0:
        raise ValueError('Bates start must be >= 0')
    if not 3 <= digits <= 10:
        raise ValueError('Bates digits must be between 3 and 10')
    if position not in BATES_POSITIONS:
        raise ValueError(
            f'Invalid position {position!r}; expected one of {BATES_POSITIONS}'
        )
    _validate_color(color)

    prefix = '' if prefix is None else str(prefix)
    font_size = float(font_size)
    margin = float(margin)

    reader = PdfReader(input_path)
    writer = PdfWriter()

    last_label = format_bates(prefix, start, digits)
    number = start
    for page in reader.pages:
        label = format_bates(prefix, number, digits)
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        stamp = _make_bates_stamp(
            width, height, label, position, font_size, color, margin,
        )
        page.merge_page(stamp.pages[0])
        writer.add_page(page)
        last_label = label
        number += 1

    with open(output_path, 'wb') as fh:
        writer.write(fh)

    return last_label
