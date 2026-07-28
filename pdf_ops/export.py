"""PDF export operations: rasterize pages to images, or extract plain text.

Pure PDF-processing functions with no Flask dependency.

* :func:`pdf_to_images` renders each page to a raster image (PNG or JPEG) at a
  chosen DPI using ``pypdfium2``.
* :func:`pdf_to_text` extracts the embedded text layer with pypdf. By default
  there is **no OCR**: a scanned / image-only PDF has no text layer, so those
  pages come out empty (that limitation is surfaced honestly in the UI copy).
  Passing ``ocr_fallback=True`` opts in to OCR: any page whose extracted text
  is empty is rasterized and run through Tesseract, so scanned pages are
  recovered while pages that already carry a text layer are left untouched
  (no needless OCR cost).
* :func:`pdf_to_text_report` does the same work but returns a dict summary
  (``{'output_path', 'pages', 'ocr_pages'}``) that the route uses to report
  how many pages needed OCR. :func:`pdf_to_text` is a thin wrapper over it that
  preserves the historical return value (``output_path``).
"""
import os

import pypdfium2 as pdfium
from pypdf import PdfReader

from pdf_ops.ocr import is_available as _ocr_is_available

# Accepted raster formats and the sane DPI window we render within.
VALID_FORMATS = {"png", "jpg"}
MIN_DPI = 30
MAX_DPI = 600

# Resolution used when rasterizing a page for OCR. 300 DPI is the sweet spot
# for Tesseract accuracy without blowing up render time / memory.
OCR_DPI = 300


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


def pdf_to_text(input_path, output_path, *, ocr_fallback=False, language="eng"):
    """Extract the text layer of a PDF to a UTF-8 ``.txt`` file.

    Pages are separated by a form-feed character followed by a
    ``--- Page N ---`` header. Pages with no extractable text (e.g. scanned
    images) contribute an empty section rather than being skipped, so the page
    numbering in the output always matches the source document.

    Keyword-only ``ocr_fallback`` / ``language`` opt in to OCR: see
    :func:`pdf_to_text_report`. With ``ocr_fallback=False`` (the default) the
    behavior — and the file written — is byte-for-byte identical to before.

    Returns ``output_path``.
    """
    report = pdf_to_text_report(
        input_path, output_path, ocr_fallback=ocr_fallback, language=language
    )
    return report["output_path"]


def pdf_to_text_report(input_path, output_path, *, ocr_fallback=False, language="eng"):
    """Like :func:`pdf_to_text` but return a summary dict for the caller.

    The dict has:

    * ``output_path`` — the path written (same as the argument).
    * ``pages`` — total number of pages in the source document.
    * ``ocr_pages`` — sorted list of 1-based page numbers whose text was
      recovered via OCR (empty when ``ocr_fallback`` is False or every page
      already had a text layer).

    When ``ocr_fallback`` is True, each page whose extracted text is empty or
    whitespace-only is rasterized at :data:`OCR_DPI`, flattened to RGB and run
    through ``pytesseract.image_to_string(lang=language)``. Pages that already
    carry a text layer keep their extracted text and are never re-OCR'd.

    Gating: OCR needs the Tesseract (and Ghostscript) system binaries. If
    ``ocr_fallback`` is requested while they are unavailable, ``ValueError`` is
    raised before any work is done. A bad ``language`` surfaces as a clean
    ``ValueError`` (Tesseract's own error is mapped to a readable message).
    """
    if ocr_fallback and not _ocr_is_available():
        raise ValueError(
            "OCR requires Tesseract and Ghostscript to be installed."
        )

    reader = PdfReader(input_path)
    page_texts = []
    needs_ocr = []  # 0-based indices with no usable text layer
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        page_texts.append(text)
        if ocr_fallback and not text.strip():
            needs_ocr.append(i)

    ocr_pages = []
    if needs_ocr:
        recovered = _ocr_render_pages(input_path, needs_ocr, language)
        for idx in needs_ocr:
            text = recovered.get(idx, "")
            if text.strip():
                page_texts[idx] = text
                ocr_pages.append(idx + 1)

    sections = [f"--- Page {i} ---\n{t}" for i, t in enumerate(page_texts, start=1)]

    # Join pages with a form-feed so downstream readers see a real page break.
    body = "\f".join(sections)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(body)

    return {"output_path": output_path, "pages": len(page_texts), "ocr_pages": ocr_pages}


def _ocr_render_pages(input_path, page_indices, language):
    """OCR the given 0-based page indices, returning ``{index: text}``.

    Renders each page once via pypdfium2 at :data:`OCR_DPI`, flattens to RGB
    and hands it to Tesseract. A Tesseract failure (most commonly a missing
    language pack) is mapped to a clean ``ValueError``.
    """
    import pytesseract

    scale = OCR_DPI / 72.0
    results = {}
    doc = pdfium.PdfDocument(input_path)
    try:
        for i in page_indices:
            page = doc[i]
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil().convert("RGB")
            try:
                results[i] = pytesseract.image_to_string(image, lang=language)
            except pytesseract.TesseractError as exc:
                raise ValueError(_map_tesseract_error(exc, language))
    finally:
        doc.close()
    return results


def _map_tesseract_error(exc, language):
    """Turn a raw ``pytesseract.TesseractError`` into a readable message."""
    msg = str(exc)
    lowered = msg.lower()
    if "failed loading language" in lowered or "error opening data file" in lowered \
            or "could not initialize tesseract" in lowered or "tessdata" in lowered:
        return (
            f"OCR language '{language}' is not available on this system. "
            "Install the matching Tesseract language pack, or use 'eng'."
        )
    return f"OCR failed: {msg}"
