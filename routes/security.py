"""Watermark & security routes: stamp text or protect/unlock a single PDF.

Uploads are processed inside a ``tempfile.TemporaryDirectory`` -- deliberately
NOT the shared UPLOAD_FOLDER, which the merge flow clears on every request.
Results land in OUTPUT_FOLDER and are served by the shared /download endpoint.
"""
import os
import tempfile
import uuid

from flask import Blueprint, current_app, render_template, request, jsonify
from werkzeug.utils import secure_filename

from pdf_ops.security import protect_pdf, unlock_pdf
from pdf_ops.watermark import apply_text_watermark
from pdf_ops.stamp import apply_header_footer, apply_bates_numbers, format_bates
from utils.validation import UploadValidationError, validate_upload

bp = Blueprint('security', __name__)

_OPERATIONS = ('watermark', 'protect', 'unlock', 'headerfooter', 'bates')


@bp.route('/security')
def security():
    return render_template('security.html')


def _output_name(operation, original):
    """A distinct OUTPUT_FOLDER filename for a processed document."""
    base = os.path.splitext(secure_filename(original) or 'document.pdf')[0] or 'document'
    suffix = {
        'watermark': 'watermarked',
        'protect': 'protected',
        'unlock': 'unlocked',
        'headerfooter': 'stamped',
        'bates': 'bates',
    }[operation]
    return f'{base}-{suffix}-{uuid.uuid4().hex[:8]}.pdf'


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
    output_path = os.path.join(output_folder, output_name)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, filename)
            file.save(input_path)
            try:
                validate_upload(input_path, '.pdf')
            except UploadValidationError as exc:
                return jsonify({'error': str(exc)}), 400

            if operation == 'watermark':
                text = request.form.get('text', '')
                if not text or not text.strip():
                    return jsonify({'error': 'Watermark text is required'}), 400
                position = request.form.get('position', 'center')
                opacity = request.form.get('opacity', 0.15)
                font_size = request.form.get('font_size', 48)
                rotation = request.form.get('rotation', 45)
                apply_text_watermark(
                    input_path, output_path, text,
                    position=position, opacity=opacity,
                    font_size=font_size, rotation=rotation,
                )
                message = 'Watermark applied to every page'
            elif operation == 'headerfooter':
                slots = {
                    'header_left': request.form.get('header_left', ''),
                    'header_center': request.form.get('header_center', ''),
                    'header_right': request.form.get('header_right', ''),
                    'footer_left': request.form.get('footer_left', ''),
                    'footer_center': request.form.get('footer_center', ''),
                    'footer_right': request.form.get('footer_right', ''),
                }
                font_size = request.form.get('font_size', 9)
                color = request.form.get('color', '#444444')
                margin = request.form.get('margin', 36)
                apply_header_footer(
                    input_path, output_path,
                    font_size=font_size, color=color, margin=margin,
                    **slots,
                )
                message = 'Header/footer applied to every page'
            elif operation == 'bates':
                prefix = request.form.get('prefix', '')
                start = request.form.get('start', 1)
                digits = request.form.get('digits', 6)
                position = request.form.get('position', 'bottom-right')
                font_size = request.form.get('font_size', 9)
                color = request.form.get('color', '#000000')
                last_label = apply_bates_numbers(
                    input_path, output_path,
                    prefix=prefix, start=start, digits=digits,
                    position=position, font_size=font_size, color=color,
                )
                first_label = _bates_first_label(prefix, start, digits)
                message = f'Stamped {first_label}–{last_label}'
            elif operation == 'protect':
                # Password is used only to encrypt the user's own file; never logged.
                password = request.form.get('password', '')
                protect_pdf(input_path, output_path, password)
                message = 'PDF password protection applied'
            else:  # unlock
                password = request.form.get('password', '')
                unlock_pdf(input_path, output_path, password)
                message = 'PDF unlocked successfully'
    except ValueError as exc:
        _cleanup(output_path)
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 -- surface as 500
        _cleanup(output_path)
        return jsonify({'error': str(exc)}), 500

    return jsonify({
        'success': True,
        'message': message,
        'filename': output_name,
        'download_url': '/download?filename=' + output_name,
    })


def _cleanup(path):
    """Remove a partially-written output file, ignoring absence."""
    try:
        os.remove(path)
    except OSError:
        pass
