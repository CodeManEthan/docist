"""Hand-zipped .docx fixtures for the Word engine tests (no python-docx).

The four documents design prelaunch-fixes §8 names for the engine: a 1-page
letter, a 36-page document with a table, an A4 document with a header and a
footer, and one set in Calibri and Cambria. Plus the network-guard document
(external targets of every kind, pointed at a local server) and an ODT.
"""
import zipfile

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
WORD_MAIN = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'

LETTER_TWIPS = (12240, 15840)   # 8.5 x 11 in
A4_TWIPS = (11906, 16838)       # 210 x 297 mm

LETTER_MARKER = 'WordEngineLetterMarker'
TABLE_MARKER = 'WordEngineTableMarker'
HEADER_MARKER = 'WordEngineHeaderMarker'
FOOTER_MARKER = 'WordEngineFooterMarker'
FONT_MARKER = 'WordEngineFontMarker'


def _content_types(extra_overrides=(), extra_defaults=()):
    defaults = ''.join(
        f'<Default Extension="{ext}" ContentType="{ct}"/>' for ext, ct in extra_defaults)
    overrides = ''.join(
        f'<Override PartName="{part}" ContentType="{ct}"/>' for part, ct in extra_overrides)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'{defaults}'
        f'<Override PartName="/word/document.xml" ContentType="{WORD_MAIN}"/>'
        f'{overrides}</Types>'
    )


_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<Relationships xmlns="{PKG}">'
    f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="word/document.xml"/>'
    '</Relationships>'
)


def rels(*items):
    """A ``.rels`` part from ``(id, type, target, external)`` tuples."""
    body = ''.join(
        f'<Relationship Id="{rid}" Type="{REL}/{rtype}" Target="{target}"'
        + (' TargetMode="External"' if external else '') + '/>'
        for rid, rtype, target, external in items)
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{PKG}">{body}</Relationships>')


def para(text, font=None):
    rpr = (f'<w:rPr><w:rFonts w:ascii="{font}" w:hAnsi="{font}"/></w:rPr>' if font else '')
    return f'<w:p><w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def page_break():
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def sect(size, extra=''):
    w, h = size
    return (f'<w:sectPr>{extra}<w:pgSz w:w="{w}" w:h="{h}"/>'
            '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
            'w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>')


def document(body, sect_pr):
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}" xmlns:r="{R}" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<w:body>{body}{sect_pr}</w:body></w:document>'
    )


def write_docx(path, parts):
    """Zip ``{name: text}`` into ``path``; ``[Content_Types].xml`` and the
    package ``.rels`` are added when missing."""
    parts = dict(parts)
    parts.setdefault('[Content_Types].xml', _content_types())
    parts.setdefault('_rels/.rels', _ROOT_RELS)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return path


def letter_1page(path):
    body = para(LETTER_MARKER) + ''.join(para(f'Dear reader, line {i}.') for i in range(12))
    return write_docx(path, {'word/document.xml': document(body, sect(LETTER_TWIPS))})


def table_36pages(path, rows=400):
    """About 36 Letter pages: a heading, a 400-row table, then filler pages."""
    cells = ''.join(
        f'<w:tr><w:tc><w:p><w:r><w:t>{TABLE_MARKER if i == 0 else f"row {i}"}</w:t></w:r></w:p></w:tc>'
        f'<w:tc><w:p><w:r><w:t>value {i * 7}</w:t></w:r></w:p></w:tc></w:tr>'
        for i in range(rows))
    table = ('<w:tbl><w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4"/>'
             '<w:bottom w:val="single" w:sz="4"/><w:insideH w:val="single" w:sz="4"/>'
             '</w:tblBorders></w:tblPr>'
             '<w:tblGrid><w:gridCol w:w="4680"/><w:gridCol w:w="4680"/></w:tblGrid>'
             f'{cells}</w:tbl>')
    filler = ''.join(page_break() + para(f'Filler page {i}') for i in range(26))
    body = para('Thirty-six pages') + table + filler
    return write_docx(path, {'word/document.xml': document(body, sect(LETTER_TWIPS))})


