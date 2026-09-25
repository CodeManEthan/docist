"""A link_callback for xhtml2pdf that keeps rendering inside the document.

Left alone, xhtml2pdf resolves every ``<img src>``, ``<link href>``,
``@import``, ``@font-face`` and CSS ``url()`` it meets: http(s) URLs are
fetched from the server and anything else is opened as a local path. An
uploaded HTML or Markdown file could therefore make the server request
internal addresses or read its own files into the rendered PDF.

Every converter that calls ``pisa.CreatePDF`` passes ``link_callback=``
:func:`link_callback`. Inline ``data:`` URIs pass through untouched (mammoth
embeds DOCX images that way); every other reference is swapped for an empty
inline resource, so the document still renders, just without it.
"""

# xhtml2pdf falls back to the original URI when the callback returns a falsy
# value, so a blocked reference must map to something truthy and inert.
BLOCKED = 'data:application/octet-stream;base64,'


def link_callback(uri, rel):
    if isinstance(uri, str) and uri.strip().lower().startswith('data:'):
        return uri
    return BLOCKED
