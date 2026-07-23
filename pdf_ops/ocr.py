"""OCR support: availability detection and searchable-PDF creation.

OCR requires the Tesseract system binary (and Ghostscript for OCRmyPDF).
These are system packages, not pip installs, so every entry point that
uses OCR must gate on :func:`is_available` and degrade gracefully —
returning a clear message rather than crashing — when they are missing.
"""
import shutil

# Shown wherever OCR is requested but the system binaries are missing.
UNAVAILABLE_HINT = (
    'OCR requires Tesseract and Ghostscript to be installed on the system.'
)


def is_available():
    """True when the Tesseract and Ghostscript binaries are both present."""
    return shutil.which('tesseract') is not None and shutil.which('gs') is not None


def tesseract_version():
    """Tesseract version string, or None when unavailable."""
    if not is_available():
        return None
    import pytesseract
    try:
        return str(pytesseract.get_tesseract_version())
    except Exception:
        return None


def installed_languages():
    """List of Tesseract language codes installed, or [] when unavailable."""
    if not is_available():
        return []
    import pytesseract
    try:
        return list(pytesseract.get_languages())
    except Exception:
        return []


def _page_count(path):
    """Number of pages in a PDF, or 0 if it can't be read."""
    try:
        from PyPDF2 import PdfReader
        return len(PdfReader(path).pages)
    except Exception:
        return 0


def make_searchable(input_path, output_path, language='eng', deskew=False,
                    force=False):
    """Add an invisible OCR text layer to ``input_path`` -> ``output_path``.

    Turns an image-only (scanned) PDF into a searchable one using OCRmyPDF /
    Tesseract. ``language`` is a Tesseract language code (or '+'-joined codes).

    ``force`` selects how pages that *already* contain text are handled —
    OCRmyPDF refuses to run on such pages unless told what to do:

    * ``force=False`` (default) -> ``skip_text=True``: pages that already have
      a text layer are passed through untouched, so a mixed or fully-text PDF
      survives OCR without error or double-printed glyphs.
    * ``force=True``            -> ``force_ocr=True``: every page is rasterised
      and re-OCR'd, replacing any existing text layer.

    Returns ``{'pages': N, 'language': language}``. Raises ``RuntimeError`` if
    the OCR binaries are missing and ``ValueError`` for bad input / language.
    """
    if not is_available():
        raise RuntimeError(UNAVAILABLE_HINT)

    # Validate every requested language against what Tesseract has installed.
    installed = installed_languages()
    requested = [code.strip() for code in str(language).split('+') if code.strip()]
    if not requested:
        raise ValueError('Provide a Tesseract language code, e.g. "eng".')
    unknown = [code for code in requested if code not in installed]
    if unknown:
        raise ValueError(
            "Unknown OCR language {}. Installed: {}.".format(
                ', '.join(repr(u) for u in unknown),
                ', '.join(sorted(installed)) or '(none)',
            )
        )

    import ocrmypdf
    from ocrmypdf.exceptions import (
        PriorOcrFoundError,
        MissingDependencyError,
    )

    kwargs = dict(language=language, deskew=deskew, progress_bar=False)
    if force:
        kwargs['force_ocr'] = True
    else:
        kwargs['skip_text'] = True

    try:
        ocrmypdf.ocr(input_path, output_path, **kwargs)
    except PriorOcrFoundError as exc:
        raise ValueError(
            'This PDF already has an OCR text layer. Enable '
            '"Force re-OCR" to replace it.'
        ) from exc
    except MissingDependencyError as exc:
        raise ValueError(
            'A required OCR component is missing: {}'.format(exc)
        ) from exc
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('Could not OCR this PDF: {}'.format(exc)) from exc

    return {'pages': _page_count(output_path), 'language': language}
