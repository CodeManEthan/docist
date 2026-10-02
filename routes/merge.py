"""Merge & convert routes: the original core of the app.

Uploads land in a per-request ``tempfile.TemporaryDirectory`` (nothing is
kept after the response), matching every other tool page. Only the merged
result is written to OUTPUT_FOLDER, under an unguessable owner-bound name,
and served by the shared /download endpoint below.
"""
import hmac
import os
import tempfile

from flask import Blueprint, current_app, render_template, request, send_file, jsonify
from werkzeug.utils import secure_filename

from converters import get_converter, supported_extensions
from pdf_ops.merge import merge_pipeline, parse_options, OptionsError
from utils.identity import current_user, owner_tag
from utils.naming import display_name, owner_of, result_name
from utils.render_opts import RenderOptionsError, from_form, with_notes
from utils.uploads import charge_uploads, merge_large_ok, upload_ext
from utils.validation import UploadValidationError, validate_upload

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
        render_opts = from_form(request.form, current_user())
    except (OptionsError, RenderOptionsError) as e:
        return jsonify({'error': str(e)}), 400

    # Before anything is saved or converted: the files that can't be large
    # share the free limit across the request (utils/uploads.py).
    try:
        charge_uploads((f, merge_large_ok(upload_ext(f), render_opts))
                       for f in files if f and f.filename)
    except UploadValidationError as e:
        return jsonify({'error': str(e)}), 400

    output_folder = current_app.config['OUTPUT_FOLDER']

    with tempfile.TemporaryDirectory() as tmpdir:
        uploaded_files = []  # list of (pdf_path, bookmark_title)
        first_filename = None
        for file in files:
            if not file or not file.filename:
                continue
            filename = secure_filename(file.filename)
            ext = os.path.splitext(filename)[1].lower()
            if ext != '.pdf' and not get_converter(ext):
                continue

            filepath = os.path.join(tmpdir, filename)
            title = os.path.splitext(filename)[0]  # original name without extension
            file.save(filepath)
            try:
                validate_upload(filepath, ext)
            except UploadValidationError as e:
                return jsonify({'error': f'{filename}: {e}'}), 400

            if ext == '.pdf':
                uploaded_files.append((filepath, title))
            else:
                pdf_path = filepath + '.converted.pdf'
                try:
                    get_converter(ext)(filepath, pdf_path, render_opts)
                except Exception as e:
                    return jsonify({'error': f'Could not convert {filename}: {e}'}), 400
                uploaded_files.append((pdf_path, title))

            if first_filename is None:
                first_filename = filename

        if not uploaded_files:
            return jsonify({'error': 'No supported files provided'}), 400

        # Interleave combines exactly two sources (fronts + backs); reject any
        # other count up front with a clear 400 rather than a downstream 500.
        if options.get('mode') == 'interleave' and len(uploaded_files) != 2:
            return jsonify({
                'error': 'Interleave mode requires exactly 2 files '
                         f'(a fronts file and a backs file); got {len(uploaded_files)}.'
            }), 400

        # Generate output filename based on first file
        base_name = os.path.splitext(first_filename)[0]

        try:
            # Merge, stamp page numbers and add bookmarks in one pass so the
            # outline survives page-number stamping.
            writer = merge_pipeline(uploaded_files, options)

            output_filename = result_name(f"{base_name}-merged.pdf")
            final_output = os.path.join(output_folder, output_filename)
            with open(final_output, 'wb') as output_file:
                writer.write(output_file)

            return jsonify({
                'success': True,
                'message': with_notes(
                    f'Successfully merged {len(uploaded_files)} file(s)', render_opts),
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
    filename = request.args.get('filename', '')
    # Serve only plain names that exist inside OUTPUT_FOLDER — anything
    # secure_filename would alter (path separators, '..', etc.) is rejected,
    # so the query parameter can't reach files outside the folder. The name
    # must also carry the key result_name() gave it, and that key's owner tag
    # must be the asker's, so a result is served only to the browser session
    # or signed-in user that made it. A wrong owner gets the same 404 as a
    # missing file, so the answer never confirms that a name exists.
    friendly = display_name(filename)
    if not filename or filename != secure_filename(filename) or friendly is None:
        return jsonify({'error': 'Invalid filename'}), 400
    if not hmac.compare_digest(owner_of(filename), owner_tag()):
        return jsonify({'error': 'No file available'}), 404
    output_path = os.path.join(current_app.config['OUTPUT_FOLDER'], filename)
    if os.path.exists(output_path):
        return send_file(output_path, as_attachment=True, download_name=friendly)
    return jsonify({'error': 'No file available'}), 404