def a4_header_footer(path):
    hdr = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:hdr xmlns:w="{W}" xmlns:r="{R}">{para(HEADER_MARKER)}</w:hdr>')
    ftr = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:ftr xmlns:w="{W}" xmlns:r="{R}">{para(FOOTER_MARKER)}</w:ftr>')
    refs = ('<w:headerReference w:type="default" r:id="rId10"/>'
            '<w:footerReference w:type="default" r:id="rId11"/>')
    body = para('An A4 document') + page_break() + para('Second page')
    return write_docx(path, {
        '[Content_Types].xml': _content_types(extra_overrides=[
            ('/word/header1.xml',
             'application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml'),
            ('/word/footer1.xml',
             'application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml'),
        ]),
        'word/document.xml': document(body, sect(A4_TWIPS, refs)),
        'word/_rels/document.xml.rels': rels(('rId10', 'header', 'header1.xml', False),
                                             ('rId11', 'footer', 'footer1.xml', False)),
        'word/header1.xml': hdr,
        'word/footer1.xml': ftr,
    })


def calibri_cambria(path):
    body = (para(FONT_MARKER + ' in Calibri', font='Calibri')
            + para('Cambria heading text', font='Cambria'))
    return write_docx(path, {'word/document.xml': document(body, sect(LETTER_TWIPS))})


def _picture(rid, attr):
    n = rid[3:]
    return (
        '<w:p><w:r><w:drawing><wp:inline><wp:extent cx="952500" cy="952500"/>'
        f'<wp:docPr id="{n}" name="p{n}"/><a:graphic>'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{n}" name="p{n}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip {attr}="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="952500" cy="952500"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
        '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
    )


def network_guard(path, base_url):
    """External targets of each kind, all at ``base_url``:

    rId5  typed ``hyperlink`` but used as an image's ``r:link`` (the spoof)
    rId6  an ``image`` relationship used as ``r:link``
    rId7  a real hyperlink (kept by the strip; LibreOffice never fetches it)
    rId9  a linked template in settings.xml
    and an INCLUDEPICTURE field, which uses no relationship.
    """
    settings = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:settings xmlns:w="{W}" xmlns:r="{R}"><w:attachedTemplate r:id="rId9"/>'
        '<w:linkStyles/></w:settings>')
    field = (
        '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r><w:instrText xml:space="preserve"> INCLUDEPICTURE "{base_url}/field.png" \\d </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>x</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
    body = (para('Network guard fixture') + _picture('rId5', 'r:link')
            + _picture('rId6', 'r:link')
            + '<w:p><w:hyperlink r:id="rId7"><w:r><w:t>a link</w:t></w:r></w:hyperlink></w:p>'
            + field)
    return write_docx(path, {
        '[Content_Types].xml': _content_types(extra_overrides=[
            ('/word/settings.xml',
             'application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml')]),
        'word/document.xml': document(body, sect(LETTER_TWIPS)),
        'word/_rels/document.xml.rels': rels(
            ('rId5', 'hyperlink', f'{base_url}/spoofed.png', True),
            ('rId6', 'image', f'{base_url}/linked.png', True),
            ('rId7', 'hyperlink', f'{base_url}/link-only', True),
            ('rId8', 'settings', 'settings.xml', False),
        ),
        'word/settings.xml': settings,
        'word/_rels/settings.xml.rels': rels(
            ('rId9', 'attachedTemplate', f'{base_url}/template.dotx', True)),
    })


def odt(path):
    """A minimal OpenDocument text file (it would be saved as ``.docx``)."""
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2">'
        '<office:body><office:text><text:p>An ODT</text:p></office:text></office:body>'
        '</office:document-content>')
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">'
        '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
        '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
        '</manifest:manifest>')
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr('mimetype', 'application/vnd.oasis.opendocument.text',
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr('content.xml', content)
        zf.writestr('META-INF/manifest.xml', manifest)
    return path
