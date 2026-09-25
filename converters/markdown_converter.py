"""Converter plugin: Markdown (.md, .markdown) -> PDF.

Renders Markdown to HTML with the ``markdown`` library (tables, fenced code,
sane lists), wraps it in a minimal styled HTML document, and converts that to
PDF with ``xhtml2pdf`` (pisa).
"""
import markdown as _markdown
from xhtml2pdf import pisa

from . import ConversionError
from .safe_links import link_callback


EXTENSIONS = ['.md', '.markdown']

# Markdown extensions that make everyday documents render sensibly.
_MD_EXTENSIONS = [
    'tables',
    'fenced_code',
    'sane_lists',
    'nl2br',
]

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
ul, ol {
    margin: 0.5em 0 0.5em 0;
    padding-left: 1.6em;
}
li {
    margin: 0.2em 0;
}
code {
    font-family: "Courier New", Courier, monospace;
    font-size: 10pt;
    background-color: #f2f2f2;
    padding: 1px 3px;
    border-radius: 3px;
}
pre {
    font-family: "Courier New", Courier, monospace;
    font-size: 9.5pt;
    background-color: #f5f5f5;
    border: 1px solid #dddddd;
    border-radius: 4px;
    padding: 8px 10px;
    line-height: 1.35;
    white-space: pre-wrap;
    word-wrap: break-word;
}
pre code {
    background-color: transparent;
    padding: 0;
    border-radius: 0;
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
    """Convert a Markdown file to a PDF written to ``output_path``.

    Raises ``ConversionError`` with a helpful message on failure.
    """
    try:
        with open(input_path, 'r', encoding='utf-8') as fh:
            text = fh.read()
    except UnicodeDecodeError:
        # Fall back to a lenient decode so odd bytes don't kill the job.
        try:
            with open(input_path, 'r', encoding='utf-8', errors='replace') as fh:
                text = fh.read()
        except OSError as exc:
            raise ConversionError(
                f"Could not read Markdown file '{input_path}': {exc}"
            ) from exc
    except OSError as exc:
        raise ConversionError(
            f"Could not read Markdown file '{input_path}': {exc}"
        ) from exc

    try:
        body_html = _markdown.markdown(text, extensions=_MD_EXTENSIONS)
    except Exception as exc:
        raise ConversionError(
            f"Failed to render Markdown to HTML: {exc}"
        ) from exc

    document = _HTML_TEMPLATE.format(css=_CSS, body=body_html)

    try:
        with open(output_path, 'wb') as out_file:
            result = pisa.CreatePDF(
                src=document, dest=out_file, encoding='utf-8',
                link_callback=link_callback,
            )
    except OSError as exc:
        raise ConversionError(
            f"Could not write PDF to '{output_path}': {exc}"
        ) from exc
    except Exception as exc:
        raise ConversionError(
            f"Failed to render PDF from Markdown: {exc}"
        ) from exc

    if result.err:
        raise ConversionError(
            f"PDF rendering reported {result.err} error(s) while converting "
            f"'{input_path}'."
        )
