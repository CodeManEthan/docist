"""Export routes: rasterize a PDF to images (zipped) or extract its text.

Uploads are handled in an isolated ``tempfile.TemporaryDirectory`` so they
never collide with the merge flow's UPLOAD_FOLDER (which is wiped on every
merge request). Results are written to OUTPUT_FOLDER and served by the shared
/download endpoint (from routes/merge.py).
"""
import os
import shutil
import tempfile
import zipfile

from flask import Blueprint, current_app, render_template, request, jsonify
from werkzeug.utils import secure_filename

from pdf_ops.export import pdf_to_images, pdf_to_text, pdf_to_text_report
from pdf_ops.ocr import installed_languages
from pdf_ops.ocr import is_available as ocr_is_available
from pdf_ops.ocr_langs import language_choices
from utils.render_opts import from_form, run_in_job
from utils.naming import result_name
from pypdf import PdfReader

bp = Blueprint('export', __name__)

VALID_OPERATIONS = {'images', 'text'}

_OCR_INSTALL_HINT = (
    "OCR needs the Tesseract and Ghostscript system packages, "
    "which are not installed on this server."
)


def _truthy(value):
    """Interpret a bool-ish form value (checkbox / flag) as a boolean."""
    return str(value).strip().lower() in {'1', 'true', 'on', 'yes'}


@bp.route('/export')
def export_index():
    return render_template('export.html', ocr_available=ocr_is_available(),
                           ocr_languages=language_choices(installed_languages()))


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
        # The form is read here; the PDF only in the request's job.
        if operation == 'images':
            fmt = (request.form.get('fmt') or 'png').strip().lower()
            dpi = request.form.get('dpi', '150')
            out_ext = '.zip'
        else:  # text
            ocr_fallback = _truthy(request.form.get('ocr_fallback'))
            if ocr_fallback and not ocr_is_available():
                return jsonify({'error': _OCR_INSTALL_HINT}), 400
            # Only the OCR fallback reads `language`; a bad one is a
            # RenderOptionsError, a ValueError, so a 400 below.
            language = 'eng'
            if ocr_fallback:
                language = from_form(request.form, paper=False, ocr=True).ocr_language
            out_ext = '.txt'

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, filename)
            upload.save(input_path)
            # Built here, outside the job directory; moved to OUTPUT_FOLDER
            # only after the job returns (design launch-hardening §10.3).
            out_path = os.path.join(tmpdir, 'result' + out_ext)

            def work():
                # Validate it is a readable PDF (and confirm it has pages).
                try:
                    page_count = len(PdfReader(input_path).pages)
                except MemoryError:
                    raise
                except Exception:
                    raise ValueError('Could not read the PDF file.')
                if page_count < 1:
                    raise ValueError('The PDF has no pages to export.')

                if operation == 'images':
                    images_dir = tempfile.mkdtemp(prefix='images-')   # the job's
                    images = pdf_to_images(input_path, images_dir, fmt=fmt, dpi=dpi)
                    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                        for img in images:
                            zf.write(img, arcname=os.path.basename(img))
                    return f"Exported {len(images)} page(s) as {fmt.upper()} images."

                report = pdf_to_text_report(
                    input_path, out_path,
                    ocr_fallback=ocr_fallback, language=language,
                )
                n_ocr = len(report['ocr_pages'])
                if ocr_fallback:
                    if n_ocr:
                        return (
                            f"Extracted text from {report['pages']} page(s) "
                            f"({n_ocr} via OCR)."
                        )
                    return (
                        f"Extracted text from {report['pages']} page(s). "
                        "No pages needed OCR."
                    )
                return (
                    f"Extracted text from {report['pages']} page(s). "
                    "Scanned pages with no text layer come out empty."
                )

            message = run_in_job(work, tmpdir)
            if operation == 'images':
                out_name = result_name(f"{base}_images.zip")
            else:
                out_name = result_name(f"{base}.txt")
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
