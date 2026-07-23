"""Print Prep routes: N-up (2/4 pages per sheet) and booklet imposition.

Uploads are handled in an isolated ``tempfile.TemporaryDirectory`` so they
never collide with the merge flow's UPLOAD_FOLDER (which is wiped on every
merge request). Results are written to OUTPUT_FOLDER and served by the shared
/download endpoint (from routes/merge.py).
"""
import os
import tempfile

from flask import Blueprint, current_app, render_template, request, jsonify
from werkzeug.utils import secure_filename

from pdf_ops.imposition import nup_pdf, booklet_pdf
from PyPDF2 import PdfReader

bp = Blueprint('print', __name__)

VALID_OPERATIONS = {'nup', 'booklet'}


@bp.route('/print')
def print_index():
    return render_template('print.html')


@bp.route('/print/run', methods=['POST'])
def run_print():
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
            'error': "Choose an operation: nup or booklet."
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
                return jsonify({'error': 'The PDF has no pages to impose.'}), 400

            if operation == 'nup':
                try:
                    n = int(request.form.get('n', ''))
                except (TypeError, ValueError):
                    raise ValueError("Pages per sheet must be 2 or 4.")
                out_name = f"{base}_{n}up.pdf"
                out_path = os.path.join(output_folder, out_name)
                nup_pdf(input_path, out_path, n=n)
                sheets = (page_count + n - 1) // n
                message = (
                    f"Imposed {page_count} page(s) as {n}-up "
                    f"into {sheets} sheet(s)."
                )

            else:  # booklet
                out_name = f"{base}_booklet.pdf"
                out_path = os.path.join(output_folder, out_name)
                booklet_pdf(input_path, out_path)
                padded = page_count + (-page_count % 4)
                message = (
                    f"Built a {padded}-page booklet ({padded // 2} sheet(s)). "
                    "Print duplex, flip on the short edge, and fold in half."
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
