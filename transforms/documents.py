"""Transform plugin: document-to-document conversions.

Registers a family of same-domain (non-PDF) document transforms so the
registry can, for example, turn a Markdown file into HTML, an HTML file into
plain text, or a Word document into Markdown -- without ever routing through
the PDF pivot.

Pairs implemented (see ``TRANSFORMS`` at the bottom):

    Markdown  -> HTML   (.md / .markdown -> .html)   markdown lib
    HTML      -> Markdown (.html / .htm -> .md)      html2text (keeps links)
    HTML      -> Text   (.html / .htm -> .txt)       html2text (clean text)
    DOCX      -> HTML   (.docx -> .html)             mammoth.convert_to_html
    DOCX      -> Markdown (.docx -> .md)             mammoth.convert_to_markdown
    DOCX      -> Text   (.docx -> .txt)              mammoth.extract_raw_text
    RTF       -> Text   (.rtf -> .txt)               striprtf
    Markdown  -> Text   (.md / .markdown -> .txt)    md -> html -> html2text

``mammoth.convert_to_markdown`` is present in the installed mammoth, so the
DOCX -> MD pair uses it directly (no html2text chaining needed). Should a
future mammoth drop it, chain ``convert_to_html`` + html2text instead.

Text I/O is UTF-8 with a latin-1 fallback on read; corrupt/non-DOCX inputs
and non-RTF inputs raise :class:`transforms.TransformError`.
"""
import re
import zipfile

import html2text
import mammoth
import markdown as _markdown

from . import TransformError


# --------------------------------------------------------------------------
# Markdown rendering config -- match converters/markdown_converter's core set.
# --------------------------------------------------------------------------
_MD_EXTENSIONS = ['tables', 'fenced_code', 'sane_lists']

