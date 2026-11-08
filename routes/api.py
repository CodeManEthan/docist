"""Versioned REST API (/api/v1) plus the human-readable docs page at /api.

Every endpoint here is a thin file-in / file-out wrapper around the very same
pure helpers the HTML tool pages use (``pdf_ops``, ``converters``,
``transforms``) -- no processing logic is duplicated.  Three things differ from
the browser routes on purpose:

  * the response *is* the file (``send_file`` with a proper ``download_name``),
    not JSON pointing at ``/download``;
  * nothing is written to OUTPUT_FOLDER -- all work happens inside a
    ``tempfile.TemporaryDirectory``, in the request's job (pdf_ops/jobs.py),
    so API traffic leaves no residue on disk. Every endpoint streams the
    finished file from an open handle;
  * every failure is JSON ``{"error": ...}`` with 400 (user-fixable) or 500
    (unexpected), so clients never have to parse HTML.

Auth: every POST needs ``Authorization: Bearer <key>`` with a per-user API
key created at /account (401 without one, or with a wrong or revoked key),
unless the operator sets ``DOCIST_API_ANONYMOUS=1``.  The session cookie is
never read here -- see utils/identity.py.  ``GET /api/v1/formats`` and the
docs page stay open.

App-wide behaviour still applies: POSTs are rate-limited (429 + ``Retry-After``),
each successful POST counts against the caller's daily limit (429 with
``code: daily_limit`` once spent; see utils/metering.py), and requests larger
than ``DOCIST_MAX_UPLOAD_MB`` are rejected with a 413.
"""
import contextlib
import logging
import mimetypes
import os
import tempfile
import zipfile

from flask import Blueprint, jsonify, render_template, request, send_file
from pypdf import PdfReader
from werkzeug.utils import secure_filename

from converters import get_converter, supported_extensions
from pdf_ops.export import pdf_to_images, pdf_to_text_report
from pdf_ops.imposition import booklet_pdf, nup_pdf
from pdf_ops.limits import LimitError
from pdf_ops.merge import OptionsError, SourceError, convert_and_merge, parse_options
from pdf_ops.optimize import compress_pdf
from pdf_ops.pages import (
    extract_pages,
    parse_page_ranges,
    remove_pages,
    rotate_pages,
    split_pdf,
)
from pdf_ops.security import protect_pdf, unlock_pdf
from pdf_ops.stamp import apply_bates_numbers, apply_header_footer
from pdf_ops.watermark import apply_text_watermark
from transforms import (
    TransformError,
    get_transform,
    matrix,
    renders_pages,
    supported_sources,
    targets_for,
    uses_ocr,
)
from converters.options import PAPER_SIZES
from pdf_ops.ocr import UNAVAILABLE_HINT, installed_languages, make_searchable
from pdf_ops.ocr import is_available as ocr_is_available
from pdf_ops.ocr_langs import language_choices
from utils.identity import current_user
from utils.render_opts import (
    RenderOptionsError,
    from_form,
    notes_header,
    run_in_job,
    take_notes,
)
from utils.uploads import (
    charge_uploads,
    convert_large_ok,
    merge_large_ok,
    merge_plan,
    upload_ext,
)
from utils.validation import UploadValidationError, validate_upload
from routes.errors import server_error

bp = Blueprint('api', __name__)

# Every POST endpoint, as /api/v1/formats lists it under ``operations``.
OPERATIONS = [
    '/api/v1/merge',
    '/api/v1/convert',
    '/api/v1/pages/extract',
    '/api/v1/pages/split',
    '/api/v1/pages/rotate',
    '/api/v1/pages/remove',
    '/api/v1/pages/compress',
    '/api/v1/pages/ocr',
    '/api/v1/print/nup',
    '/api/v1/print/booklet',
    '/api/v1/watermark',
    '/api/v1/stamp/header-footer',
    '/api/v1/stamp/bates',
    '/api/v1/export',
    '/api/v1/protect',
    '/api/v1/unlock',
]


