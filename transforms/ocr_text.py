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
The transform takes ``opts`` (a :class:`converters.options.RenderOptions`);
recognition uses ``opts.ocr_language``, English (``'eng'``) by default. The
language is checked by :func:`pdf_ops.ocr.validate_language` before any frame
is read.

Empty vs. corrupt
-----------------
An image that yields no text (blank/whitespace-only OCR) is **not** an error --
it writes an empty ``.txt``. Only an image that cannot be opened/decoded raises
:class:`transforms.TransformError`. Output is always UTF-8.
"""
import pillow_heif
from PIL import Image, ImageOps, ImageSequence

from pdf_ops import limits

from . import TransformError
# Import ONLY the stable read-only availability helpers. pdf_ops.ocr is being
# extended concurrently; is_available / tesseract_version are the stable API.
from pdf_ops.ocr import is_available, tesseract_version  # noqa: F401
from pdf_ops.ocr import validate_language


# Image source extensions that OCR to text.
IMAGE_FORMATS = ['.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp',
                 '.gif', '.heic', '.heif']

# Teach Pillow to open HEIF/HEIC. Idempotent, safe to call repeatedly.
pillow_heif.register_heif_opener()


def _open(input_path):
    """Open an image, raising TransformError on unreadable/corrupt input.

    The first frame is checked against ``FRAME_PIXELS`` before it is decoded.
    """
    try:
        img = Image.open(input_path)
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
        # Paste white through the inverted alpha band onto the RGB copy:
        # the same result as compositing onto white, without a full-size
        # RGBA copy, RGBA background and composite (Rule D, design §6.2).
        alpha = (frame if frame.mode in ('RGBA', 'LA') else frame.convert('RGBA')).getchannel('A')
        rgb = frame.convert('RGB')
        rgb.paste((255, 255, 255), None, ImageOps.invert(alpha))
        return rgb
    return frame.convert('RGB')


def _ocr_frame(frame, language):
    """Recognise text in a single (already-flattened) frame -> str."""
    import pytesseract  # deferred: only needed once Tesseract is confirmed present
    try:
        return pytesseract.image_to_string(frame, lang=language)
    except Exception as exc:
        raise TransformError(f"OCR failed: {exc}") from exc


def image_to_text(input_path, output_path, opts=None):
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
    try:
        language = validate_language(opts.ocr_language if opts is not None else 'eng')
    except ValueError as exc:
        raise TransformError(str(exc)) from exc

    img = _open(input_path)

    # One frame at a time (Rule D): check it after its seek and before it is
    # decoded, flatten it, OCR it and drop it before the next.
    texts = []
    for frame in ImageSequence.Iterator(img):
        limits.check_frame(frame.width, frame.height)
        flat = _flatten_to_white(frame)
        texts.append(_ocr_frame(flat, language))
        del flat
    if not texts:  # extremely defensive: a decoded image always has >=1 frame
        texts = [_ocr_frame(_flatten_to_white(img), language)]

    if len(texts) == 1:
        body = texts[0]
    else:
        sections = [
            f"--- Frame {i} ---\n{text}"
            for i, text in enumerate(texts, start=1)
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
