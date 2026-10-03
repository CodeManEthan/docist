"""Page Tools routes: single-PDF extract / remove / rotate / split.

Uploads are handled in an isolated ``tempfile.TemporaryDirectory`` so they
never collide with the merge flow's UPLOAD_FOLDER (which is wiped on every
merge request). Results are written to OUTPUT_FOLDER and served by the
shared /download endpoint (from routes/merge.py).
"""
import os
import shutil
import tempfile
import zipfile

from flask import Blueprint, current_app, render_template, request, jsonify
from werkzeug.utils import secure_filename

from pdf_ops.pages import (
    parse_page_ranges,
    extract_pages,
    remove_pages,
    rotate_pages,
    split_pdf,
)
from pdf_ops.optimize import compress_pdf
from pdf_ops import ocr as ocr_ops
from pdf_ops.ocr_langs import language_choices
from utils.render_opts import from_form, run_in_job
from utils.naming import result_name
from pypdf import PdfReader

bp = Blueprint('pages', __name__)

VALID_OPERATIONS = {'extract', 'remove', 'rotate', 'split', 'compress', 'ocr'}


def _is_truthy(raw):
    """Interpret a form value as a boolean (checkboxes send 'on'/'true'/'1')."""
    return str(raw).strip().lower() in ('1', 'true', 'on', 'yes')


def _human_size(num_bytes):
    """Format a byte count as a short human-readable string (e.g. '1.4 MB')."""
    size = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            if unit == 'B':
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024


@bp.route('/pages')
def pages_index():
    return render_template('pages.html', ocr_available=ocr_ops.is_available(),
                           ocr_languages=language_choices(ocr_ops.installed_languages()))


def _output_name(base, suffix, ext):
    """Build an unguessable output filename like '<key>_doc_extracted.pdf'."""
    return result_name(f"{base}_{suffix}{ext}")


@bp.route('/pages/run', methods=['POST'])
def run_operation():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided.'}), 400

    upload = request.files['file']
    if not upload or upload.filename == '':
        return jsonify({'error': 'No file selected.'}), 400

    filename = secure_filename(upload.filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext != '.pdf':
        return jsonify({'error': 'Only .pdf files are supported here.'}), 400

    operation = (request.form.get('operation') or '').strip().lower()
    if operation not in VALID_OPERATIONS:
        return jsonify({
            'error': "Choose an operation: extract, remove, rotate or split."
        }), 400

    base = os.path.splitext(filename)[0] or 'document'
    output_folder = current_app.config['OUTPUT_FOLDER']
    form = request.form

    def _int_param(name, default, low, high, label):
        raw = form.get(name)
        if raw is None or str(raw).strip() == '':
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{label} must be a whole number.")
        if value < low or value > high:
            raise ValueError(
                f"{label} must be between {low} and {high}."
            )
        return value

    try:
        # Everything that reads only the form is checked here, before the
        # job; everything that reads the PDF runs in the request's job.
        if operation in ('extract', 'remove'):
            ext_out = '.pdf'
            suffix = 'extracted' if operation == 'extract' else 'trimmed'
        elif operation == 'rotate':
            try:
                angle = int(form.get('angle', ''))
            except (TypeError, ValueError):
                raise ValueError("Rotation angle must be 90, 180 or 270.")
            ext_out, suffix = '.pdf', 'rotated'
        elif operation == 'compress':
            image_quality = _int_param('image_quality', 60, 10, 95, "Image quality")
            image_max_dpi = _int_param('image_max_dpi', 150, 72, 300, "Max image DPI")
            ext_out, suffix = '.pdf', 'compressed'
        elif operation == 'ocr':
            if not ocr_ops.is_available():
                return jsonify({'error': ocr_ops.UNAVAILABLE_HINT}), 400
            # RenderOptionsError is a ValueError: a 400 below.
            language = from_form(form, paper=False, ocr=True).ocr_language
            deskew = _is_truthy(form.get('deskew'))
            force = _is_truthy(form.get('force'))
            ext_out, suffix = '.pdf', 'searchable'
        else:  # split
            mode = (form.get('split_mode') or '').strip().lower()
            raw_value = form.get('split_value', '')
            if mode == 'every_n':
                value = raw_value
            elif mode == 'ranges':
                value = [s for s in
                         (part.strip() for part in str(raw_value).split(';'))
                         if s]
                if not value:
                    raise ValueError(
                        "Provide one or more ranges (separate with ';')."
                    )
            else:
                raise ValueError(
                    "Choose a split mode: 'every_n' or 'ranges'."
                )
            ext_out, suffix = '.zip', 'split'
        ranges = form.get('ranges')

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, filename)
            upload.save(input_path)
            # The result is built here, outside the job directory, and moved
            # to OUTPUT_FOLDER only after the job returns.
            out_path = os.path.join(tmpdir, 'result' + ext_out)

            def work():
                # Validate it is a readable PDF and get its page count.
                try:
                    page_count = len(PdfReader(input_path).pages)
                except MemoryError:
                    raise
                except Exception:
                    raise ValueError('Could not read the PDF file.')

                if operation in ('extract', 'remove'):
                    indices = parse_page_ranges(ranges, page_count)
                    if operation == 'extract':
                        extract_pages(input_path, out_path, indices)
                        return f"Extracted {len(indices)} page(s)."
                    remove_pages(input_path, out_path, indices)
                    return f"Removed {len(indices)} page(s)."

                if operation == 'rotate':
                    indices = None
                    if ranges and ranges.strip():
                        indices = parse_page_ranges(ranges, page_count)
                    rotate_pages(input_path, out_path, angle, indices)
                    scope = f"{len(indices)} page(s)" if indices is not None else "all pages"
                    return f"Rotated {scope} by {angle}°."

                if operation == 'compress':
                    stats = compress_pdf(
                        input_path, out_path,
                        image_quality=image_quality,
                        image_max_dpi=image_max_dpi,
                    )
                    percent_saved = (1.0 - stats['ratio']) * 100.0
                    before = _human_size(stats['original_bytes'])
                    after = _human_size(stats['compressed_bytes'])
                    if percent_saved < 0.05:
                        return (
                            f"Already well compressed — kept the original "
                            f"({before}). {stats['images_recompressed']} image(s) "
                            f"recompressed."
                        )
                    return (
                        f"Compressed {before} → {after} "
                        f"({percent_saved:.1f}% smaller). "
                        f"{stats['images_recompressed']} image(s) recompressed."
                    )

                if operation == 'ocr':
                    stats = ocr_ops.make_searchable(
                        input_path, out_path,
                        language=language, deskew=deskew, force=force,
                    )
                    return (
                        f"OCR complete — {stats['pages']} page(s) now searchable "
                        f"({stats['language']})."
                    )

                # split: the parts go in the job directory, the zip in tmpdir.
                parts = split_pdf(input_path, tempfile.mkdtemp(prefix='parts-'), mode, value)
                with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for part in parts:
                        zf.write(part, arcname=os.path.basename(part))
                return f"Split into {len(parts)} file(s)."

            message = run_in_job(work, tmpdir)
            out_name = _output_name(base, suffix, ext_out)
            shutil.move(out_path, os.path.join(output_folder, out_name))

    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({'error': f'Unexpected error: {exc}'}), 500

    return jsonify({
        'success': True,
        'message': message,
        'filename': out_name,
        'download_url': '/download?filename=' + out_name,
    })
