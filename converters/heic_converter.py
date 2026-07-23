"""Converter plugin: HEIC / HEIF (.heic, .heif) -> PDF.

Apple's HEIC/HEIF images are not decodable by a stock Pillow build, so this
module registers the ``pillow_heif`` opener (module-level and idempotent) which
teaches Pillow how to open them. Once registered, HEIC files behave like any
other raster image, so we delegate straight to
:func:`converters.image_converter.convert` and reuse its entire pipeline --
letter-page composition, aspect-preserving scale-down, transparency flattening
and centring -- rather than duplicating any of that geometry here.
"""
import pillow_heif

from . import ConversionError
from .image_converter import convert as _image_convert


EXTENSIONS = ['.heic', '.heif']

# Teach Pillow to open HEIC/HEIF. register_heif_opener() is safe to call more
# than once, so doing it at import time keeps things simple and idempotent.
pillow_heif.register_heif_opener()


def convert(input_path, output_path):
    """Convert the HEIC/HEIF image at ``input_path`` to a PDF at ``output_path``.

    Delegates to the shared raster image pipeline. Corrupt / non-HEIC input
    surfaces as a :class:`ConversionError` (the image pipeline already raises
    ConversionError when Pillow cannot open or process the file).
    """
    _image_convert(input_path, output_path)
