"""Converter plugin: HTML (.html, .htm) to PDF.

Uses xhtml2pdf (pisa) to render simple, self-contained HTML documents.
JavaScript is ignored, and external resources (remote or local images, CSS,
fonts) are blocked by :mod:`converters.safe_links`; only inline ``data:``
URIs render. This plugin targets straightforward, self-contained documents.

Page size: a document that sets its own (``@page { size: ... }`` anywhere in a
``<style>`` block, ``@media`` included) keeps it. Otherwise Docist adds the
chosen paper to pisa's own default CSS. The rule is added only when the
document has no size, because pisa keeps the first page template it sees, so
a default rule beside a document ``@page`` would override the document.
"""
import os
import re

from . import ConversionError
from .options import paper_css
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


_STYLE_RE = re.compile(r'<style\b[^>]*>(.*?)</style\s*>', re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)
# CSS ignores the SGML comment markers old HTML wraps a stylesheet in, and so
# does pisa. Word's "Save as HTML" writes them.
_CDO_CDC_RE = re.compile(r'<!--|-->')


def _statements(css):
    """Split CSS into ``(prelude, block)`` pairs at one nesting level.

    ``block`` is the text between a statement's matching braces, found by
    counting them, or ``None`` for a statement with no block (a declaration
    or an ``@import``). Quoted strings are skipped whole so a brace inside
    one doesn't count.
    """
    out = []
    i, n = 0, len(css)
    while i < n:
        start = i
        quote = None
        while i < n:
            ch = css[i]
            if quote:
                if ch == '\\':
                    i += 1
                elif ch == quote:
                    quote = None
            elif ch in '"\'':
                quote = ch
            elif ch in '{;}':
                break
            i += 1
        prelude = css[start:i]
        if i >= n or css[i] in ';}':
            if prelude.strip():
                out.append((prelude, None))
            i += 1
            continue
        # css[i] == '{': find its match.
        depth = 0
        body_start = i + 1
        quote = None
        while i < n:
            ch = css[i]
            if quote:
                if ch == '\\':
                    i += 1
                elif ch == quote:
                    quote = None
            elif ch in '"\'':
                quote = ch
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append((prelude, css[body_start:i]))
        i += 1
    return out


def _page_block_sets_size(block):
    """True when an ``@page`` block declares ``size`` itself.

    Nested blocks such as ``@frame`` are skipped; only the block's own
    declarations count, and only a property named exactly ``size``.
    """
    for prelude, nested in _statements(block):
        if nested is not None:
            continue
        name, sep, _value = prelude.partition(':')
        if sep and name.strip().lower() == 'size':
            return True
    return False


def css_sets_page_size(css):
    """True when ``css`` has an ``@page`` rule, at any depth, that sets ``size``."""
    for prelude, block in _statements(css):
        if block is None:
            continue
        head = prelude.strip().lower()
        if head.startswith('@page'):
            if _page_block_sets_size(block):
                return True
        elif head.startswith('@') and css_sets_page_size(block):
            # @media, @supports and the like can hold an @page.
            return True
    return False


def document_sets_page_size(source):
    """True when any ``<style>`` block of the HTML sets its own page size."""
    for match in _STYLE_RE.finditer(source):
        css = _CDO_CDC_RE.sub(' ', _COMMENT_RE.sub('', match.group(1)))
        if css_sets_page_size(css):
            return True
    return False


def _default_css(source, opts):
    """pisa's default CSS plus the paper rule, or ``None`` to leave pisa's own.

    ``None`` when the document sets its own size: pisa then uses its own
    default CSS and the document's ``@page``.
    """
    if document_sets_page_size(source):
        return None
    from xhtml2pdf import default
    return default.DEFAULT_CSS + '@page { size: %s; }' % paper_css(opts)


def convert(input_path, output_path, opts=None):
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
                src=source, dest=out, encoding='utf-8', link_callback=link_callback,
                default_css=_default_css(source, opts),
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
