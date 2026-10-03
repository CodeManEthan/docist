"""Raster image-to-image transform plugin (full N x N matrix).

This module registers a direct transform for every ordered pair of raster
formats whose *underlying codec* differs.  It plugs into the transform
registry (see :mod:`transforms`) via the module-level ``TRANSFORMS`` dict.

Codec families
--------------
Several extensions are just aliases for the same codec, so they are treated
as one:

    PNG   -> .png
    JPEG  -> .jpg, .jpeg
    WEBP  -> .webp
    BMP   -> .bmp
    TIFF  -> .tiff, .tif
    GIF   -> .gif
    HEIF  -> .heic, .heif

Pairs whose source and destination share a codec (e.g. ``.jpg -> .jpeg`` or
``.tif -> .tiff``) are **skipped** -- they would be a byte-for-byte re-encode
of an identical format, which the registry treats as a no-op anyway
(``get_transform`` returns ``None`` when ``src == dst``). Every *cross-codec*
ordered pair is generated, including all alias source variants, so both
``.tif -> .png`` and ``.tiff -> .png`` (and ``.heic -> .jpg`` and
``.heif -> .jpg``) are registered.

Mode / channel policy per target codec
---------------------------------------
* JPEG, BMP    -- flattened to RGB; any transparency is composited onto a
                  white background (mirrors ``converters.image_converter``).
* PNG, WEBP,
  TIFF, HEIF   -- alpha is preserved (RGBA kept; opaque images stay RGB/L).
* GIF          -- flattened to RGB (alpha onto white) then quantised with an
                  adaptive 256-colour palette.

JPEG is written at quality 90.

Animation / multi-frame policy
------------------------------
Only the **first frame** of a multi-frame / animated source (animated GIF,
multi-page TIFF, ...) is converted. Animation is intentionally *not*
preserved on the output side -- every target here is written as a single
still image. This keeps the whole matrix predictable.

SVG is deliberately NOT handled here: vector -> raster is covered by the
PDF pivot (svg -> pdf via the existing converter, then pdf -> image).

HEIC/HEIF support relies on ``pillow-heif``.  ``register_heif_opener()`` is
called at import time (idempotent) and, with current pillow-heif, enables
both *reading* and *writing* of HEIF via Pillow's ``save(..., "HEIF")``.
"""
import re

import pillow_heif
from PIL import Image, ImageOps

from pdf_ops import limits

from . import TransformError


# All extensions this plugin knows about.
FORMATS = ['.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff', '.tif', '.gif',
           '.heic', '.heif']

# Extension -> Pillow format name (also the codec-identity key: two extensions
# with the same value are the same underlying codec).
_CODEC = {
    '.png': 'PNG',
    '.jpg': 'JPEG',
    '.jpeg': 'JPEG',
    '.webp': 'WEBP',
    '.bmp': 'BMP',
    '.tiff': 'TIFF',
    '.tif': 'TIFF',
    '.gif': 'GIF',
    '.heic': 'HEIF',
    '.heif': 'HEIF',
}

# Targets that cannot carry an alpha channel -> flatten onto white.
_NEEDS_RGB = {'JPEG', 'BMP'}

# Teach Pillow to open *and* save HEIF/HEIC. Safe to call repeatedly.
pillow_heif.register_heif_opener()

JPEG_QUALITY = 90

# Pillow's text for libwebp's VP8_ENC_ERROR_OUT_OF_MEMORY.
_WEBP_OUT_OF_MEMORY = re.compile(r'\bencoding error 1\b')


def _open(input_path):
    """Open + decode the first frame, raising TransformError on bad input."""
    try:
        img = Image.open(input_path)
        # Its one frame is checked before it is decoded (design §6.1).
        limits.check_frame(img.width, img.height)
        img.load()  # force a real decode so truncated/corrupt data fails here
    except limits.LimitError:
        raise
    except Image.DecompressionBombError as exc:
        # Over Pillow's own limit (about 179 MP), before check_frame runs:
        # the same ruled sentence as any image over FRAME_PIXELS.
        raise limits.LimitError(limits.image_message(), kind='frame') from exc
    except Exception as exc:
        raise TransformError(
            f"Could not open image '{input_path}': {exc}"
        ) from exc
    return img


