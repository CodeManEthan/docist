"""Versioned REST API (/api/v1) plus the human-readable docs page at /api.

Every endpoint here is a thin file-in / file-out wrapper around the very same
pure helpers the HTML tool pages use (``pdf_ops``, ``converters``,
``transforms``) -- no processing logic is duplicated.  Three things differ from
the browser routes on purpose:

  * the response *is* the file (``send_file`` with a proper ``download_name``),
    not JSON pointing at ``/download``;
  * nothing is written to OUTPUT_FOLDER -- all work happens inside a
    ``tempfile.TemporaryDirectory``, so API traffic leaves no residue on disk.
    Merge and convert stream the finished file from an open handle; the other
    endpoints still send it from memory (critic review 2026-10-02, blocker 7,
    left for its own round);
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
import io
import mimetypes
import os
import tempfile
import zipfile

from flask import Blueprint, jsonify, render_template, request, send_file
from pypdf import PdfReader
from werkzeug.utils import secure_filename

from converters import get_converter, supported_extensions
from pdf_ops.merge import OptionsError, merge_pipeline, parse_options
from pdf_ops.pages import extract_pages, parse_page_ranges, split_pdf
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
from pdf_ops.ocr import installed_languages
from pdf_ops.ocr_langs import language_choices
from utils.identity import current_user
from utils.render_opts import RenderOptionsError, from_form, notes_header
from utils.uploads import convert_large_ok, file_limit, merge_large_ok
from utils.validation import UploadValidationError, validate_upload

bp = Blueprint('api', __name__)


class ApiError(Exception):
    """A user-fixable problem with the request; rendered as JSON + status."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------
def _send_bytes(data, download_name):
    """Stream finished bytes back as an attachment with a sensible mimetype."""
    mimetype = mimetypes.guess_type(download_name)[0] or 'application/octet-stream'
    return send_file(
        io.BytesIO(data),
        mimetype=mimetype,
        as_attachment=True,
        download_name=download_name,
    )


def _stream_path(path, download_name=None):
    """Stream a file produced inside the temp dir back without reading it into
    memory. The open handle outlives the TemporaryDirectory's removal (POSIX
    keeps an unlinked file's data until its last handle closes), and the
    response closes the handle when it has been sent. Used by merge and
    convert, which take paid-size uploads (round prelaunch-fixes B2)."""
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


def _send_path(path, download_name=None):
    """Read a file produced inside the temp dir and stream it back.

    Reading eagerly (rather than handing ``send_file`` the path) is what makes
    the TemporaryDirectory contract safe: by the time the response is built the
    directory can be torn down.
    """
    with open(path, 'rb') as fh:
        data = fh.read()
    return _send_bytes(data, download_name or os.path.basename(path))


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
    try:
        return len(PdfReader(path).pages)
    except Exception:
        raise ApiError('Could not read the PDF file.')


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
    """What the API accepts: merge inputs, the conversion matrix, papers and OCR languages."""
    return jsonify({
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

    with tempfile.TemporaryDirectory() as tmpdir:
        sources = []  # (pdf_path, bookmark_title)
        first_filename = None

        for index, upload in enumerate(files):
            if not upload or not upload.filename:
                continue
            filename = secure_filename(upload.filename) or f'file{index}'
            ext = os.path.splitext(filename)[1].lower()
            if ext != '.pdf' and not get_converter(ext):
                continue

            # secure_filename can collapse distinct names to the same string;
            # nest each upload so one can't overwrite another.
            slot = os.path.join(tmpdir, str(index))
            os.makedirs(slot, exist_ok=True)
            path = os.path.join(slot, filename)
            title = os.path.splitext(filename)[0]
            upload.save(path)
            try:
                validate_upload(path, ext,
                                max_bytes=file_limit(merge_large_ok(ext, render_opts)))
            except UploadValidationError as exc:
                raise ApiError(f'{filename}: {exc}')

            if ext == '.pdf':
                sources.append((path, title))
            else:
                converted = path + '.converted.pdf'
                try:
                    get_converter(ext)(path, converted, render_opts)
                except Exception as exc:
                    raise ApiError(f'Could not convert {filename}: {exc}')
                sources.append((converted, title))

            if first_filename is None:
                first_filename = filename

        if not sources:
            raise ApiError('No supported files provided.')

        # Interleave pairs exactly two sources (fronts + backs).
        if options.get('mode') == 'interleave' and len(sources) != 2:
            raise ApiError(
                'Interleave mode requires exactly 2 files (a fronts file and '
                f'a backs file); got {len(sources)}.'
            )

        try:
            writer = merge_pipeline(sources, options)
        except OptionsError as exc:
            raise ApiError(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            raise ApiError(str(exc), status=500)

        base = os.path.splitext(first_filename)[0] or 'document'
        merged = os.path.join(tmpdir, 'merged.pdf')
        with open(merged, 'wb') as out:
            writer.write(out)
        del writer
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

    stem = os.path.splitext(filename)[0] or 'document'

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, filename)
        upload.save(input_path)
        try:
            validate_upload(input_path, src_ext, max_bytes=file_limit(
                convert_large_ok(src_ext, target, render_opts)))
        except UploadValidationError as exc:
            raise ApiError(str(exc))

        requested_output = os.path.join(tmpdir, f'{stem}{target}')
        try:
            actual_path = transform(input_path, requested_output, render_opts)
        except TransformError as exc:
            raise ApiError(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            raise ApiError(f'Unexpected error: {exc}', status=500)

        return notes_header(_stream_path(actual_path), render_opts)


# ---------------------------------------------------------------------------
# Page tools
# ---------------------------------------------------------------------------
@bp.route('/api/v1/pages/extract', methods=['POST'])
def api_pages_extract():
    """Extract 1-based page ranges (e.g. '1-3,5') into a new PDF."""
    upload = _require_upload('file')

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path, base = _save_pdf(upload, tmpdir)
        count = _page_count(input_path)
        try:
            indices = parse_page_ranges(request.form.get('ranges'), count)
        except ValueError as exc:
            raise ApiError(str(exc))

        out_path = os.path.join(tmpdir, f'{base}_extracted.pdf')
        try:
            extract_pages(input_path, out_path, indices)
        except ValueError as exc:
            raise ApiError(str(exc))

        return _send_path(out_path)


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
        _page_count(input_path)

        parts_dir = os.path.join(tmpdir, 'parts')
        os.makedirs(parts_dir, exist_ok=True)
        try:
            parts = split_pdf(input_path, parts_dir, mode, value)
        except ValueError as exc:
            raise ApiError(str(exc))

        zip_path = os.path.join(tmpdir, f'{base}_split.zip')
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for part in parts:
                zf.write(part, arcname=os.path.basename(part))

        return _send_path(zip_path)


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
            apply_text_watermark(
                input_path, out_path, text,
                position=position, opacity=opacity,
                font_size=font_size, rotation=rotation,
            )
        except ValueError as exc:
            raise ApiError(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            raise ApiError(str(exc), status=500)

        return _send_path(out_path)
