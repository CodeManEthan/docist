"""Converter plugin: Rich Text Format (.rtf) -> PDF.

``striprtf`` extracts the *plain text* from the RTF document; all formatting
(fonts, bold/italic, colours, tables) is intentionally lost -- only the
textual content survives. The extracted text is then rendered through the
existing plain-text pipeline (:func:`converters.text_converter.render_text`) so
RTF output looks and paginates exactly like a ``.txt`` conversion.

Garbage/non-RTF input: a real RTF file begins with the control word
``{\\rtf``. Files that do not are rejected with ``ConversionError`` before
any parsing is attempted (striprtf would otherwise silently emit garbage).
"""
from striprtf.striprtf import rtf_to_text

from . import ConversionError, logged_error, upload_name
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
        raise logged_error(
            ConversionError,
            f"Could not read '{upload_name(input_path)}'. Try uploading it again.", exc
        ) from exc
    # Tolerate a UTF-8 BOM and leading whitespace before the brace.
    if head.startswith(b'\xef\xbb\xbf'):
        head = head[3:]
    head = head.lstrip()
    return head.startswith(_RTF_MAGIC)


def convert(input_path, output_path, opts=None):
    """Convert an RTF file to a PDF written to ``output_path``."""
    name = upload_name(input_path)
    if not _looks_like_rtf(input_path):
        raise ConversionError(f"'{name}' is not an RTF file.")

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
        raise logged_error(
            ConversionError,
            f"Could not read the text in '{name}'. It may be damaged; try "
            "saving it again as RTF.", exc
        ) from exc

    # Reuse the plain-text -> PDF pipeline by handing it the extracted text.
    text_converter.render_text(text, output_path, opts, name)