def _has_alpha(img):
    return (
        img.mode in ('RGBA', 'LA')
        or (img.mode == 'P' and 'transparency' in img.info)
    )


def _flatten_to_rgb(img):
    """RGB copy with any transparency composited onto a white background.

    White is pasted through the inverted alpha band onto the RGB copy, the
    same result as compositing onto white without a full-size RGBA copy, RGBA
    background and composite (Rule D; as ``ocr_text._flatten_to_white``).
    """
    if _has_alpha(img):
        source = img if img.mode in ('RGBA', 'LA') else img.convert('RGBA')
        alpha = source.getchannel('A')
        rgb = img.convert('RGB')
        rgb.paste((255, 255, 255), None, ImageOps.invert(alpha))
        return rgb
    return img.convert('RGB')


def _prep_keep_alpha(img):
    """Normalise a frame for a target codec that supports alpha (an RGBA
    frame is used as it is, not copied)."""
    if _has_alpha(img):
        return img if img.mode == 'RGBA' else img.convert('RGBA')
    if img.mode in ('RGB', 'L'):
        return img
    return img.convert('RGB')


def _convert(input_path, output_path, target_fmt):
    """Core conversion: read ``input_path``, write ``output_path`` as ``target_fmt``.

    ``target_fmt`` is a Pillow format name ('PNG', 'JPEG', 'WEBP', 'BMP',
    'TIFF', 'GIF' or 'HEIF').
    """
    img = _open(input_path)

    if target_fmt in _NEEDS_RGB:
        out = _flatten_to_rgb(img)
    elif target_fmt == 'GIF':
        out = _flatten_to_rgb(img).convert(
            'P', palette=Image.ADAPTIVE, colors=256
        )
    else:  # PNG, WEBP, TIFF, HEIF -- alpha-capable
        out = _prep_keep_alpha(img)

    save_kwargs = {}
    if target_fmt == 'JPEG':
        save_kwargs['quality'] = JPEG_QUALITY

    try:
        out.save(output_path, target_fmt, **save_kwargs)
    except (OSError, ValueError) as exc:
        if target_fmt == 'WEBP' and _WEBP_OUT_OF_MEMORY.search(str(exc)):
            # libwebp's VP8_ENC_ERROR_OUT_OF_MEMORY (1): the encoder ran out of
            # the job's memory, but Pillow 12.3 reports it as a plain
            # ValueError ("encoding error 1"; OSError too, to be safe). Give
            # the ruled memory refusal, not Pillow's text and a server path.
            raise limits.LimitError(limits.memory_message(), kind='memory') from exc
        raise TransformError(
            f"Could not write '{output_path}' as {target_fmt}: {exc}"
        ) from exc
    except Exception as exc:
        raise TransformError(
            f"Could not write '{output_path}' as {target_fmt}: {exc}"
        ) from exc


def _make_transform(target_fmt):
    """Build a registry-shaped func(input_path, output_path) for a target."""
    def _transform(input_path, output_path):
        _convert(input_path, output_path, target_fmt)
    _transform.__name__ = f'convert_to_{target_fmt.lower()}'
    _transform.__qualname__ = _transform.__name__
    return _transform


# --------------------------------------------------------------------------
# Generate the full cross-codec matrix programmatically.
# --------------------------------------------------------------------------
TRANSFORMS = {}
for _src in FORMATS:
    for _dst in FORMATS:
        if _CODEC[_src] == _CODEC[_dst]:
            continue  # same codec (identity or alias pair) -> skip
        TRANSFORMS[(_src, _dst)] = _make_transform(_CODEC[_dst])
