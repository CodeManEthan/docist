"""Merge & convert routes: the original core of the app."""
import os

from flask import Blueprint, current_app, render_template, request, send_file, jsonify
from werkzeug.utils import secure_filename

from converters import get_converter, supported_extensions
from pdf_ops.merge import merge_pipeline, parse_options, OptionsError

bp = Blueprint('merge', __name__)


@bp.route('/')
def index():
    return render_template('index.html')


@bp.route('/formats')
def formats():
    """Extensions the app accepts: .pdf plus everything convertible."""
    return jsonify({'extensions': ['.pdf'] + supported_extensions()})


@bp.route('/upload', methods=['POST'])
def upload_files():
    if 'files[]' not in request.files:
        return jsonify({'error': 'No files provided'}), 400

    files = request.files.getlist('files[]')

    if not files or files[0].filename == '':
        return jsonify({'error': 'No files selected'}), 400

    # Validate merge options up front so bad input fails fast with a 400.
    try:
        options = parse_options(request.form)
    except OptionsError as e:
        return jsonify({'error': str(e)}), 400

    upload_folder = current_app.config['UPLOAD_FOLDER']
    output_folder = current_app.config['OUTPUT_FOLDER']

    # Clear previous uploads
    for file in os.listdir(upload_folder):
        os.remove(os.path.join(upload_folder, file))

    uploaded_files = []  # list of (pdf_path, bookmark_title)
    first_filename = None
    for file in files:
        if not file or not file.filename:
            continue
        filename = secure_filename(file.filename)
        ext = os.path.splitext(filename)[1].lower()
        filepath = os.path.join(upload_folder, filename)
        title = os.path.splitext(filename)[0]  # original name without extension

        if ext == '.pdf':
            file.save(filepath)
            uploaded_files.append((filepath, title))
        elif get_converter(ext):
            file.save(filepath)
            pdf_path = filepath + '.converted.pdf'
            try:
                get_converter(ext)(filepath, pdf_path)
            except Exception as e:
                return jsonify({'error': f'Could not convert {filename}: {e}'}), 400
            uploaded_files.append((pdf_path, title))
        else:
            continue

        if first_filename is None:
            first_filename = filename

    if not uploaded_files:
        return jsonify({'error': 'No supported files provided'}), 400

    # Interleave combines exactly two sources (fronts + backs); reject any other
    # count up front with a clear 400 rather than a downstream 500.
    if options.get('mode') == 'interleave' and len(uploaded_files) != 2:
        return jsonify({
            'error': 'Interleave mode requires exactly 2 files '
                     f'(a fronts file and a backs file); got {len(uploaded_files)}.'
        }), 400

    # Generate output filename based on first file
    base_name = os.path.splitext(first_filename)[0]
    output_filename = f"{base_name}-merged.pdf"

    try:
        # Merge, stamp page numbers and add bookmarks in one pass so the
        # outline survives page-number stamping.
        writer = merge_pipeline(uploaded_files, options)

        final_output = os.path.join(output_folder, output_filename)
        with open(final_output, 'wb') as output_file:
            writer.write(output_file)

        return jsonify({
            'success': True,
            'message': f'Successfully merged {len(uploaded_files)} PDFs',
            'download_url': '/download',
            'filename': output_filename
        })

    except OptionsError as e:
        # e.g. interleave page-count mismatch -> a user-fixable 400.
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/download')
def download_file():
    # Get the filename from query parameter, or use default
    filename = request.args.get('filename', 'merged_output.pdf')
    output_path = os.path.join(current_app.config['OUTPUT_FOLDER'], filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True, download_name=filename)
    return jsonify({'error': 'No file available'}), 404
