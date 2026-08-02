#!/usr/bin/env python
"""Generate every file needed for the manual hand-test checklists.

Writes ~25 fixture files into tests/manual/fixtures/ (created if missing,
regenerated on every run). Nothing binary is committed to the repo -- run
this script before a hand-test session:

    .venv/bin/python tests/manual/make_fixtures.py

Uses only libraries already in requirements.txt (reportlab, pypdf, Pillow,
pillow-heif, openpyxl, PyYAML, pypdfium2). Text that must survive rendering
or OCR is drawn with reportlab and rasterized via pypdfium2, so no system
font paths are involved.
"""
import io
import json
import zipfile
from pathlib import Path

import pypdfium2 as pdfium
import yaml
from openpyxl import Workbook
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

FIXTURES = Path(__file__).parent / "fixtures"
LETTER_W, LETTER_H = letter

TEST_PASSWORD = "docist-test"  # password on both encrypted PDFs


# --------------------------------------------------------------------------
# Core builders
# --------------------------------------------------------------------------
def labeled_pdf_bytes(doc_name, pages, accent="#1f6feb"):
    """A PDF whose every page carries a huge, unmistakable label.

    Page text includes the document name and "Page N of M" so reordering,
    rotation, imposition and extraction results can be verified by eye,
    plus a body line so text export has something to find.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for i in range(1, pages + 1):
        c.setFillColor(HexColor(accent))
        c.rect(0, LETTER_H - 40, LETTER_W, 40, fill=1, stroke=0)
        c.setFillColor(HexColor("#ffffff"))
        c.setFont("Helvetica-Bold", 16)
        c.drawString(36, LETTER_H - 28, doc_name)
        c.setFillColor(HexColor("#111111"))
        c.setFont("Helvetica-Bold", 64)
        c.drawCentredString(LETTER_W / 2, LETTER_H / 2, f"Page {i} of {pages}")
        c.setFont("Helvetica", 14)
        c.drawCentredString(
            LETTER_W / 2, LETTER_H / 2 - 60,
            f"{doc_name} body text on page {i}."
        )
        # Corner tick so 90/180/270 rotation is obvious at a glance.
        c.setFont("Helvetica", 12)
        c.drawString(36, 36, "bottom-left corner")
        c.showPage()
    c.save()
    return buf.getvalue()


def write_labeled_pdf(name, doc_name, pages, **kw):
    (FIXTURES / name).write_bytes(labeled_pdf_bytes(doc_name, pages, **kw))


def render_page_image(text_lines, dpi=200, bg="#ffffff", fg="#111111"):
    """Rasterize a one-page reportlab PDF to a PIL image (no system fonts)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    if bg != "#ffffff":
        c.setFillColor(HexColor(bg))
        c.rect(0, 0, LETTER_W, LETTER_H, fill=1, stroke=0)
    c.setFillColor(HexColor(fg))
    y = LETTER_H - 160
    for line, size in text_lines:
        c.setFont("Helvetica-Bold" if size >= 28 else "Helvetica", size)
        c.drawString(72, y, line)
        y -= size * 1.8
    c.showPage()
    c.save()
    doc = pdfium.PdfDocument(buf.getvalue())
    try:
        img = doc[0].render(scale=dpi / 72).to_pil().convert("RGB")
    finally:
        doc.close()
    return img


# --------------------------------------------------------------------------
# Fixture groups
# --------------------------------------------------------------------------
def make_basic_pdfs():
    write_labeled_pdf("single-1p.pdf", "SINGLE PAGE", 1, accent="#16a085")
    write_labeled_pdf("report-12p.pdf", "REPORT", 12)
    write_labeled_pdf("odd-3p.pdf", "DOC A (odd)", 3, accent="#c0392b")
    write_labeled_pdf("even-4p.pdf", "DOC B (even)", 4, accent="#27ae60")
    write_labeled_pdf("big-30p.pdf", "BIG DOC", 30, accent="#8e44ad")


