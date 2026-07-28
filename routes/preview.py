"""Preview routes: small page thumbnails for a single PDF.

A read-only helper endpoint — nothing is written to OUTPUT_FOLDER and there is
no download URL. The upload lands in an isolated
``tempfile.TemporaryDirectory`` (so it can never collide with the merge flow's
UPLOAD_FOLDER, which is wiped on every merge request), is content-validated,
rendered by :mod:`pdf_ops.preview`, and thrown away before the response is
built. Thumbnails travel back inline as base64 data URLs.
"""
import os
import tempfile

from flask import Blueprint, jsonify, request
from werkzeug.utils import secure_filename

from pdf_ops.preview import MAX_THUMBS, render_thumbnails
from utils.validation import UploadValidationError, validate_upload

bp = Blueprint('preview', __name__)


@bp.route('/preview/thumbs', methods=['POST'])
def thumbs():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided.'}), 400

    upload = request.files['file']
    if not upload or upload.filename == '':
        return jsonify({'error': 'No file selected.'}), 400

    filename = secure_filename(upload.filename) or 'document.pdf'
    ext = os.path.splitext(filename)[1].lower()
    if ext != '.pdf':
        return jsonify({'error': 'Only .pdf files can be previewed.'}), 400

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, filename)
            upload.save(input_path)

            try:
                validate_upload(input_path, '.pdf')
            except UploadValidationError as exc:
                return jsonify({'error': str(exc)}), 400

            try:
                result = render_thumbnails(input_path, max_pages=MAX_THUMBS)
            except Exception:
                # Unreadable, corrupt or password-locked: the client only
                # needs to know the preview is unavailable.
                return jsonify({'error': 'Could not read the PDF file.'}), 400
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({'error': f'Unexpected error: {exc}'}), 500

    return jsonify(result)