class ApiError(Exception):
    """A user-fixable problem with the request; rendered as JSON + status."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------
def _stream_path(path, download_name=None):
    """Stream a file produced inside the temp dir back without reading it into
    memory. The open handle outlives the TemporaryDirectory's removal (POSIX
    keeps an unlinked file's data until its last handle closes), and the
    response closes the handle when it has been sent. Every endpoint answers
    with it (design launch-hardening §10.4, Rule F)."""
    name = download_name or os.path.basename(path)
    mimetype = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    handle = open(path, 'rb')
    try:
        size = os.fstat(handle.fileno()).st_size
        response = send_file(handle, mimetype=mimetype, as_attachment=True,
                             download_name=name, conditional=False)
    except Exception:
        handle.close()
        raise
    response.content_length = size
    return response


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------
def _norm_ext(raw):
    """Normalise a user-supplied extension to lowercase '.xyz' or '' if bad."""
    if not raw:
        return ''
    ext = raw.strip().lower()
    if not ext:
        return ''
    if not ext.startswith('.'):
        ext = '.' + ext
    body = ext[1:]
    if not body or not body.isalnum():
        return ''
    return ext


def _require_upload(field='file'):
    """Return the uploaded FileStorage for ``field`` or raise ApiError(400)."""
    upload = request.files.get(field)
    if upload is None or not upload.filename:
        raise ApiError(f"No file provided (send a multipart '{field}' field).")
    return upload


def _save_pdf(upload, tmpdir):
    """Save a single .pdf upload into ``tmpdir``; return (path, base name).

    Validates both the extension and the file's magic bytes so a renamed
    binary can't ride in as a PDF.
    """
    filename = secure_filename(upload.filename) or 'document.pdf'
    if os.path.splitext(filename)[1].lower() != '.pdf':
        raise ApiError('Only .pdf files are supported by this endpoint.')
    path = os.path.join(tmpdir, filename)
    upload.save(path)
    try:
        validate_upload(path, '.pdf')
    except UploadValidationError as exc:
        raise ApiError(str(exc))
    return path, (os.path.splitext(filename)[0] or 'document')


def _page_count(path):
    """Pages in the PDF at ``path``; runs inside the request's job."""
    try:
        return len(PdfReader(path).pages)
    except MemoryError:
        raise
    except Exception:
        raise ValueError('Could not read the PDF file.')


def _int_field(name, default, low, high, label):
    """A whole-number form field in [low, high]; ``default`` when blank."""
    raw = request.form.get(name)
    if raw is None or str(raw).strip() == '':
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ApiError(f'{label} must be a whole number.')
    if value < low or value > high:
        raise ApiError(f'{label} must be between {low} and {high}.')
    return value


def _paper():
    """The ``paper`` form field (letter|a4, default letter)."""
    try:
        return from_form(request.form, current_user()).paper
    except RenderOptionsError as exc:
        raise ApiError(str(exc))


def _flag(name):
    """A checkbox-style form field: '1', 'true', 'on' or 'yes' is on."""
    return str(request.form.get(name, '')).strip().lower() in {'1', 'true', 'on', 'yes'}


def _one_pdf(upload, what, out_suffix, operate, count_pages=True):
    """One PDF in, one file out (``<name><out_suffix>``), the shape every
    tool endpoint here has.

    ``operate(input_path, out_path, page_count)`` runs in the request's job,
    after the upload has passed its extension and magic-bytes checks and the
    PDF has been opened once (``count_pages=False`` skips that and passes
    None: an encrypted PDF can't be read before it is unlocked). Options that
    read only the form are checked by the caller before this, so a bad one
    answers 400 without a job."""
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path, base = _save_pdf(upload, tmpdir)
        out_path = os.path.join(tmpdir, f'{base}{out_suffix}')

        def work():
            count = _page_count(input_path) if count_pages else None
            operate(input_path, out_path, count)

        try:
            run_in_job(work, tmpdir)
        except ValueError as exc:   # LimitError is a ValueError
            raise ApiError(str(exc))
        except Exception:
            return server_error(f'api {what} failed')

        return _stream_path(out_path)


