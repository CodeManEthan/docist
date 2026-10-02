"""OCR support: availability detection and searchable-PDF creation.

OCR requires the Tesseract system binary (and Ghostscript for OCRmyPDF).
These are system packages, not pip installs, so every entry point that
uses OCR must gate on :func:`is_available` and degrade gracefully —
returning a clear message rather than crashing — when they are missing.

Languages: every OCR path checks the requested language with
:func:`validate_language`. A request may name at most ``DOCIST_OCR_MAX_LANGS``
languages (default 2), the combinations the memory budget prices; raising it
is the operator's call. OCRmyPDF runs at most ``DOCIST_OCR_JOBS`` Tesseract
processes at once (default 2).
"""
import functools
import os
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


DEFAULT_MAX_LANGS = 2
DEFAULT_OCR_JOBS = 2


def _env_int(name, default, minimum=1):
    """A positive int from the environment, else ``default``."""
    try:
        value = int(os.environ.get(name, ''))
    except ValueError:
        return default
    return value if value >= minimum else default


def max_languages():
    """How many languages one OCR request may name (``DOCIST_OCR_MAX_LANGS``)."""
    return _env_int('DOCIST_OCR_MAX_LANGS', DEFAULT_MAX_LANGS)


def ocr_jobs():
    """OCRmyPDF's parallel Tesseract processes (``DOCIST_OCR_JOBS``)."""
    return _env_int('DOCIST_OCR_JOBS', DEFAULT_OCR_JOBS)


@functools.lru_cache(maxsize=1)
def _probe_languages():
    """Ask Tesseract for its language list. Cached: an image's packs don't change while it runs."""
    import pytesseract
    try:
        return tuple(pytesseract.get_languages())
    except Exception:
        return ()


def installed_languages():
    """List of Tesseract language codes installed, or [] when unavailable."""
    if not is_available():
        return []
    return list(_probe_languages())


def validate_language(spec):
    """Check a ``'+'``-joined language spec; return it normalised (``'eng+spa'``).

    Each code must be installed, and the count must be at most
    :func:`max_languages`. Raises ``ValueError`` with a user-facing message.
    """
    requested = [code.strip() for code in str(spec or '').split('+') if code.strip()]
    if not requested:
        raise ValueError('Provide a Tesseract language code, e.g. "eng".')
    cap = max_languages()
    if len(requested) > cap:
        raise ValueError('Choose at most {} OCR language{}.'.format(
            cap, '' if cap == 1 else 's'))
    installed = installed_languages()
    unknown = [code for code in requested if code not in installed]
    if unknown:
        raise ValueError(
            "Unknown OCR language {}. Installed: {}.".format(
                ', '.join(repr(u) for u in unknown),
                ', '.join(sorted(installed)) or '(none)',
            )
        )
    return '+'.join(requested)


def _page_count(path):
    """Number of pages in a PDF, or 0 if it can't be read."""
    try:
        from pypdf import PdfReader
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

    language = validate_language(language)

    import ocrmypdf
    from ocrmypdf.exceptions import (
        PriorOcrFoundError,
        MissingDependencyError,
    )

    kwargs = dict(language=language, deskew=deskew, progress_bar=False,
                  jobs=ocr_jobs())
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
