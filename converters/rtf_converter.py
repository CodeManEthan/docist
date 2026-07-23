"""Converter plugin: Rich Text Format (.rtf) -> PDF.

``striprtf`` extracts the *plain text* from the RTF document; all formatting
(fonts, bold/italic, colours, tables) is intentionally lost -- only the
textual content survives. The extracted text is then rendered through the
existing plain-text pipeline (:func:`converters.text_converter.convert`) so
RTF output looks and paginates exactly like a ``.txt`` conversion.

Garbage/non-RTF input: a real RTF file begins with the control word
``{\\rtf``. Files that do not are rejected with ``ConversionError`` before
any parsing is attempted (striprtf would otherwise silently emit garbage).
"""
import os
import tempfile

from striprtf.striprtf import rtf_to_text

from . import ConversionError
from . import text_converter

EXTENSIONS = ['.rtf']

# An RTF document must start with this control word (opt/- leading whitespace).
_RTF_MAGIC = b'{\\rtf'


def _looks_like_rtf(input_path):
    """True if the file begins with the RTF signature (ignoring BOM/space)."""
    try:
        with open(input_path, 'rb') as fh:
            head = fh.read(16)
    except OSError as exc:
        raise ConversionError(f'Could not read RTF file: {exc}') from exc
    # Tolerate a UTF-8 BOM and leading whitespace before the brace.
    if head.startswith(b'\xef\xbb\xbf'):
        head = head[3:]
    head = head.lstrip()
    return head.startswith(_RTF_MAGIC)


def convert(input_path, output_path):
    """Convert an RTF file to a PDF written to ``output_path``."""
    if not _looks_like_rtf(input_path):
        raise ConversionError('not an RTF file')

    try:
        with open(input_path, 'rb') as fh:
            raw = fh.read()
        try:
            source = raw.decode('utf-8')
        except UnicodeDecodeError:
            source = raw.decode('latin-1', errors='replace')
        text = rtf_to_text(source, errors='ignore')
    except ConversionError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface any striprtf failure
        raise ConversionError(f'Failed to parse RTF file: {exc}') from exc

    # Reuse the plain-text -> PDF pipeline by handing it the extracted text.
    tmp_fd, tmp_path = tempfile.mkstemp(suffix='.txt')
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as tmp:
            tmp.write(text)
        text_converter.convert(tmp_path, output_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