@bp.errorhandler(ApiError)
def _handle_api_error(exc):
    return jsonify({'error': str(exc)}), exc.status


# ---------------------------------------------------------------------------
# Docs page
# ---------------------------------------------------------------------------
@bp.route('/api')
def api_docs():
    """Human-readable documentation for the /api/v1 endpoints."""
    return render_template('api.html')


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
@bp.route('/api/v1/formats')
def api_formats():
    """What the API accepts: its operations, merge inputs, the conversion
    matrix, papers and OCR languages."""
    return jsonify({
        'operations': OPERATIONS,
        'merge_extensions': ['.pdf'] + supported_extensions(),
        'convert_sources': supported_sources(),
        'convert_matrix': matrix(),
        'paper': list(PAPER_SIZES),
        'ocr_languages': language_choices(installed_languages()),
    })


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------
@bp.route('/api/v1/merge', methods=['POST'])
def api_merge():
    """Merge many uploads (PDF or convertible) into one PDF.

    Form fields mirror the merge UI exactly (see pdf_ops.merge.parse_options):
    ``page_numbers``, ``number_position``, ``start_number``, ``blank_pages``,
    ``bookmarks``, ``mode`` (standard|interleave), ``reverse_second``, plus
    ``paper`` (letter|a4) for converted files.
    """
    files = request.files.getlist('files[]')
    if not files or all(not f or not f.filename for f in files):
        raise ApiError("No files provided (send multipart 'files[]' fields).")

    try:
        options = parse_options(request.form)
        render_opts = from_form(request.form, current_user())
    except (OptionsError, RenderOptionsError) as exc:
        raise ApiError(str(exc))
    try:
        charge_uploads((f, merge_large_ok(upload_ext(f), render_opts))
                       for f in files if f and f.filename)
    except UploadValidationError as exc:
        raise ApiError(str(exc))

    # Every file's extension is checked before any is saved; one that is
    # neither a PDF nor convertible refuses the whole request (§13).
    try:
        plan = merge_plan(files, get_converter)
    except UploadValidationError as exc:
        raise ApiError(str(exc))

    with tempfile.TemporaryDirectory() as tmpdir:
        items = []  # (path, ext, bookmark title, name shown in errors)
        first_filename = None

        for upload, relative, ext, title, filename in plan:
            # Each upload in its own directory: secure_filename can collapse
            # distinct names to the same string.
            path = os.path.join(tmpdir, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            upload.save(path)
            try:
                validate_upload(path, ext)
            except UploadValidationError as exc:
                raise ApiError(f'{filename}: {exc}')
            items.append((path, ext, title, filename))

            if first_filename is None:
                first_filename = filename

        if not items:
            raise ApiError('No supported files provided.')

        # Interleave pairs exactly two sources (fronts + backs).
        if options.get('mode') == 'interleave' and len(items) != 2:
            raise ApiError(
                'Interleave mode requires exactly 2 files (a fronts file and '
                f'a backs file); got {len(items)}.'
            )

        base = os.path.splitext(first_filename)[0] or 'document'
        merged = os.path.join(tmpdir, 'merged.pdf')
        try:
            done = run_in_job(
                lambda: convert_and_merge(items, options, render_opts, merged), tmpdir)
        except (LimitError, SourceError, OptionsError) as exc:
            raise ApiError(str(exc))
        except Exception:
            return server_error('api merge failed')
        take_notes(render_opts, done['notes'])
        return notes_header(_stream_path(merged, f'{base}-merged.pdf'), render_opts)


# ---------------------------------------------------------------------------
# Convert (transforms registry)
# ---------------------------------------------------------------------------
@bp.route('/api/v1/convert', methods=['POST'])
def api_convert():
    """Convert one file to ``target`` (e.g. '.png') using the transforms registry.

    A transform may legitimately write a *different* extension than requested --
    a multi-page ``pdf -> png`` yields a single ``.zip`` bundle.  The registry
    returns the path it actually wrote and that name is what comes back in
    ``Content-Disposition``, so clients should trust the returned filename.
    """
    upload = _require_upload('file')

    filename = secure_filename(upload.filename) or 'document'
    src_ext = os.path.splitext(filename)[1].lower()

    sources = supported_sources()
    if src_ext not in sources:
        shown = ', '.join(sources[:12]) if sources else '(none available yet)'
        raise ApiError(
            f"Can't convert {src_ext or 'files without an extension'}. "
            f'Supported inputs: {shown}.'
        )

    target = _norm_ext(request.form.get('target'))
    if not target:
        raise ApiError("Choose a target format (e.g. target='.png').")

    allowed = targets_for(src_ext)
    if target not in allowed:
        shown = ', '.join(allowed) if allowed else '(none)'
        raise ApiError(
            f"Can't convert {src_ext} to {target}. Available targets: {shown}."
        )

    transform = get_transform(src_ext, target)
    if transform is None:  # pragma: no cover - targets_for gates this
        raise ApiError(f'No converter for {src_ext} -> {target}.')

    try:
        render_opts = from_form(request.form, current_user(),
                                paper=renders_pages(src_ext, target),
                                ocr=uses_ocr(src_ext, target))
    except RenderOptionsError as exc:
        raise ApiError(str(exc))
    try:
        charge_uploads([(upload, convert_large_ok(src_ext, target, render_opts))])
    except UploadValidationError as exc:
        raise ApiError(str(exc))

    stem = os.path.splitext(filename)[0] or 'document'

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, filename)
        upload.save(input_path)
        try:
            validate_upload(input_path, src_ext)
        except UploadValidationError as exc:
            raise ApiError(str(exc))

        requested_output = os.path.join(tmpdir, f'{stem}{target}')

        def work():
            path = transform(input_path, requested_output, render_opts)
            return {'path': path, 'notes': list(render_opts.notes)}

        try:
            done = run_in_job(work, tmpdir)
        except (LimitError, TransformError) as exc:
            raise ApiError(str(exc))
        except Exception:
            return server_error('api convert failed')

        take_notes(render_opts, done['notes'])
        return notes_header(_stream_path(done['path']), render_opts)


