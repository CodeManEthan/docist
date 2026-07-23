"""Shared fixtures for the backend test suite.

Everything here is generated programmatically into ``tmp_path`` -- no binary
sample files are committed to the repo. Helpers build the various source
documents the converters accept, plus a real (reportlab) PDF and a minimal,
hand-zipped OOXML ``.docx`` (python-docx is intentionally NOT a dependency).

The Flask ``app`` module is imported once (module singleton). The ``client``
fixture repoints ``UPLOAD_FOLDER``/``OUTPUT_FOLDER`` at per-test ``tmp_path``
subdirectories so tests never touch the real ``uploads/`` or ``output/`` dirs
(the /upload endpoint clears the upload folder on every request).
"""
import io
import zipfile

import pytest
from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

import app as flask_app_module


# --------------------------------------------------------------------------
# Marker strings -- unique tokens embedded in each source doc so the merged /
# converted output can be spot-checked by text extraction.
# --------------------------------------------------------------------------
MD_HEADING = "MarkdownHeadingZeta"
MD_PARAGRAPH = "MarkdownBodyOmega"
MD_CODE = "MarkdownCodeKappa"
MD_TABLE_CELL = "MdCellPhi"

TXT_MARKER = "PlainTextMarkerNu"
TXT_UNICODE = "café ünïcödé naïve"

HTML_HEADING = "HtmlHeadingTheta"
HTML_BODY = "HtmlBodyLambda"

DOCX_HEADING = "DocxHeadingSigma"
DOCX_BODY = "DocxBodyDelta"
DOCX_SECOND = "DocxSecondPsi"

PDF_MARKER_A = "PdfContentAlpha"
PDF_MARKER_B = "PdfContentBeta"

LETTER_W, LETTER_H = letter  # (612.0, 792.0)
SIZE_TOL = 2.0


# --------------------------------------------------------------------------
# Programmatic file builders
# --------------------------------------------------------------------------
def build_markdown(path):
    """A Markdown doc with a heading, paragraph, table and fenced code block."""
    text = (
        f"# {MD_HEADING}\n\n"
        f"Some {MD_PARAGRAPH} paragraph with **bold** and *italic* text.\n\n"
        "## A table\n\n"
        f"| Name | Value |\n"
        f"|------|-------|\n"
        f"| {MD_TABLE_CELL} | 42 |\n\n"
        "```\n"
        f"def demo():\n    return '{MD_CODE}'\n"
        "```\n"
    )
    path.write_text(text, encoding="utf-8")
    return path


def build_text_rich(path):
    """A text file that spans >2 pages, has a very long unbroken line, unicode."""
    lines = [TXT_MARKER]
    lines += [f"short line number {i:03d}" for i in range(300)]  # forces many pages
    lines.append("W" * 6000)  # a single very long unbroken line -> wraps hard
    lines.append(TXT_UNICODE)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def build_text_longline(path):
    """A file whose sole content is one very long unbroken line."""
    path.write_text(("W" * 6000) + "\n", encoding="utf-8")
    return path


def build_html(path):
    html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
        f"<h1>{HTML_HEADING}</h1>"
        f"<p>{HTML_BODY} with some <strong>emphasis</strong>.</p>"
        "<ul><li>one</li><li>two</li></ul>"
        "</body></html>"
    )
    path.write_text(html, encoding="utf-8")
    return path


def build_png(path):
    """A PNG with an alpha channel."""
    img = Image.new("RGBA", (320, 240), (200, 30, 30, 128))
    img.save(path, "PNG")
    return path


def build_jpg(path):
    """A small opaque JPEG."""
    img = Image.new("RGB", (160, 120), (20, 140, 60))
    img.save(path, "JPEG")
    return path


