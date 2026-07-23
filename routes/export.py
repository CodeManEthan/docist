"""Export routes: rasterize a PDF to images (zipped) or extract its text.

Uploads are handled in an isolated ``tempfile.TemporaryDirectory`` so they
never collide with the merge flow's UPLOAD_FOLDER (which is wiped on every
merge request). Results are written to OUTPUT_FOLDER and served by the shared
/download endpoint (from routes/merge.py).
"""
import os
import tempfile
import zipfile

from flask import Blueprint, current_app, render_template, request, jsonify
from werkzeug.utils import secure_filename

from pdf_ops.export import pdf_to_images, pdf_to_text
from PyPDF2 import PdfReader

bp = Blueprint('export', __name__)

VALID_OPERATIONS = {'images', 'text'}


@bp.route('/export')
def export_index():
    return render_template('export.html')


@bp.route('/export/run', methods=['POST'])
def run_export():
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
            'error': "Choose an operation: images or text."
        }), 400

    base = os.path.splitext(filename)[0] or 'document'
    output_folder = current_app.config['OUTPUT_FOLDER']

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, filename)
            upload.save(input_path)

            # Validate it is a readable PDF (and confirm it has pages).
            try:
                page_count = len(PdfReader(input_path).pages)
            except Exception:
                return jsonify({'error': 'Could not read the PDF file.'}), 400
            if page_count < 1:
                return jsonify({'error': 'The PDF has no pages to export.'}), 400

            if operation == 'images':
                fmt = (request.form.get('fmt') or 'png').strip().lower()
                dpi = request.form.get('dpi', '150')
                images_dir = os.path.join(tmpdir, 'images')
                os.makedirs(images_dir, exist_ok=True)
                images = pdf_to_images(input_path, images_dir, fmt=fmt, dpi=dpi)

                out_name = f"{base}_images.zip"
                out_path = os.path.join(output_folder, out_name)
                with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for img in images:
                        zf.write(img, arcname=os.path.basename(img))
                message = f"Exported {len(images)} page(s) as {fmt.upper()} images."

            else:  # text
                out_name = f"{base}.txt"
                out_path = os.path.join(output_folder, out_name)
                pdf_to_text(input_path, out_path)
                message = (
                    f"Extracted text from {page_count} page(s). "
                    "Scanned pages with no text layer come out empty."
                )

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