# ---------------------------------------------------------------------------
# Page tools
# ---------------------------------------------------------------------------
@bp.route('/api/v1/pages/extract', methods=['POST'])
def api_pages_extract():
    """Extract 1-based page ranges (e.g. '1-3,5') into a new PDF."""
    upload = _require_upload('file')

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path, base = _save_pdf(upload, tmpdir)
        ranges = request.form.get('ranges')
        out_path = os.path.join(tmpdir, f'{base}_extracted.pdf')

        def work():
            count = _page_count(input_path)
            indices = parse_page_ranges(ranges, count)
            extract_pages(input_path, out_path, indices)

        try:
            run_in_job(work, tmpdir)
        except ValueError as exc:
            raise ApiError(str(exc))

        return _stream_path(out_path)


@bp.route('/api/v1/pages/split', methods=['POST'])
def api_pages_split():
    """Split a PDF into parts and return them bundled as one .zip.

    ``mode='every_n'`` takes ``value`` as a page count per part;
    ``mode='ranges'`` takes ``value`` as ';'-separated range specs
    (e.g. '1-2;3-4'), one output file per spec.
    """
    upload = _require_upload('file')
    mode = (request.form.get('mode') or '').strip().lower()
    raw_value = request.form.get('value', '')

    if mode == 'every_n':
        value = raw_value
    elif mode == 'ranges':
        value = [s for s in (part.strip() for part in str(raw_value).split(';')) if s]
        if not value:
            raise ApiError("Provide one or more ranges (separate with ';').")
    else:
        raise ApiError("Choose a split mode: 'every_n' or 'ranges'.")

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path, base = _save_pdf(upload, tmpdir)
        zip_path = os.path.join(tmpdir, f'{base}_split.zip')

        def work():
            _page_count(input_path)
            parts_dir = tempfile.mkdtemp(prefix='parts-')   # in the job directory
            parts = split_pdf(input_path, parts_dir, mode, value)
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for part in parts:
                    zf.write(part, arcname=os.path.basename(part))

        try:
            run_in_job(work, tmpdir)
        except ValueError as exc:
            raise ApiError(str(exc))

        return _stream_path(zip_path)