def build_tiff(path, frames=3):
    """A multi-frame TIFF (one PDF page per frame)."""
    base = Image.new("RGB", (140, 100), (10, 20, 30))
    extra = [
        Image.new("RGB", (140, 100), (60 * (i + 1) % 255, 90, 40 * i % 255))
        for i in range(1, frames)
    ]
    base.save(path, save_all=True, append_images=extra)
    return path


# Minimal OOXML parts for a valid .docx (a ZIP of these three members).
_DOCX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_DOCX_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    "</Relationships>"
)


def _docx_document_xml(paragraphs):
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = []
    for text, bold in paragraphs:
        rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
        body.append(f"<w:p><w:r>{rpr}<w:t>{text}</w:t></w:r></w:p>")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{ns}"><w:body>'
        + "".join(body)
        + "<w:sectPr/></w:body></w:document>"
    )


def build_docx(path, paragraphs=None):
    """Build a minimal but valid .docx by zipping OOXML parts by hand."""
    if paragraphs is None:
        paragraphs = [
            (DOCX_HEADING, True),
            (DOCX_BODY, False),
            (DOCX_SECOND, False),
        ]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        zf.writestr("_rels/.rels", _DOCX_RELS)
        zf.writestr("word/document.xml", _docx_document_xml(paragraphs))
    return path


def build_pdf(path, pages=1, marker="PdfContent"):
    """A real PDF (reportlab) with ``pages`` pages, each stamped with a marker."""
    buf = io.BytesIO()
    c = canvas.Canvas(str(path), pagesize=letter)
    for i in range(pages):
        c.setFont("Helvetica", 14)
        c.drawString(72, 720, f"{marker} page {i + 1}")
        c.showPage()
    c.save()
    del buf
    return path


# --------------------------------------------------------------------------
# Fixtures exposing the builders / built files
# --------------------------------------------------------------------------
@pytest.fixture
def builders():
    """Expose the builder functions + marker constants to tests as one object."""
    import types

    return types.SimpleNamespace(
        markdown=build_markdown,
        text_rich=build_text_rich,
        text_longline=build_text_longline,
        html=build_html,
        png=build_png,
        jpg=build_jpg,
        tiff=build_tiff,
        docx=build_docx,
        pdf=build_pdf,
        MD_HEADING=MD_HEADING,
        MD_PARAGRAPH=MD_PARAGRAPH,
        MD_CODE=MD_CODE,
        MD_TABLE_CELL=MD_TABLE_CELL,
        TXT_MARKER=TXT_MARKER,
        HTML_HEADING=HTML_HEADING,
        HTML_BODY=HTML_BODY,
        DOCX_HEADING=DOCX_HEADING,
        DOCX_BODY=DOCX_BODY,
        DOCX_SECOND=DOCX_SECOND,
        PDF_MARKER_A=PDF_MARKER_A,
        PDF_MARKER_B=PDF_MARKER_B,
    )


@pytest.fixture
def client(tmp_path):
    """Flask test client with UPLOAD/OUTPUT folders redirected into tmp_path."""
    upload_dir = tmp_path / "uploads"
    output_dir = tmp_path / "output"
    upload_dir.mkdir()
    output_dir.mkdir()

    app = flask_app_module.app
    prev_upload = app.config["UPLOAD_FOLDER"]
    prev_output = app.config["OUTPUT_FOLDER"]
    app.config["UPLOAD_FOLDER"] = str(upload_dir)
    app.config["OUTPUT_FOLDER"] = str(output_dir)
    app.config["TESTING"] = True

    test_client = app.test_client()
    # Attach the dirs so tests can inspect the filesystem.
    test_client.upload_dir = upload_dir
    test_client.output_dir = output_dir
    try:
        yield test_client
    finally:
        app.config["UPLOAD_FOLDER"] = prev_upload
        app.config["OUTPUT_FOLDER"] = prev_output


def extract_all_text(pdf_path):
    """Concatenate extract_text() from every page of a PDF."""
    from PyPDF2 import PdfReader

    reader = PdfReader(str(pdf_path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)
