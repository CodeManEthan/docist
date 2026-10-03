"""Watermark & security routes: stamp text or protect/unlock a single PDF.

Uploads are processed inside a ``tempfile.TemporaryDirectory`` -- deliberately
NOT the shared UPLOAD_FOLDER, which the merge flow clears on every request.
The work runs in the request's job (utils.render_opts.run_in_job); the result
is built in the temp directory and moved to OUTPUT_FOLDER only when the job
succeeds, so a refused request leaves nothing there. Results are served by the
shared /download endpoint.
"""
import os
import shutil
import tempfile

from flask import Blueprint, current_app, render_template, request, jsonify
from werkzeug.utils import secure_filename

from pdf_ops.security import protect_pdf, unlock_pdf
from pdf_ops.watermark import apply_text_watermark
from pdf_ops.stamp import apply_header_footer, apply_bates_numbers, format_bates
from utils.naming import result_name
from utils.render_opts import run_in_job
from utils.validation import UploadValidationError, validate_upload

bp = Blueprint('security', __name__)

_OPERATIONS = ('watermark', 'protect', 'unlock', 'headerfooter', 'bates')


@bp.route('/security')
def security():
    return render_template('security.html')


def _output_name(operation, original):
    """An unguessable OUTPUT_FOLDER filename for a processed document."""
    base = os.path.splitext(secure_filename(original) or 'document.pdf')[0] or 'document'
    suffix = {
        'watermark': 'watermarked',
        'protect': 'protected',
        'unlock': 'unlocked',
        'headerfooter': 'stamped',
        'bates': 'bates',
    }[operation]
    return result_name(f'{base}-{suffix}.pdf')


def _bates_first_label(prefix, start, digits):
    """The Bates label for the first stamped page (start/digits already valid)."""
    prefix = '' if prefix is None else str(prefix)
    return format_bates(prefix, int(start), int(digits))


@bp.route('/security/run', methods=['POST'])
def run():
    file = request.files.get('file')
    if file is None or not file.filename:
        return jsonify({'error': 'No file provided'}), 400

    filename = secure_filename(file.filename)
    if os.path.splitext(filename)[1].lower() != '.pdf':
        return jsonify({'error': 'Only .pdf files are supported'}), 400

    operation = request.form.get('operation', '')
    if operation not in _OPERATIONS:
        return jsonify({'error': f'Unknown operation {operation!r}'}), 400

    output_folder = current_app.config['OUTPUT_FOLDER']
    output_name = _output_name(operation, file.filename)
    form = request.form

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, filename)
            file.save(input_path)
            try:
                validate_upload(input_path, '.pdf')
            except UploadValidationError as exc:
                return jsonify({'error': str(exc)}), 400
            # Built here, outside the job directory; moved to OUTPUT_FOLDER
            # only after the job returns (design launch-hardening §10.3).
            output_path = os.path.join(tmpdir, 'result.pdf')

            if operation == 'watermark':
                text = form.get('text', '')
                if not text or not text.strip():
                    return jsonify({'error': 'Watermark text is required'}), 400
                position = form.get('position', 'center')
                opacity = form.get('opacity', 0.15)
                font_size = form.get('font_size', 48)
                rotation = form.get('rotation', 45)

                def work():
                    apply_text_watermark(
                        input_path, output_path, text,
                        position=position, opacity=opacity,
                        font_size=font_size, rotation=rotation,
                    )
                    return 'Watermark applied to every page'
            elif operation == 'headerfooter':
                slots = {
                    'header_left': form.get('header_left', ''),
                    'header_center': form.get('header_center', ''),
                    'header_right': form.get('header_right', ''),
                    'footer_left': form.get('footer_left', ''),
                    'footer_center': form.get('footer_center', ''),
                    'footer_right': form.get('footer_right', ''),
                }
                font_size = form.get('font_size', 9)
                color = form.get('color', '#444444')
                margin = form.get('margin', 36)

                def work():
                    apply_header_footer(
                        input_path, output_path,
                        font_size=font_size, color=color, margin=margin,
                        **slots,
                    )
                    return 'Header/footer applied to every page'
            elif operation == 'bates':
                prefix = form.get('prefix', '')
                start = form.get('start', 1)
                digits = form.get('digits', 6)
                position = form.get('position', 'bottom-right')
                font_size = form.get('font_size', 9)
                color = form.get('color', '#000000')

                def work():
                    last_label = apply_bates_numbers(
                        input_path, output_path,
                        prefix=prefix, start=start, digits=digits,
                        position=position, font_size=font_size, color=color,
                    )
                    first_label = _bates_first_label(prefix, start, digits)
                    return f'Stamped {first_label}–{last_label}'
            elif operation == 'protect':
                # Password is used only to encrypt the user's own file; never logged.
                password = form.get('password', '')

                def work():
                    protect_pdf(input_path, output_path, password)
                    return 'PDF password protection applied'
            else:  # unlock
                password = form.get('password', '')

                def work():
                    unlock_pdf(input_path, output_path, password)
                    return 'PDF unlocked successfully'

            message = run_in_job(work, tmpdir)
            shutil.move(output_path, os.path.join(output_folder, output_name))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 -- surface as 500
        return jsonify({'error': str(exc)}), 500

    return jsonify({
        'success': True,
        'message': message,
        'filename': output_name,
        'download_url': '/download?filename=' + output_name,
    })

