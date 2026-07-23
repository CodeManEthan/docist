"""Converter plugin: spreadsheets (.csv, .xlsx) -> PDF.

Renders tabular data as PDF tables using ``reportlab.platypus``. Each table
has a shaded, repeated header row (``repeatRows=1``), grid lines and light
alternating row shading. Multi-page tables flow naturally across pages.

Wide-table strategy
    When a table has many columns the page is switched to *landscape* letter
    to win back horizontal room. Column widths are then capped to a maximum
    and individual cells whose text is far too long are truncated with an
    ellipsis so nothing overflows the page. Very wide tables therefore stay
    readable (if abbreviated) rather than spilling off the edge.

Empty-file policy
    An empty CSV, or an XLSX whose sheets are all empty, produces a valid
    one-page PDF stating that the file is empty (rather than raising). A
    *corrupt*/unreadable XLSX raises ``ConversionError``.

CSV parsing
    The delimiter is sniffed with :class:`csv.Sniffer`, falling back to a
    comma. Bytes are decoded as UTF-8 with a latin-1 fallback.

XLSX parsing
    Read values-only with ``openpyxl``. Every non-empty worksheet is rendered
    as its own table preceded by a heading with the sheet name.
"""
import csv
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.pdfbase.pdfmetrics import stringWidth
from xml.sax.saxutils import escape

from . import ConversionError

EXTENSIONS = ['.csv', '.xlsx']

# --- Layout tuning ---------------------------------------------------------
_MARGIN = 0.6 * inch
_CELL_FONT = 'Helvetica'
_CELL_FONT_SIZE = 8
_HEADER_FONT = 'Helvetica-Bold'
# Switch to landscape once a table is at least this many columns wide.
_LANDSCAPE_COL_THRESHOLD = 6
# Hard cap on characters kept in a single cell before truncation.
_MAX_CELL_CHARS = 200
# Column-width caps (points).
_MAX_COL_WIDTH = 2.2 * inch
_MIN_COL_WIDTH = 0.4 * inch


def _read_csv_rows(input_path):
    """Read a CSV into a list of string rows, sniffing the delimiter."""
    with open(input_path, 'rb') as fh:
        raw = fh.read()
    if not raw.strip():
        return []
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        text = raw.decode('latin-1', errors='replace')

    # Sniff the delimiter from a sample; fall back to comma.
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
    except csv.Error:
        dialect = csv.excel  # comma-delimited default

    reader = csv.reader(io.StringIO(text), dialect)
    rows = [list(row) for row in reader]
    return rows


