"""OCR image-to-text transform plugin (raster image -> ``.txt``).

This module registers a direct ``(image_ext, '.txt')`` transform for every
raster image extension the app understands, using Tesseract (via
``pytesseract``) to recognise text baked into pixels. It plugs into the
transform registry (see :mod:`transforms`) through the module-level
``TRANSFORMS`` dict.

Registered pairs
----------------
Only ``(<image_ext>, '.txt')`` pairs are registered -- one per extension in
:data:`IMAGE_FORMATS`. This module is **deliberately scoped to image sources
only**. It does *not* register ``('.pdf', '.txt')`` (or any other pair the
:mod:`transforms.pdf_bridge` module owns).

Naming / precedence note
------------------------
The registry loads plugin modules in **sorted module-name order** and the
*first* registration of a given pair wins (``dict.setdefault`` in
``transforms/__init__``). This module is named ``ocr_text`` so that it sorts
*before* ``pdf_bridge`` (``images < ocr_text < pdf_bridge``). If it were to
register a pair the bridge also owns -- e.g. ``('.pdf', '.txt')`` -- this
module's version would silently shadow the bridge's. To guarantee that never
happens we register image sources exclusively; the bridge keeps sole ownership
of ``.pdf -> .txt`` (embedded-text extraction, no OCR). The test suite asserts
the pair sets are disjoint.

Availability gating (call time, not import time)
------------------------------------------------
OCR needs the Tesseract system binary, which is not a pip dependency. This
module still **imports cleanly and registers its pairs on machines without
Tesseract** -- gating happens when a conversion is *attempted*, not at import,
so the conversion matrix advertises the same shape everywhere and a missing
binary surfaces as a clear :class:`transforms.TransformError` at call time
rather than making the whole plugin (and every image transform beside it)
fail to load. The gate is :func:`pdf_ops.ocr.is_available`.

Alpha / transparency policy
---------------------------
Transparent backgrounds confuse Tesseract (undefined RGB under transparent
pixels reads as noise), so every frame is **flattened onto a solid white
background** before recognition (mirrors the RGB-flatten policy in
:mod:`transforms.images`). Opaque images are simply converted to RGB.

Multi-frame policy
------------------
Animated / multi-page rasters (animated GIF, multi-page TIFF, ...) are OCR'd
**frame by frame**. When there is more than one frame the per-frame results are
joined with a form-feed followed by a ``--- Frame N ---`` header, mirroring the
page-separator style of :func:`pdf_ops.export.pdf_to_text`. A single-frame image
produces plain text with no header.

Language
--------
The transform signature is ``func(input_path, output_path)`` -- there is no
channel to route a language code through it, so recognition always uses English
(``'eng'``). Non-English documents are a known limitation of this entry point.

Empty vs. corrupt
-----------------
An image that yields no text (blank/whitespace-only OCR) is **not** an error --
it writes an empty ``.txt``. Only an image that cannot be opened/decoded raises
:class:`transforms.TransformError`. Output is always UTF-8.
"""
import pillow_heif
from PIL import Image, ImageSequence

from . import TransformError
# Import ONLY the stable read-only availability helpers. pdf_ops.ocr is being
# extended concurrently; is_available / tesseract_version are the stable API.
from pdf_ops.ocr import is_available, tesseract_version  # noqa: F401


# Image source extensions that OCR to text.
IMAGE_FORMATS = ['.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp',
                 '.gif', '.heic', '.heif']

# Tesseract recognises this language (see module docstring -- not routable).
OCR_LANGUAGE = 'eng'

# Teach Pillow to open HEIF/HEIC. Idempotent, safe to call repeatedly.
pillow_heif.register_heif_opener()


def _open(input_path):
    """Open an image, raising TransformError on unreadable/corrupt input."""
    try:
        img = Image.open(input_path)
        img.load()  # force a real decode so truncated/corrupt data fails here
    except Exception as exc:
        raise TransformError(
            f"Could not open image '{input_path}': {exc}"
        ) from exc
    return img


def _flatten_to_white(frame):
    """RGB copy of a frame with any transparency composited onto white.

    Tesseract works on the pixel values; transparent regions carry undefined
    RGB, so we composite onto an opaque white background before recognition.
    """
    has_alpha = (
        frame.mode in ('RGBA', 'LA')
        or (frame.mode == 'P' and 'transparency' in frame.info)
    )
    if has_alpha:
        rgba = frame.convert('RGBA')
        background = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert('RGB')
    return frame.convert('RGB')


def _ocr_frame(frame):
    """Recognise text in a single (already-flattened) frame -> str."""
    import pytesseract  # deferred: only needed once Tesseract is confirmed present
    try:
        return pytesseract.image_to_string(frame, lang=OCR_LANGUAGE)
    except Exception as exc:
        raise TransformError(f"OCR failed: {exc}") from exc


def image_to_text(input_path, output_path):
    """Recognise text in a raster image and write it as a UTF-8 ``.txt``.

    Multi-frame images are OCR'd per frame and joined with form-feed +
    ``--- Frame N ---`` headers; a single frame yields plain text. A blank
    image writes an empty file (not an error). Returns ``output_path``.
    """
    # Gate at CALL time -- the module imports/registers fine without Tesseract.
    if not is_available():
        raise TransformError(
            "OCR requires Tesseract (and Ghostscript). Install the tesseract "
            "and ghostscript system packages to enable image -> text."
        )

    img = _open(input_path)

    frames = [_flatten_to_white(f) for f in ImageSequence.Iterator(img)]
    if not frames:  # extremely defensive: a decoded image always has >=1 frame
        frames = [_flatten_to_white(img)]

    if len(frames) == 1:
        body = _ocr_frame(frames[0])
    else:
        sections = [
            f"--- Frame {i} ---\n{_ocr_frame(frame)}"
            for i, frame in enumerate(frames, start=1)
        ]
        # Form-feed between frames so downstream readers see a real break.
        body = "\f".join(sections)

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(body)

    return output_path


# --------------------------------------------------------------------------
# Register (image_ext, '.txt') for every supported raster source.
# --------------------------------------------------------------------------
TRANSFORMS = {(_ext, '.txt'): image_to_text for _ext in IMAGE_FORMATS}