@bp.route('/api/v1/pages/rotate', methods=['POST'])
def api_pages_rotate():
    """Rotate pages clockwise by ``angle`` (90|180|270). ``ranges`` (e.g.
    '1-3,5') picks the pages; blank rotates every page."""
    upload = _require_upload('file')
    try:
        angle = int(request.form.get('angle', ''))
    except (TypeError, ValueError):
        raise ApiError('Rotation angle must be 90, 180 or 270.')
    ranges = request.form.get('ranges') or ''

    def operate(input_path, out_path, count):
        indices = parse_page_ranges(ranges, count) if ranges.strip() else None
        rotate_pages(input_path, out_path, angle, indices)

    return _one_pdf(upload, 'rotate', '_rotated.pdf', operate)


@bp.route('/api/v1/pages/remove', methods=['POST'])
def api_pages_remove():
    """Remove 1-based page ranges (e.g. '2,4-5'); at least one page must remain."""
    upload = _require_upload('file')
    ranges = request.form.get('ranges')

    def operate(input_path, out_path, count):
        remove_pages(input_path, out_path, parse_page_ranges(ranges, count))

    return _one_pdf(upload, 'remove', '_trimmed.pdf', operate)


@bp.route('/api/v1/pages/compress', methods=['POST'])
def api_pages_compress():
    """Recompress embedded images: ``image_quality`` (JPEG, 10-95, default 60)
    and ``image_max_dpi`` (72-300, default 150). A file that would grow comes
    back unchanged."""
    upload = _require_upload('file')
    quality = _int_field('image_quality', 60, 10, 95, 'Image quality')
    max_dpi = _int_field('image_max_dpi', 150, 72, 300, 'Max image DPI')

    def operate(input_path, out_path, count):
        compress_pdf(input_path, out_path, image_quality=quality, image_max_dpi=max_dpi)

    return _one_pdf(upload, 'compress', '_compressed.pdf', operate)


@bp.route('/api/v1/pages/ocr', methods=['POST'])
def api_pages_ocr():
    """Add an invisible OCR text layer: ``language`` (Tesseract code(s),
    '+'-joined, default 'eng'), ``deskew`` and ``force`` (re-OCR pages that
    already have text) as on/off flags. 400 when Tesseract isn't installed."""
    upload = _require_upload('file')
    if not ocr_is_available():
        raise ApiError(UNAVAILABLE_HINT)
    try:
        language = from_form(request.form, current_user(), paper=False,
                             ocr=True).ocr_language
    except RenderOptionsError as exc:
        raise ApiError(str(exc))
    deskew = _flag('deskew')
    force = _flag('force')

    def operate(input_path, out_path, count):
        make_searchable(input_path, out_path, language=language,
                        deskew=deskew, force=force)

    return _one_pdf(upload, 'ocr', '_searchable.pdf', operate)