def _read_xlsx_sheets(input_path):
    """Read an XLSX into ``[(sheet_name, rows), ...]`` skipping empty sheets.

    ``rows`` is a list of lists of stringified cell values. Raises
    ``ConversionError`` if the workbook cannot be opened/parsed.
    """
    import openpyxl

    try:
        wb = openpyxl.load_workbook(input_path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - any openpyxl/zip failure -> corrupt
        raise ConversionError(
            f"Could not open spreadsheet '{input_path}': {exc}"
        ) from exc

    sheets = []
    try:
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = ['' if v is None else _stringify(v) for v in row]
                # Drop trailing all-empty cells for a tidier table.
                while cells and cells[-1] == '':
                    cells.pop()
                if any(c != '' for c in cells):
                    rows.append(cells)
            if rows:
                # Normalise ragged rows to a common width.
                width = max(len(r) for r in rows)
                for r in rows:
                    r.extend([''] * (width - len(r)))
                sheets.append((ws.title, rows))
    finally:
        wb.close()
    return sheets


def _stringify(value):
    """Turn a cell value into a display string."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _truncate(text):
    """Cap extreme cell text with an ellipsis to avoid overflow."""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if len(text) > _MAX_CELL_CHARS:
        return text[: _MAX_CELL_CHARS - 1] + '…'
    return text


def _wrap_cell(text, style):
    """Wrap cell text into a Paragraph so long values flow onto new lines."""
    safe = escape(_truncate(text)).replace('\n', '<br/>')
    return Paragraph(safe or '&nbsp;', style)


def _column_widths(rows, usable_width):
    """Compute capped, proportional column widths that fit ``usable_width``."""
    ncols = max((len(r) for r in rows), default=0)
    if ncols == 0:
        return []

    # Natural width per column from the widest cell (sampled/capped).
    natural = []
    for c in range(ncols):
        widest = _MIN_COL_WIDTH
        for r in rows:
            cell = r[c] if c < len(r) else ''
            w = stringWidth(cell[:60], _CELL_FONT, _CELL_FONT_SIZE) + 10
            if w > widest:
                widest = w
        natural.append(min(widest, _MAX_COL_WIDTH))

    total = sum(natural)
    if total <= usable_width:
        return natural
    # Scale down proportionally to fit the page.
    scale = usable_width / total
    return [max(w * scale, 24) for w in natural]


def _make_table(rows, usable_width, body_style, header_style):
    """Build a styled platypus Table from string rows (row 0 = header)."""
    col_widths = _column_widths(rows, usable_width)

    data = []
    for i, row in enumerate(rows):
        style = header_style if i == 0 else body_style
        cells = [_wrap_cell(row[c] if c < len(row) else '', style)
                 for c in range(len(col_widths))]
        data.append(cells)

    table = Table(data, colWidths=col_widths, repeatRows=1)
    ts = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3b5b8c')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#999999')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 3),
        ('RIGHTPADDING', (0, 0), (-1, -1), 3),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]
    # Alternating row shading for readability.
    for r in range(1, len(data)):
        if r % 2 == 0:
            ts.append(('BACKGROUND', (0, r), (-1, r), colors.HexColor('#f2f5fa')))
    table.setStyle(TableStyle(ts))
    return table


def _build_pdf(output_path, sections):
    """Render ``sections`` (list of ``(heading_or_None, rows)``) to a PDF.

    Chooses landscape orientation when any section is wide.
    """
    styles = getSampleStyleSheet()
    body_style = styles['BodyText'].clone('cellBody')
    body_style.fontName = _CELL_FONT
    body_style.fontSize = _CELL_FONT_SIZE
    body_style.leading = _CELL_FONT_SIZE + 2

    header_style = styles['BodyText'].clone('cellHeader')
    header_style.fontName = _HEADER_FONT
    header_style.fontSize = _CELL_FONT_SIZE
    header_style.leading = _CELL_FONT_SIZE + 2
    header_style.textColor = colors.white

    heading_style = styles['Heading2']

    max_cols = max((max((len(r) for r in rows), default=0)
                    for _, rows in sections), default=0)
    pagesize = landscape(letter) if max_cols >= _LANDSCAPE_COL_THRESHOLD else letter
    usable_width = pagesize[0] - 2 * _MARGIN

    doc = SimpleDocTemplate(
        output_path,
        pagesize=pagesize,
        leftMargin=_MARGIN,
        rightMargin=_MARGIN,
        topMargin=_MARGIN,
        bottomMargin=_MARGIN,
    )

    story = []
    for heading, rows in sections:
        if heading is not None:
            story.append(Paragraph(escape(str(heading)), heading_style))
            story.append(Spacer(1, 4))
        story.append(_make_table(rows, usable_width, body_style, header_style))
        story.append(Spacer(1, 12))

    if not story:
        story.append(Paragraph('This file is empty.', styles['Normal']))

    try:
        doc.build(story)
    except ConversionError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface any reportlab failure
        raise ConversionError(f'Failed to render spreadsheet to PDF: {exc}') from exc


def convert(input_path, output_path):
    """Convert a .csv or .xlsx file to a PDF written to ``output_path``."""
    lower = input_path.lower()
    try:
        if lower.endswith('.xlsx'):
            sheets = _read_xlsx_sheets(input_path)
            sections = [(name, rows) for name, rows in sheets]
        else:  # treat everything else routed here as CSV
            try:
                rows = _read_csv_rows(input_path)
            except OSError as exc:
                raise ConversionError(f'Could not read CSV file: {exc}') from exc
            sections = [(None, rows)] if rows else []
    except ConversionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConversionError(f'Failed to parse spreadsheet: {exc}') from exc

    _build_pdf(output_path, sections)