# Minimal, self-contained document CSS echoing markdown_converter's styling.
_CSS = """
body {
    font-family: Helvetica, Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.5;
    color: #222222;
    margin: 2em auto;
    max-width: 46em;
    padding: 0 1em;
}
h1, h2, h3, h4, h5, h6 {
    color: #111111;
    line-height: 1.25;
    margin-top: 1.1em;
    margin-bottom: 0.4em;
    font-weight: bold;
}
h1 { font-size: 2em; border-bottom: 2px solid #cccccc; padding-bottom: 4px; }
h2 { font-size: 1.5em; border-bottom: 1px solid #dddddd; padding-bottom: 3px; }
a { color: #1a5fb4; text-decoration: underline; }
code {
    font-family: "Courier New", Courier, monospace;
    background-color: #f2f2f2;
    padding: 1px 3px;
    border-radius: 3px;
}
pre {
    font-family: "Courier New", Courier, monospace;
    background-color: #f5f5f5;
    border: 1px solid #dddddd;
    border-radius: 4px;
    padding: 8px 10px;
    white-space: pre-wrap;
    word-wrap: break-word;
}
pre code { background-color: transparent; padding: 0; border-radius: 0; }
blockquote {
    margin: 0.6em 0;
    padding: 0.2em 0.9em;
    color: #555555;
    border-left: 4px solid #cccccc;
    background-color: #fafafa;
}
table { border-collapse: collapse; margin: 0.7em 0; }
th, td { border: 1px solid #bbbbbb; padding: 5px 8px; text-align: left; }
th { background-color: #eeeeee; font-weight: bold; }
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


# --------------------------------------------------------------------------
# Small I/O helpers
# --------------------------------------------------------------------------
def _read_text(input_path):
    """Read a text file as UTF-8, falling back to latin-1 for odd bytes."""
    try:
        with open(input_path, 'rb') as fh:
            raw = fh.read()
    except OSError as exc:
        raise TransformError(f"Could not read '{input_path}': {exc}") from exc
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('latin-1', errors='replace')


def _write_text(output_path, text):
    """Write text as UTF-8."""
    try:
        with open(output_path, 'w', encoding='utf-8') as fh:
            fh.write(text)
    except OSError as exc:
        raise TransformError(
            f"Could not write '{output_path}': {exc}"
        ) from exc


def _wrap_html(body_html):
    """Wrap an HTML fragment in a minimal standalone styled document."""
    return _HTML_TEMPLATE.format(css=_CSS, body=body_html or '')


def _new_html2text(*, ignore_links=False, ignore_emphasis=False,
                   ignore_images=False):
    """Build a configured HTML2Text with no hard line wrapping."""
    conv = html2text.HTML2Text()
    conv.body_width = 0            # never hard-wrap output lines
    conv.ignore_links = ignore_links
    conv.ignore_emphasis = ignore_emphasis
    conv.ignore_images = ignore_images
    return conv


# Leading Markdown heading markers ("# ", "## ", ...) that html2text always
# emits for <h1>..<h6>; stripped for the plain-text targets.
_HEADING_MARKER = re.compile(r'^\s{0,3}#{1,6}\s+', re.MULTILINE)


def _html_to_plain_text(html):
    """Render HTML to genuinely marker-free plain text.

    html2text is markdown-flavoured; for ``.txt`` targets we disable links,
    emphasis, images, table pipes and list bullets, then strip the heading
    ``#`` markers it hard-codes -- leaving only textual content.
    """
    conv = _new_html2text(ignore_links=True, ignore_emphasis=True,
                          ignore_images=True)
    conv.ignore_tables = True     # drop the | ... | table grid
    conv.ul_item_mark = ''        # no "*"/"-" bullet markers
    conv.emphasis_mark = ''
    conv.strong_mark = ''
    text = conv.handle(html)
    return _HEADING_MARKER.sub('', text)


def _read_docx_result(input_path, mammoth_func):
    """Run a mammoth reader on a DOCX path, translating bad input to error."""
    try:
        with open(input_path, 'rb') as docx_file:
            return mammoth_func(docx_file)
    except OSError as exc:
        raise TransformError(
            f"Could not read DOCX file '{input_path}': {exc}"
        ) from exc
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise TransformError(
            f"'{input_path}' is not a valid .docx file. It may be corrupt, "
            f"empty, or an older .doc renamed to .docx (original error: "
            f"{exc})."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - surface any mammoth failure
        raise TransformError(
            f"Failed to read '{input_path}' as a Word document. It may be "
            f"corrupt or not a real .docx file (original error: {exc})."
        ) from exc


# --------------------------------------------------------------------------
# Markdown <-> HTML / Text
# --------------------------------------------------------------------------
def _md_to_html_fragment(text):
    try:
        return _markdown.markdown(text, extensions=_MD_EXTENSIONS)
    except Exception as exc:  # noqa: BLE001 - surface markdown failures
        raise TransformError(f"Failed to render Markdown to HTML: {exc}") from exc


def md_to_html(input_path, output_path):
    """Markdown -> standalone styled HTML document."""
    text = _read_text(input_path)
    _write_text(output_path, _wrap_html(_md_to_html_fragment(text)))


def md_to_txt(input_path, output_path):
    """Markdown -> plain text by rendering to HTML then stripping it.

    This strips Markdown syntax (``#``, ``**``, code fences, table pipes)
    rather than passing the source through verbatim.
    """
    text = _read_text(input_path)
    fragment = _md_to_html_fragment(text)
    _write_text(output_path, _html_to_plain_text(fragment))


def html_to_md(input_path, output_path):
    """HTML -> Markdown, preserving links and emphasis."""
    html = _read_text(input_path)
    conv = _new_html2text()  # keep links + emphasis
    _write_text(output_path, conv.handle(html))


def html_to_txt(input_path, output_path):
    """HTML -> clean plain text (no links markup, no emphasis markers)."""
    html = _read_text(input_path)
    _write_text(output_path, _html_to_plain_text(html))


# --------------------------------------------------------------------------
# DOCX -> HTML / Markdown / Text
# --------------------------------------------------------------------------
def docx_to_html(input_path, output_path):
    """DOCX -> standalone styled HTML document (via mammoth)."""
    result = _read_docx_result(input_path, mammoth.convert_to_html)
    _write_text(output_path, _wrap_html(result.value))


def docx_to_md(input_path, output_path):
    """DOCX -> Markdown (via mammoth.convert_to_markdown)."""
    result = _read_docx_result(input_path, mammoth.convert_to_markdown)
    _write_text(output_path, result.value or '')


def docx_to_txt(input_path, output_path):
    """DOCX -> plain text (via mammoth.extract_raw_text)."""
    result = _read_docx_result(input_path, mammoth.extract_raw_text)
    _write_text(output_path, result.value or '')


# --------------------------------------------------------------------------
# RTF -> Text
# --------------------------------------------------------------------------
_RTF_MAGIC = b'{\\rtf'


def _looks_like_rtf(input_path):
    try:
        with open(input_path, 'rb') as fh:
            head = fh.read(16)
    except OSError as exc:
        raise TransformError(f'Could not read RTF file: {exc}') from exc
    if head.startswith(b'\xef\xbb\xbf'):
        head = head[3:]
    return head.lstrip().startswith(_RTF_MAGIC)


def rtf_to_txt(input_path, output_path):
    """RTF -> plain text (via striprtf), rejecting non-RTF input."""
    if not _looks_like_rtf(input_path):
        raise TransformError('not an RTF file')

    from striprtf.striprtf import rtf_to_text

    try:
        with open(input_path, 'rb') as fh:
            raw = fh.read()
        try:
            source = raw.decode('utf-8')
        except UnicodeDecodeError:
            source = raw.decode('latin-1', errors='replace')
        text = rtf_to_text(source, errors='ignore')
    except Exception as exc:  # noqa: BLE001 - surface any striprtf failure
        raise TransformError(f'Failed to parse RTF file: {exc}') from exc

    _write_text(output_path, text)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
TRANSFORMS = {
    ('.md', '.html'): md_to_html,
    ('.markdown', '.html'): md_to_html,
    ('.md', '.txt'): md_to_txt,
    ('.markdown', '.txt'): md_to_txt,
    ('.html', '.md'): html_to_md,
    ('.htm', '.md'): html_to_md,
    ('.html', '.txt'): html_to_txt,
    ('.htm', '.txt'): html_to_txt,
    ('.docx', '.html'): docx_to_html,
    ('.docx', '.md'): docx_to_md,
    ('.docx', '.txt'): docx_to_txt,
    ('.rtf', '.txt'): rtf_to_txt,
}
