"""Converter plugin: HTML (.html, .htm) to PDF.

Uses xhtml2pdf (pisa) to render simple, self-contained HTML documents.
JavaScript is ignored, and external resources (remote or local images, CSS,
fonts) are blocked by :mod:`converters.safe_links`; only inline ``data:``
URIs render. This plugin targets straightforward, self-contained documents.
"""
import os

from . import ConversionError
from .safe_links import link_callback

EXTENSIONS = ['.html', '.htm']


def _read_html(input_path):
    """Read an HTML file, trying utf-8 then falling back to latin-1."""
    with open(input_path, 'rb') as fh:
        raw = fh.read()
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('latin-1', errors='replace')


def convert(input_path, output_path):
    """Render the HTML file at input_path to a PDF at output_path."""
    try:
        from xhtml2pdf import pisa
    except ImportError as exc:  # pragma: no cover
        raise ConversionError(
            'xhtml2pdf is not available; cannot convert HTML files.'
        ) from exc

    try:
        source = _read_html(input_path)
    except OSError as exc:
        raise ConversionError(f'Could not read HTML file: {exc}') from exc

    try:
        with open(output_path, 'wb') as out:
            result = pisa.CreatePDF(
                src=source, dest=out, encoding='utf-8', link_callback=link_callback
            )
    except Exception as exc:  # noqa: BLE001 - surface any pisa failure
        raise ConversionError(f'Failed to render HTML to PDF: {exc}') from exc

    # pisa reports trouble via result.err; treat a truthy error count OR an
    # empty/missing output file as a failure.
    produced_output = os.path.exists(output_path) and os.path.getsize(output_path) > 0
    if getattr(result, 'err', 0) or not produced_output:
        raise ConversionError(
            'xhtml2pdf could not render the HTML document '
            '(the file may contain unsupported markup or no renderable content).'
        )
