"""Convert Files routes: any-format-to-any-format file conversion.

Backed by the ``transforms`` registry (transforms/__init__.py), which exposes
the available (source -> target) conversions as a matrix and hands back a
callable for each supported pair. Uploads are processed in an isolated
``tempfile.TemporaryDirectory`` so they never collide with the merge flow's
UPLOAD_FOLDER (which is wiped on every merge request). Results land in
OUTPUT_FOLDER and are served by the shared /download endpoint (routes/merge.py).

A transform may write a file whose name differs from the one requested -- e.g.
a multi-page ``pdf -> png`` conversion yields a single ``.zip`` bundle. The
registry callable returns the path it *actually* wrote; we honour that name (not
the requested one) when moving the result into OUTPUT_FOLDER and reporting back.
"""
import os
import shutil
import tempfile

from flask import Blueprint, current_app, render_template, request, jsonify
from werkzeug.utils import secure_filename

from transforms import (
    TransformError,
    get_transform,
    matrix,
    supported_sources,
    targets_for,
)
from utils.naming import collision_safe
from utils.validation import UploadValidationError, validate_upload

bp = Blueprint('convert', __name__)


def _norm_ext(raw):
    """Normalise a user-supplied extension to lowercase '.xyz' or '' if bad."""
    if not raw:
        return ''
    ext = raw.strip().lower()
    if not ext:
        return ''
    if not ext.startswith('.'):
        ext = '.' + ext
    # A valid extension is a dot followed by alphanumeric characters only.
    body = ext[1:]
    if not body or not body.isalnum():
        return ''
    return ext


@bp.route('/convert')
def convert_index():
    return render_template('convert.html')


@bp.route('/convert/matrix')
def convert_matrix():
    """The full conversion map: {source_ext: [target_ext, ...]}."""
    return jsonify({'matrix': matrix()})


@bp.route('/convert/targets')
def convert_targets():
    """Target extensions reachable from ?ext=.csv."""
    raw = request.args.get('ext')
    if raw is None:
        return jsonify({'error': 'Missing ext parameter.'}), 400
    ext = _norm_ext(raw)
    if not ext:
        return jsonify({'error': 'Malformed ext parameter.'}), 400
    return jsonify({'targets': targets_for(ext)})


@bp.route('/convert/run', methods=['POST'])
def run_convert():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided.'}), 400

    upload = request.files['file']
    if not upload or upload.filename == '':
        return jsonify({'error': 'No file selected.'}), 400

    filename = secure_filename(upload.filename)
    src_ext = os.path.splitext(filename)[1].lower()

    sources = supported_sources()
    if src_ext not in sources:
        shown = ', '.join(sources[:12]) if sources else '(none available yet)'
        return jsonify({
            'error': f"Can't convert {src_ext or 'files without an extension'}. "
                     f"Supported inputs: {shown}."
        }), 400

    target = _norm_ext(request.form.get('target'))
    if not target:
        return jsonify({'error': 'Choose a target format.'}), 400

    allowed = targets_for(src_ext)
    if target not in allowed:
        shown = ', '.join(allowed) if allowed else '(none)'
        return jsonify({
            'error': f"Can't convert {src_ext} to {target}. "
                     f"Available targets: {shown}."
        }), 400

    transform = get_transform(src_ext, target)
    if transform is None:  # pragma: no cover - defensive; targets_for gates this
        return jsonify({'error': f"No converter for {src_ext} -> {target}."}), 400

    stem = os.path.splitext(filename)[0] or 'document'
    requested_name = f"{stem}{target}"
    output_folder = current_app.config['OUTPUT_FOLDER']

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, filename or ('input' + src_ext))
            upload.save(input_path)
            try:
                validate_upload(input_path, src_ext)
            except UploadValidationError as exc:
                return jsonify({'error': str(exc)}), 400

            requested_output = os.path.join(tmpdir, requested_name)
            actual_path = transform(input_path, requested_output)

            actual_name = os.path.basename(actual_path)
            actual_ext = os.path.splitext(actual_name)[1].lower()

            final_name = collision_safe(output_folder, actual_name)
            shutil.move(actual_path, os.path.join(output_folder, final_name))
    except TransformError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({'error': f'Unexpected error: {exc}'}), 500

    if actual_ext != target:
        message = (
            f"Converted {src_ext} to {actual_ext} "
            f"(a single {target} wasn't possible, so you get {final_name})."
        )
    else:
        message = f"Converted {src_ext} to {target}: {final_name}."

    return jsonify({
        'success': True,
        'message': message,
        'filename': final_name,
        'download_url': '/download?filename=' + final_name,
    })