def make_interleave_pair():
    """fronts.pdf reads Sheet 1,3,5; backs-reversed.pdf reads Sheet 6,4,2.

    Interleaving with "second stack is reversed" ON must produce a PDF
    reading Sheet 1..6 in order.
    """
    def stack(name, numbers, accent):
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=letter)
        for n in numbers:
            c.setFillColor(HexColor(accent))
            c.rect(0, LETTER_H - 40, LETTER_W, 40, fill=1, stroke=0)
            c.setFillColor(HexColor("#ffffff"))
            c.setFont("Helvetica-Bold", 16)
            c.drawString(36, LETTER_H - 28, name)
            c.setFillColor(HexColor("#111111"))
            c.setFont("Helvetica-Bold", 64)
            c.drawCentredString(LETTER_W / 2, LETTER_H / 2, f"Sheet {n}")
            c.showPage()
        c.save()
        return buf.getvalue()

    (FIXTURES / "fronts.pdf").write_bytes(
        stack("FRONTS (odd sheets)", [1, 3, 5], "#d35400"))
    (FIXTURES / "backs-reversed.pdf").write_bytes(
        stack("BACKS (even, reversed)", [6, 4, 2], "#2980b9"))


def make_photos_heavy_pdf():
    """4 pages, each a full-page ~240 DPI photo-like JPEG -> compress shows
    real savings when downsampled to 150 DPI / requality'd."""
    import random

    rng = random.Random(42)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for p in range(4):
        w, h = 2040, 2640  # letter at ~240 DPI
        # Coarse noise upscaled bilinearly reads as photo-like texture.
        sw, sh = w // 8, h // 8
        img = Image.frombytes("RGB", (sw, sh), rng.randbytes(sw * sh * 3))
        img = img.resize((w, h), Image.BILINEAR)
        jbuf = io.BytesIO()
        img.save(jbuf, "JPEG", quality=92)
        jbuf.seek(0)
        from reportlab.lib.utils import ImageReader
        c.drawImage(ImageReader(jbuf), 0, 0, LETTER_W, LETTER_H)
        c.setFillColor(HexColor("#ffffff"))
        c.setFont("Helvetica-Bold", 36)
        c.drawString(72, LETTER_H - 90, f"PHOTOS page {p + 1} of 4")
        c.showPage()
    c.save()
    (FIXTURES / "photos-heavy.pdf").write_bytes(buf.getvalue())


def make_scanned_pdf():
    """3 pages of *rasterized* text -- image-only, no text layer.

    OCR (make searchable) must add an invisible layer containing the
    marker words; PDF->text without OCR fallback must come back empty.
    """
    pages = []
    for i in range(1, 4):
        img = render_page_image(
            [
                (f"SCANNED PAGE {i} OF 3", 36),
                ("This page is a picture of text.", 20),
                ("There is no text layer underneath.", 20),
                (f"Unique marker: SCANMARK {i}", 24),
            ],
            dpi=300,
        )
        pages.append(img)
    pages[0].save(
        FIXTURES / "scanned-notext.pdf", "PDF",
        save_all=True, append_images=pages[1:], resolution=300,
    )
    # Sanity: must have zero extractable text.
    reader = PdfReader(FIXTURES / "scanned-notext.pdf")
    assert not any((p.extract_text() or "").strip() for p in reader.pages), \
        "scanned-notext.pdf unexpectedly has a text layer"


def make_encrypted_pdfs():
    base = labeled_pdf_bytes("SECRET DOC", 3, accent="#7f8c8d")
    for name, algo in [
        ("encrypted-aes256.pdf", "AES-256"),
        ("encrypted-rc4.pdf", "RC4-128"),
    ]:
        writer = PdfWriter(clone_from=io.BytesIO(base))
        writer.encrypt(TEST_PASSWORD, algorithm=algo)
        with open(FIXTURES / name, "wb") as fh:
            writer.write(fh)