# ---------------------------------------------------------------------------
# Print prep
# ---------------------------------------------------------------------------
@bp.route('/api/v1/print/nup', methods=['POST'])
def api_print_nup():
    """Impose ``n`` (2|4) pages per sheet on ``paper`` (letter|a4)."""
    upload = _require_upload('file')
    try:
        n = int(request.form.get('n', ''))
    except (TypeError, ValueError):
        raise ApiError('Pages per sheet must be 2 or 4.')
    paper = _paper()

    def operate(input_path, out_path, count):
        nup_pdf(input_path, out_path, n=n, paper=paper)

    return _one_pdf(upload, 'nup', f'_{n}up.pdf', operate)


@bp.route('/api/v1/print/booklet', methods=['POST'])
def api_print_booklet():
    """Impose a saddle-stitch booklet on ``paper`` (letter|a4), padded to a
    multiple of 4 pages. Print duplex, flip on the short edge, fold."""
    upload = _require_upload('file')
    paper = _paper()

    def operate(input_path, out_path, count):
        booklet_pdf(input_path, out_path, paper=paper)

    return _one_pdf(upload, 'booklet', '_booklet.pdf', operate)


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------
@bp.route('/api/v1/watermark', methods=['POST'])
def api_watermark():
    """Stamp ``text`` onto every page (position/opacity/font_size/rotation)."""
    upload = _require_upload('file')

    text = request.form.get('text', '')
    if not text.strip():
        raise ApiError('Watermark text is required.')

    position = request.form.get('position', 'center')
    opacity = request.form.get('opacity', 0.15)
    font_size = request.form.get('font_size', 48)
    rotation = request.form.get('rotation', 45)

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path, base = _save_pdf(upload, tmpdir)
        out_path = os.path.join(tmpdir, f'{base}-watermarked.pdf')
        try:
            run_in_job(lambda: apply_text_watermark(
                input_path, out_path, text,
                position=position, opacity=opacity,
                font_size=font_size, rotation=rotation,
            ), tmpdir)
        except ValueError as exc:
            raise ApiError(str(exc))
        except Exception:
            return server_error('api watermark failed')

        return _stream_path(out_path)


# ---------------------------------------------------------------------------
# Stamps
# ---------------------------------------------------------------------------
_HEADER_FOOTER_SLOTS = ('header_left', 'header_center', 'header_right',
                        'footer_left', 'footer_center', 'footer_right')


@bp.route('/api/v1/stamp/header-footer', methods=['POST'])
def api_stamp_header_footer():
    """Stamp text into any of the six margin slots (``header_left`` ...
    ``footer_right``; at least one non-blank; ``{page}`` and ``{pages}`` are
    filled in per page), with ``font_size`` (9), ``color`` (#444444) and
    ``margin`` in points (36)."""
    upload = _require_upload('file')
    slots = {name: request.form.get(name, '') for name in _HEADER_FOOTER_SLOTS}
    font_size = request.form.get('font_size', 9)
    color = request.form.get('color', '#444444')
    margin = request.form.get('margin', 36)

    def operate(input_path, out_path, count):
        apply_header_footer(input_path, out_path, font_size=font_size,
                            color=color, margin=margin, **slots)

    return _one_pdf(upload, 'header-footer', '-stamped.pdf', operate)


@bp.route('/api/v1/stamp/bates', methods=['POST'])
def api_stamp_bates():
    """Stamp ``prefix`` plus a counter from ``start`` (1), padded to ``digits``
    (3-10, default 6), at ``position`` (bottom-right|bottom-left|top-right|
    top-left), with ``font_size`` (9) and ``color`` (#000000)."""
    upload = _require_upload('file')
    prefix = request.form.get('prefix', '')
    start = request.form.get('start', 1)
    digits = request.form.get('digits', 6)
    position = request.form.get('position', 'bottom-right')
    font_size = request.form.get('font_size', 9)
    color = request.form.get('color', '#000000')

    def operate(input_path, out_path, count):
        apply_bates_numbers(input_path, out_path, prefix=prefix, start=start,
                            digits=digits, position=position,
                            font_size=font_size, color=color)

    return _one_pdf(upload, 'bates', '-bates.pdf', operate)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
