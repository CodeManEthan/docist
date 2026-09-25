"""Converter plugin: Word documents (.docx) -> PDF.

Converts DOCX to HTML with ``mammoth`` (which handles headings, lists, tables,
bold/italic and inlines images as data URIs), wraps that fragment in a minimal
styled HTML document, and renders it to PDF with ``xhtml2pdf`` (pisa).
"""
import zipfile

import mammoth
from xhtml2pdf import pisa

from . import ConversionError
from .safe_links import link_callback


EXTENSIONS = ['.docx']

# Minimal document CSS for clean, readable typography in the PDF.
_CSS = """
@page {
    size: letter;
    margin: 1in 0.9in;
}
body {
    font-family: Helvetica, Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.5;
    color: #222222;
}
h1, h2, h3, h4, h5, h6 {
    font-family: Helvetica, Arial, sans-serif;
    color: #111111;
    line-height: 1.25;
    margin-top: 1.1em;
    margin-bottom: 0.4em;
    font-weight: bold;
}
h1 { font-size: 22pt; border-bottom: 2px solid #cccccc; padding-bottom: 4px; }
h2 { font-size: 17pt; border-bottom: 1px solid #dddddd; padding-bottom: 3px; }
h3 { font-size: 14pt; }
h4 { font-size: 12pt; }
h5, h6 { font-size: 11pt; }
p {
    margin: 0.5em 0;
}
a {
    color: #1a5fb4;
    text-decoration: underline;
}
strong, b {
    font-weight: bold;
}
em, i {
    font-style: italic;
}
ul, ol {
    margin: 0.5em 0 0.5em 0;
    padding-left: 1.6em;
}
li {
    margin: 0.2em 0;
}
blockquote {
    margin: 0.6em 0;
    padding: 0.2em 0.9em;
    color: #555555;
    border-left: 4px solid #cccccc;
    background-color: #fafafa;
}
table {
    border-collapse: collapse;
    margin: 0.7em 0;
    width: 100%;
}
th, td {
    border: 1px solid #bbbbbb;
    padding: 5px 8px;
    text-align: left;
}
th {
    background-color: #eeeeee;
    font-weight: bold;
}
hr {
    border: none;
    border-top: 1px solid #cccccc;
    margin: 1em 0;
}
img {
    max-width: 100%;
}
"""

_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<style>{css}</style>
</head>
<body>
{body}
</body>
</html>
"""


def convert(input_path, output_path):
    """Convert a DOCX file to a PDF written to ``output_path``.

    Raises ``ConversionError`` with a helpful message on failure, including for
    corrupt files or non-DOCX files masquerading as ``.docx`` (e.g. a legacy
    ``.doc`` renamed).
    """
    # A .docx is a ZIP archive of OOXML parts. mammoth surfaces a variety of
    # exceptions for bad input; catch broadly and translate to ConversionError
    # with a message that hints at the most common cause (wrong format).
    try:
        with open(input_path, 'rb') as docx_file:
            result = mammoth.convert_to_html(docx_file)
    except OSError as exc:
        raise ConversionError(
            f"Could not read DOCX file '{input_path}': {exc}"
        ) from exc
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise ConversionError(
            f"'{input_path}' is not a valid .docx file. It may be corrupt, "
            f"empty, or an older .doc file renamed to .docx (original error: "
            f"{exc})."
        ) from exc
    except Exception as exc:
        raise ConversionError(
            f"Failed to read '{input_path}' as a Word document. It may be "
            f"corrupt or not a real .docx file (original error: {exc})."
        ) from exc

    body_html = result.value or ''

    document = _HTML_TEMPLATE.format(css=_CSS, body=body_html)

    try:
        with open(output_path, 'wb') as out_file:
            pdf_result = pisa.CreatePDF(
                src=document, dest=out_file, encoding='utf-8',
                link_callback=link_callback,
            )
    except OSError as exc:
        raise ConversionError(
            f"Could not write PDF to '{output_path}': {exc}"
        ) from exc
    except Exception as exc:
        raise ConversionError(
            f"Failed to render PDF from '{input_path}': {exc}"
        ) from exc

    if pdf_result.err:
        raise ConversionError(
            f"PDF rendering reported {pdf_result.err} error(s) while "
            f"converting '{input_path}'."
        )
