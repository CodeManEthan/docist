"""Transform plugin: the PDF *bridge* that makes pivot routing possible.

This single module supplies both halves of the ``.pdf`` pivot that the
:mod:`transforms` registry composes automatically (see the package docstring):

* **X -> .pdf** for every extension the :mod:`converters` package can turn into
  a PDF. These pairs are built *programmatically at import time* from
  :func:`converters.supported_extensions`, each one delegating to the matching
  :func:`converters.get_converter`. A converter
  :class:`converters.ConversionError` is re-raised as a
  :class:`transforms.TransformError` with the original message chained.

* **.pdf -> .png / .jpg / .txt** using :mod:`pdf_ops.export`.

Because every listed source already converts to ``.pdf`` here, and this module
also provides ``.pdf -> {png, jpg, txt}``, the registry can compose e.g.
``.md -> .png`` or ``.svg -> .jpg`` through a temporary pivot PDF without any
dedicated plugin.

PDF -> image single-vs-multi-page policy
----------------------------------------
``pdf_to_images`` renders *one image per page*, but a transform has exactly one
``output_path``. So:

* **Single-page PDF** -> the one rendered image is moved to ``output_path`` and
  that path (the requested ``.png`` / ``.jpg``) is returned.
* **Multi-page PDF** -> the per-page images are bundled into a ``.zip`` written
  *next to* ``output_path`` with the same stem but a ``.zip`` suffix (e.g.
  ``out.png`` -> ``out.zip``). That zip path is returned. The registry contract
  explicitly allows a transform to return the path it actually wrote, so callers
  learn the real artifact is a zip. The zip's members are the ``page_NNN.<ext>``
  images in page order.

All rendering is done at **150 DPI**. ``.pdf -> .txt`` extracts the embedded
text layer only -- there is **no OCR**, so image-only/scanned PDFs yield empty
page sections (page numbering is still preserved by ``pdf_to_text``).

Corrupt / unreadable PDF input to any ``.pdf -> *`` transform is surfaced as a
:class:`transforms.TransformError`.
"""
import os
import shutil
import tempfile
import zipfile

import converters
from converters import ConversionError

from pdf_ops.export import pdf_to_images, pdf_to_text

from . import TransformError


IMAGE_DPI = 150


def _make_to_pdf(ext):
    """Build an ``X -> .pdf`` transform delegating to the converter for *ext*.

    Bound via a factory so each closure captures its own extension.
    """
    def to_pdf(input_path, output_path):
        convert = converters.get_converter(ext)
        if convert is None:  # pragma: no cover - registry built from same list
            raise TransformError(f"No converter registered for '{ext}'.")
        try:
            return convert(input_path, output_path)
        except ConversionError as exc:
            raise TransformError(
                f"Could not convert '{ext}' file to PDF: {exc}"
            ) from exc

    to_pdf.__name__ = f"convert_{ext.lstrip('.')}_to_pdf"
    to_pdf.__qualname__ = to_pdf.__name__
    return to_pdf


def _pdf_to_image(input_path, output_path, fmt):
    """Render a PDF to image(s); return the single image OR a multi-page zip.

    See the module docstring for the single-vs-multi-page policy.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                images = pdf_to_images(input_path, tmp, fmt=fmt, dpi=IMAGE_DPI)
            except ValueError:
                # Programming error (bad fmt/dpi) -- not a user input problem.
                raise
            except Exception as exc:
                raise TransformError(
                    f"Could not read PDF for rasterizing: {exc}"
                ) from exc

            if not images:
                raise TransformError("PDF has no pages to render.")

            if len(images) == 1:
                # Single page: hand back the requested output_path itself.
                shutil.move(images[0], output_path)
                return output_path

            # Multi-page: bundle into a zip sitting next to output_path.
            stem = os.path.splitext(output_path)[0]
            zip_path = stem + ".zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for img in images:
                    zf.write(img, arcname=os.path.basename(img))
            return zip_path
    except TransformError:
        raise
    except Exception as exc:  # pragma: no cover - defensive catch-all
        raise TransformError(f"PDF to {fmt} failed: {exc}") from exc


def pdf_to_png(input_path, output_path):
    """.pdf -> .png (150 DPI). Single page: PNG; multi-page: zip of PNGs."""
    return _pdf_to_image(input_path, output_path, "png")


def pdf_to_jpg(input_path, output_path):
    """.pdf -> .jpg (150 DPI). Single page: JPG; multi-page: zip of JPGs."""
    return _pdf_to_image(input_path, output_path, "jpg")


def pdf_to_txt(input_path, output_path):
    """.pdf -> .txt: extract the embedded text layer (no OCR)."""
    try:
        return pdf_to_text(input_path, output_path)
    except Exception as exc:
        raise TransformError(
            f"Could not extract text from PDF: {exc}"
        ) from exc


# --------------------------------------------------------------------------
# Build the plugin's TRANSFORMS dict programmatically at import time.
# --------------------------------------------------------------------------
TRANSFORMS = {}

# X -> .pdf for every convertible extension.
for _ext in converters.supported_extensions():
    TRANSFORMS[(_ext.lower(), ".pdf")] = _make_to_pdf(_ext.lower())

# .pdf -> images / text.
TRANSFORMS[(".pdf", ".png")] = pdf_to_png
TRANSFORMS[(".pdf", ".jpg")] = pdf_to_jpg
TRANSFORMS[(".pdf", ".txt")] = pdf_to_txt
