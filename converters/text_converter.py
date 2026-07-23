"""Converter plugin: plain text (.txt) to PDF.

Renders text with a monospace font on letter-size pages, preserving line
breaks and wrapping long lines. Multi-page output is handled automatically.
"""
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth

from . import ConversionError

EXTENSIONS = ['.txt']

_FONT_NAME = 'Courier'
_FONT_SIZE = 10
_LEADING = 12          # vertical space per line (points)
_MARGIN = 0.75 * inch


def _read_text(input_path):
    """Read a text file, trying utf-8 then falling back to latin-1."""
    with open(input_path, 'rb') as fh:
        raw = fh.read()
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('latin-1', errors='replace')


def _wrap_line(line, max_width, font_name, font_size):
    """Wrap a single logical line to fit within max_width points.

    Returns a list of physical lines. Empty input yields a single empty
    line so blank lines are preserved.
    """
    if line == '':
        return ['']
    # Expand tabs so widths are predictable with a monospace font.
    line = line.expandtabs(4)

    lines = []
    current = ''
    for ch in line:
        candidate = current + ch
        if stringWidth(candidate, font_name, font_size) <= max_width:
            current = candidate
        else:
            if current == '':
                # Single character wider than the usable width; emit it
                # anyway to guarantee progress.
                lines.append(ch)
                current = ''
            else:
                lines.append(current)
                current = ch
    lines.append(current)
    return lines


def convert(input_path, output_path):
    """Render the text file at input_path to a PDF at output_path."""
    # reportlab's Courier is a Latin-1 (WinAnsi) core font. Characters
    # outside that range are dropped by the PDF renderer, so substitute a
    # replacement for anything it cannot encode to keep output faithful.
    def _safe(text):
        return text.encode('latin-1', errors='replace').decode('latin-1')

    try:
        text = _read_text(input_path)
    except OSError as exc:
        raise ConversionError(f'Could not read text file: {exc}') from exc

    try:
        from reportlab.pdfgen import canvas

        page_width, page_height = letter
        usable_width = page_width - 2 * _MARGIN
        top = page_height - _MARGIN
        bottom = _MARGIN

        pdf = canvas.Canvas(output_path, pagesize=letter)
        pdf.setFont(_FONT_NAME, _FONT_SIZE)

        y = top
        # Normalise newlines, then wrap each logical line.
        for logical_line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
            for physical in _wrap_line(logical_line, usable_width, _FONT_NAME, _FONT_SIZE):
                if y < bottom:
                    pdf.showPage()
                    pdf.setFont(_FONT_NAME, _FONT_SIZE)
                    y = top
                pdf.drawString(_MARGIN, y, _safe(physical))
                y -= _LEADING

        pdf.showPage()
        pdf.save()
    except ConversionError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface any reportlab failure
        raise ConversionError(f'Failed to render text to PDF: {exc}') from exc
