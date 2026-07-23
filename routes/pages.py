"""Page Tools routes: single-PDF extract / remove / rotate / split.

Uploads are handled in an isolated ``tempfile.TemporaryDirectory`` so they
never collide with the merge flow's UPLOAD_FOLDER (which is wiped on every
merge request). Results are written to OUTPUT_FOLDER and served by the
shared /download endpoint (from routes/merge.py).
"""
import os
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
from PyPDF2 import PdfReader

bp = Blueprint('pages', __name__)

VALID_OPERATIONS = {'extract', 'remove', 'rotate', 'split', 'compress'}


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
    return render_template('pages.html')


def _output_name(base, suffix, ext):
    """Build a collision-friendly output filename like 'doc_extracted.pdf'."""
    return f"{base}_{suffix}{ext}"


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

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, filename)
            upload.save(input_path)

            # Validate it is a readable PDF and get its page count.
            try:
                page_count = len(PdfReader(input_path).pages)
            except Exception:
                return jsonify({'error': 'Could not read the PDF file.'}), 400

            if operation in ('extract', 'remove'):
                indices = parse_page_ranges(request.form.get('ranges'), page_count)
                suffix = 'extracted' if operation == 'extract' else 'trimmed'
                out_name = _output_name(base, suffix, '.pdf')
                out_path = os.path.join(output_folder, out_name)
                if operation == 'extract':
                    extract_pages(input_path, out_path, indices)
                    message = f"Extracted {len(indices)} page(s)."
                else:
                    remove_pages(input_path, out_path, indices)
                    message = f"Removed {len(indices)} page(s)."

            elif operation == 'rotate':
                try:
                    angle = int(request.form.get('angle', ''))
                except (TypeError, ValueError):
                    raise ValueError("Rotation angle must be 90, 180 or 270.")
                ranges = request.form.get('ranges')
                indices = None
                if ranges and ranges.strip():
                    indices = parse_page_ranges(ranges, page_count)
                out_name = _output_name(base, 'rotated', '.pdf')
                out_path = os.path.join(output_folder, out_name)
                rotate_pages(input_path, out_path, angle, indices)
                scope = f"{len(indices)} page(s)" if indices is not None else "all pages"
                message = f"Rotated {scope} by {angle}°."

            elif operation == 'compress':
                def _int_param(name, default, low, high, label):
                    raw = request.form.get(name)
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

                image_quality = _int_param(
                    'image_quality', 60, 10, 95, "Image quality"
                )
                image_max_dpi = _int_param(
                    'image_max_dpi', 150, 72, 300, "Max image DPI"
                )
                out_name = _output_name(base, 'compressed', '.pdf')
                out_path = os.path.join(output_folder, out_name)
                stats = compress_pdf(
                    input_path, out_path,
                    image_quality=image_quality,
                    image_max_dpi=image_max_dpi,
                )
                percent_saved = (1.0 - stats['ratio']) * 100.0
                before = _human_size(stats['original_bytes'])
                after = _human_size(stats['compressed_bytes'])
                if percent_saved < 0.05:
                    message = (
                        f"Already well compressed — kept the original "
                        f"({before}). {stats['images_recompressed']} image(s) "
                        f"recompressed."
                    )
                else:
                    message = (
                        f"Compressed {before} → {after} "
                        f"({percent_saved:.1f}% smaller). "
                        f"{stats['images_recompressed']} image(s) recompressed."
                    )

            else:  # split
                mode = (request.form.get('split_mode') or '').strip().lower()
                raw_value = request.form.get('split_value', '')
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
                parts = split_pdf(input_path, tmpdir, mode, value)
                out_name = _output_name(base, 'split', '.zip')
                out_path = os.path.join(output_folder, out_name)
                with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for part in parts:
                        zf.write(part, arcname=os.path.basename(part))
                message = f"Split into {len(parts)} file(s)."

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
