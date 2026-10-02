"""Build a request's RenderOptions from its form (web layer, no route logic).

Each per-request choice that changes how a file renders reaches the renderer
as one :class:`converters.options.RenderOptions`, built here once per request.
A field is validated only on a path that consumes it: routes that render pages
pass ``paper=True`` (the default), OCR paths pass ``ocr=True``. A path that
doesn't read ``language`` never starts Tesseract, and a missing field takes
its default without a probe.

The Word engine: ``from_form(form, user)`` sets ``word_engine='libreoffice'``
when ``metering.tier(user) == 'paid'`` and LibreOffice is installed, and sets
``deadline`` to ``g.request_started + RENDER_BUDGET`` (default 90 s), so time
spent uploading counts against it. ``g.request_started`` is stamped by
``utils.uploads.apply_limit``; when it is absent (a request that step never
reached, or no request at all) ``deadline`` stays None and only the per-call
LibreOffice timeout applies.

Renderer notes (``opts.notes``) reach the user wherever the result goes:
:func:`with_notes` for a browser route's message, :func:`notes_header` for an
API response.
"""
from flask import current_app, g, has_request_context

from converters.options import DEFAULT_PAPER, PAPER_SIZES, RenderOptions
from pdf_ops import ocr as ocr_ops
from pdf_ops import office
from utils.metering import tier

DEFAULT_RENDER_BUDGET = 90

NOTES_HEADER = 'X-Docist-Notes'


class RenderOptionsError(ValueError):
    """Bad render options in a request (the routes answer with a 400)."""


def parse_paper(raw):
    """``'letter'`` or ``'a4'``; missing or blank means Letter."""
    if raw is None or not str(raw).strip():
        return DEFAULT_PAPER
    value = str(raw).strip().lower()
    if value not in PAPER_SIZES:
        raise RenderOptionsError('Paper must be letter or a4.')
    return value


def parse_language(raw):
    """A validated ``'+'``-joined Tesseract spec; missing or blank means ``'eng'``.

    When Tesseract isn't installed, the length-bounded spec is passed on
    unchecked so the renderer's own "OCR requires Tesseract" message reaches
    the user.
    """
    try:
        raw = ocr_ops.bound_language_spec(raw)
        if not raw.strip():
            return 'eng'
        if not ocr_ops.is_available():
            return raw.strip()
        return ocr_ops.validate_language(raw)
    except ValueError as exc:
        raise RenderOptionsError(str(exc)) from exc


def request_deadline():
    """``g.request_started + RENDER_BUDGET``, or None outside a stamped request."""
    if not has_request_context():
        return None
    started = getattr(g, 'request_started', None)
    if started is None:
        return None
    budget = current_app.config.get('RENDER_BUDGET', DEFAULT_RENDER_BUDGET)
    return started + budget


def word_engine(user):
    """``'libreoffice'`` for a paid user when LibreOffice is installed, else ``'reflow'``."""
    if tier(user) == 'paid' and office.available():
        return 'libreoffice'
    return 'reflow'


def from_form(form, user=None, *, paper=True, ocr=False):
    """RenderOptions for one request. Raises :class:`RenderOptionsError`.

    ``user`` is the request's user (None = anonymous); it picks the Word engine.
    """
    opts = RenderOptions(word_engine=word_engine(user), deadline=request_deadline())
    if opts.word_engine == 'libreoffice' and has_request_context():
        # The fallback reflow takes only what a free user could send (§7),
        # across the whole request; docx_converter spends it.
        opts.reflow_max_bytes = current_app.config['MAX_CONTENT_LENGTH']
    if paper:
        opts.paper = parse_paper(form.get('paper'))
    if ocr:
        opts.ocr_language = parse_language(form.get('language'))
    return opts


def with_notes(message, opts):
    """``message`` followed by any renderer notes."""
    if not opts.notes:
        return message
    return ' '.join([message] + [str(n) for n in opts.notes])


def notes_header(response, opts):
    """Put renderer notes on an API response as ``X-Docist-Notes`` (ASCII, one line)."""
    if opts.notes:
        text = ' '.join(str(n) for n in opts.notes)
        text = ' '.join(text.split())  # no CR/LF in a header
        response.headers[NOTES_HEADER] = text.encode('ascii', 'replace').decode('ascii')
    return response
