"""PDF export operations: rasterize pages to images, or extract plain text.

Pure PDF-processing functions with no Flask dependency.

* :func:`pdf_to_images` renders each page to a raster image (PNG or JPEG) at a
  chosen DPI using ``pypdfium2``.
* :func:`pdf_to_text` extracts the embedded text layer with PyPDF2. There is
  **no OCR** here: a scanned / image-only PDF has no text layer, so those
  pages come out empty (that limitation is surfaced honestly in the UI copy).
"""
import os

import pypdfium2 as pdfium
from PyPDF2 import PdfReader

# Accepted raster formats and the sane DPI window we render within.
VALID_FORMATS = {"png", "jpg"}
MIN_DPI = 30
MAX_DPI = 600


def pdf_to_images(input_path, output_dir, fmt="png", dpi=150):
    """Render every page of a PDF to an image file in ``output_dir``.

    Files are named ``page_001.<ext>``, ``page_002.<ext>``, ... (1-based,
    zero-padded to at least three digits). Returns the created paths in order.

    ``fmt`` must be ``"png"`` or ``"jpg"``; JPEGs are saved as RGB at quality
    90. ``dpi`` must be an integer-ish value within ``30..600`` — pages are
    rendered at ``scale = dpi / 72``. Raises ``ValueError`` for a bad format or
    an out-of-range DPI.
    """
    fmt = str(fmt).strip().lower()
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt not in VALID_FORMATS:
        raise ValueError("Image format must be 'png' or 'jpg'.")

    try:
        dpi = int(dpi)
    except (TypeError, ValueError):
        raise ValueError(f"DPI must be a whole number between {MIN_DPI} and {MAX_DPI}.")
    if dpi < MIN_DPI or dpi > MAX_DPI:
        raise ValueError(f"DPI must be between {MIN_DPI} and {MAX_DPI}.")

    scale = dpi / 72.0
    pil_format = "PNG" if fmt == "png" else "JPEG"
    ext = "." + fmt

    doc = pdfium.PdfDocument(input_path)
    created = []
    try:
        page_count = len(doc)
        for i in range(page_count):
            page = doc[i]
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil()
            out_path = os.path.join(output_dir, f"page_{i + 1:03d}{ext}")
            if pil_format == "JPEG":
                image.convert("RGB").save(out_path, "JPEG", quality=90)
            else:
                image.save(out_path, "PNG")
            created.append(out_path)
    finally:
        doc.close()

    return created


def pdf_to_text(input_path, output_path):
    """Extract the text layer of a PDF to a UTF-8 ``.txt`` file.

    Pages are separated by a form-feed character followed by a
    ``--- Page N ---`` header. Pages with no extractable text (e.g. scanned
    images) contribute an empty section rather than being skipped, so the page
    numbering in the output always matches the source document.

    Returns ``output_path``.
    """
    reader = PdfReader(input_path)
    sections = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        sections.append(f"--- Page {i} ---\n{text}")

    # Join pages with a form-feed so downstream readers see a real page break.
    body = "\f".join(sections)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(body)

    return output_path