def make_broken_pdfs():
    # Starts with %PDF- (passes the magic-byte sniff) but is unreadable.
    (FIXTURES / "corrupt.pdf").write_bytes(
        b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog" + b"\x00garbage" * 50)
    # Real PNG bytes named .pdf (fails the magic-byte sniff).
    img = Image.new("RGB", (64, 64), (200, 30, 30))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    (FIXTURES / "fake.pdf").write_bytes(buf.getvalue())


def make_padding_pdf(target_mb=6):
    """A valid PDF a bit over `target_mb` MB (noise JPEGs), for the 413
    oversized-upload test against a server started with
    DOCIST_MAX_UPLOAD_MB=5."""
    import random

    from reportlab.lib.utils import ImageReader

    rng = random.Random(7)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    page = 0
    embedded = 0  # reportlab only fills `buf` on save(), so count JPEGs
    while embedded <= (target_mb - 0.7) * 1024 * 1024:
        page += 1
        w, h = 800, 1040
        img = Image.frombytes("RGB", (w, h), rng.randbytes(w * h * 3))
        jbuf = io.BytesIO()
        img.save(jbuf, "JPEG", quality=95)
        embedded += jbuf.getbuffer().nbytes
        jbuf.seek(0)
        c.drawImage(ImageReader(jbuf), 0, 0, LETTER_W, LETTER_H)
        c.setFillColor(HexColor("#ffffff"))
        c.setFont("Helvetica-Bold", 30)
        c.drawString(72, LETTER_H - 80, f"PADDING page {page}")
        c.showPage()
    c.save()
    (FIXTURES / "padding-6mb.pdf").write_bytes(buf.getvalue())


def make_images():
    def photo(label, bg):
        return render_page_image([(label, 48), ("Docist test image", 24)],
                                 dpi=100, bg=bg, fg="#ffffff")

    photo("PNG PHOTO", "#1f6feb").save(FIXTURES / "photo.png", "PNG")
    photo("JPG PHOTO", "#c0392b").save(FIXTURES / "photo.jpg", "JPEG",
                                       quality=90)
    photo("WEBP PHOTO", "#27ae60").save(FIXTURES / "photo.webp", "WEBP")
    photo("BMP PHOTO", "#8e44ad").save(FIXTURES / "photo.bmp", "BMP")

    import pillow_heif
    pillow_heif.register_heif_opener()
    photo("HEIC PHOTO", "#d35400").save(FIXTURES / "photo.heic",
                                        quality=90)

    # Multi-frame TIFF and animated GIF: 3 labeled frames -> 3 PDF pages
    # when converted; only frame 1 for image->image conversions.
    frames = [
        photo(f"FRAME {i} OF 3", bg)
        for i, bg in [(1, "#16a085"), (2, "#e67e22"), (3, "#2c3e50")]
    ]
    small = [f.resize((425, 550)) for f in frames]
    small[0].save(FIXTURES / "multi.tiff", save_all=True,
                  append_images=small[1:])
    small[0].save(FIXTURES / "animated.gif", save_all=True,
                  append_images=small[1:], duration=700, loop=0)

    # Clean printed text at 300 DPI for image->text OCR.
    render_page_image(
        [
            ("OCR TEST SIGN", 40),
            ("The quick brown fox", 32),
            ("jumps over the lazy dog", 32),
            ("Reference code XJ4872", 32),
        ],
        dpi=300,
    ).save(FIXTURES / "ocr-sign.png", "PNG")


def make_documents():
    (FIXTURES / "sample.md").write_text(
        "# Docist Manual-Test Markdown\n\n"
        "This paragraph has **bold** and *italic* text.\n\n"
        "## A table\n\n"
        "| Tool | Purpose |\n|------|--------|\n"
        "| Merge | Combine files |\n| Convert | Change formats |\n\n"
        "```python\ndef marker():\n    return 'MD-CODE-BLOCK'\n```\n\n"
        "- bullet one\n- bullet two\n",
        encoding="utf-8",
    )
    (FIXTURES / "sample.html").write_text(
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>Docist HTML sample</title></head><body>"
        "<h1>Docist Manual-Test HTML</h1>"
        "<p>A paragraph with <strong>bold</strong> and a "
        "<a href='https://example.com'>link</a>.</p>"
        "<ul><li>alpha</li><li>beta</li></ul>"
        "</body></html>",
        encoding="utf-8",
    )
    long_lines = ["Docist Manual-Test long text file", ""]
    long_lines += [f"Line {i:03d}: the quick brown fox jumps over the lazy dog."
                   for i in range(1, 181)]
    long_lines.append("café ünïcödé naïve — em-dash and accents")
    (FIXTURES / "long.txt").write_text("\n".join(long_lines) + "\n",
                                       encoding="utf-8")
    (FIXTURES / "sample.rtf").write_text(
        r"{\rtf1\ansi\deff0 {\fonttbl {\f0 Helvetica;}}"
        r"\f0\fs28 Docist Manual-Test RTF\par "
        r"Second paragraph with plain text extraction.\par}",
        encoding="ascii",
    )
    # Minimal hand-zipped OOXML .docx (same approach as the pytest suite;
    # python-docx is intentionally not a dependency).
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    paragraphs = [
        ("Docist Manual-Test DOCX", True),
        ("First body paragraph of the sample document.", False),
        ("Second body paragraph with a marker: DOCX-MARKER.", False),
    ]
    body = "".join(
        f"<w:p><w:r>{'<w:rPr><w:b/></w:rPr>' if bold else ''}"
        f"<w:t>{text}</w:t></w:r></w:p>"
        for text, bold in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{ns}"><w:body>{body}'
        "<w:sectPr/></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    with zipfile.ZipFile(FIXTURES / "sample.docx", "w",
                         zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)

    (FIXTURES / "sample.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">'
        '<rect width="400" height="300" fill="#1f6feb"/>'
        '<circle cx="200" cy="120" r="70" fill="#f1c40f"/>'
        '<text x="200" y="250" font-size="28" fill="white" '
        'text-anchor="middle">Docist SVG sample</text></svg>',
        encoding="utf-8",
    )


def make_data_files():
    rows = [
        ["name", "role", "score"],
        ["Ada", "engineer", "97"],
        ["Grace", "admiral", "95"],
        ["Linus", "maintainer", "88"],
    ]
    (FIXTURES / "sample.csv").write_text(
        "\n".join(",".join(r) for r in rows) + "\n", encoding="utf-8")

    header = [f"column_{i:02d}" for i in range(1, 16)]
    wide = [header] + [
        [f"r{r}c{c}" for c in range(1, 16)] for r in range(1, 6)
    ]
    (FIXTURES / "wide.csv").write_text(
        "\n".join(",".join(r) for r in wide) + "\n", encoding="utf-8")

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "People"
    for row in rows:
        ws1.append(row)
    ws2 = wb.create_sheet("Scores")
    ws2.append(["quarter", "total"])
    ws2.append(["Q1", 120])
    ws2.append(["Q2", 145])
    wb.save(FIXTURES / "sample.xlsx")

    data = {
        "project": "Docist",
        "features": ["merge", "convert", "ocr"],
        "limits": {"upload_mb": 50, "rate_per_min": 30},
    }
    (FIXTURES / "sample.json").write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8")
    (FIXTURES / "sample.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    (FIXTURES / "unsupported.xyz").write_text(
        "This extension is not supported by any converter.\n",
        encoding="utf-8",
    )


def main():
    FIXTURES.mkdir(parents=True, exist_ok=True)
    steps = [
        ("basic labeled PDFs", make_basic_pdfs),
        ("interleave pair", make_interleave_pair),
        ("photo-heavy PDF", make_photos_heavy_pdf),
        ("scanned (no text layer) PDF", make_scanned_pdf),
        ("encrypted PDFs", make_encrypted_pdfs),
        ("corrupt + fake PDFs", make_broken_pdfs),
        ("6 MB padding PDF", make_padding_pdf),
        ("images", make_images),
        ("documents", make_documents),
        ("data files", make_data_files),
    ]
    for label, fn in steps:
        print(f"  building {label} ...")
        fn()

    print(f"\nFixtures in {FIXTURES}:")
    total = 0
    for p in sorted(FIXTURES.iterdir()):
        size = p.stat().st_size
        total += size
        print(f"  {size / 1024:9.1f} KB  {p.name}")
    print(f"  {total / 1024 / 1024:9.2f} MB  total")
    print(f"\nEncrypted-PDF password: {TEST_PASSWORD}")


if __name__ == "__main__":
    main()
