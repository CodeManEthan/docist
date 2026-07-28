"""Collision-safe output filenames, shared by every route that writes results.

Previously each route wrote ``<base>_<suffix>.<ext>`` straight into
OUTPUT_FOLDER, silently clobbering any earlier result with the same name
(only /convert protected itself). Every route now calls
:func:`collision_safe` before writing.
"""
import os


def collision_safe(output_folder, name):
    """Return a filename in output_folder that does not clobber an existing file.

    'report.zip' -> 'report.zip', then 'report_1.zip', 'report_2.zip', ...
    """
    base, ext = os.path.splitext(name)
    candidate = name
    counter = 1
    while os.path.exists(os.path.join(output_folder, candidate)):
        candidate = f"{base}_{counter}{ext}"
        counter += 1
    return candidate