@bp.route('/api/v1/export', methods=['POST'])
def api_export():
    """``operation='images'``: every page as an image, zipped (``fmt`` png|jpg,
    default png; ``dpi`` 30-600, default 150). ``operation='text'``: the text
    layer as UTF-8 text, a ``--- Page N ---`` header per page; ``ocr_fallback``
    (a flag) OCRs pages with no text layer, in ``language`` (default 'eng')."""
    upload = _require_upload('file')
    operation = (request.form.get('operation') or '').strip().lower()

    if operation == 'images':
        fmt = (request.form.get('fmt') or 'png').strip().lower()
        dpi = request.form.get('dpi') or '150'

        def operate(input_path, out_path, count):
            if count < 1:
                raise ValueError('The PDF has no pages to export.')
            images = pdf_to_images(input_path, tempfile.mkdtemp(prefix='images-'),
                                   fmt=fmt, dpi=dpi)
            with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for image in images:
                    zf.write(image, arcname=os.path.basename(image))

        return _one_pdf(upload, 'export', '_images.zip', operate)

    if operation == 'text':
        ocr_fallback = _flag('ocr_fallback')
        language = 'eng'
        if ocr_fallback:
            if not ocr_is_available():
                raise ApiError(UNAVAILABLE_HINT)
            try:
                language = from_form(request.form, current_user(), paper=False,
                                     ocr=True).ocr_language
            except RenderOptionsError as exc:
                raise ApiError(str(exc))

        def operate(input_path, out_path, count):
            if count < 1:
                raise ValueError('The PDF has no pages to export.')
            pdf_to_text_report(input_path, out_path,
                               ocr_fallback=ocr_fallback, language=language)

        return _one_pdf(upload, 'export', '.txt', operate)

    raise ApiError("Choose an operation: 'images' or 'text'.")


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
# The password reaches no log line, error message or response. pdf_ops's own
# refusals are fixed sentences and pass through; any other ValueError (a
# UnicodeEncodeError quotes a character of the password) becomes a fixed
# message inside the job. Nothing else pypdf or cryptography raises carries
# the password, so the 500 branch logs as every route does.
_PASSWORD_REFUSALS = frozenset({
    'Password must not be empty',
    'Incorrect password',
    'PDF is not password-protected',
})


@contextlib.contextmanager
def _pypdf_quiet():
    """Silence pypdf's logger: its warning for a password SASLprep rejects
    names the offending character's code point."""
    logger = logging.getLogger('pypdf')
    level = logger.level
    logger.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        logger.setLevel(level)


def _password_tool(what, out_suffix, run, count_pages):
    upload = _require_upload('file')
    password = request.form.get('password', '')
    if password == '':
        raise ApiError("A password is required (send a 'password' field).")
    refused = f"Docist couldn't {what} this PDF. Check the password, or try another file."

    def operate(input_path, out_path, count):
        with _pypdf_quiet():
            try:
                run(input_path, out_path, password)
            except LimitError:
                raise
            except ValueError as exc:
                message = str(exc) if str(exc) in _PASSWORD_REFUSALS else refused
                raise ValueError(message) from None

    return _one_pdf(upload, what, out_suffix, operate, count_pages=count_pages)


@bp.route('/api/v1/protect', methods=['POST'])
def api_protect():
    """Encrypt with ``password`` (AES-256; required, not logged or echoed)."""
    return _password_tool('protect', '-protected.pdf', protect_pdf, count_pages=True)


@bp.route('/api/v1/unlock', methods=['POST'])
def api_unlock():
    """Write a decrypted copy, given the PDF's ``password`` (required, not
    logged or echoed). A wrong password answers 400 'Incorrect password'."""
    return _password_tool('unlock', '-unlocked.pdf', unlock_pdf, count_pages=False)
