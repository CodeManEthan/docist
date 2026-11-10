"""PDF compression: lossless content-stream packing + best-effort image recompression.

Pure PDF-processing (no Flask dependency), built on pypdf and Pillow.

Two passes are applied by :func:`compress_pdf`:

1. **Lossless pass** — every page is cloned into a fresh ``PdfWriter`` and
   :meth:`PageObject.compress_content_streams` is called on it. This
   Flate-compresses page content streams that were stored uncompressed and
   drops redundancy without changing a single rendered pixel.
   Document metadata is carried over onto the writer.

2. **Image pass (best-effort)** — each page's ``/Resources /XObject`` image
   objects are walked. Two kinds are handled: JPEG images and 8-bit
   DeviceRGB / DeviceGray raw rasters (``/FlateDecode`` and the like). The
   pixels are decoded with Pillow, optionally *downsampled* so the effective
   on-page resolution does not exceed ``image_max_dpi`` (the display size is
   recovered by tracking the CTM through the page content stream), and
   re-encoded as JPEG at ``image_quality``. The XObject stream is then
   rewritten in place to a single ``/DCTDecode`` filter.

   A JPEG image is one whose filter list ends in ``/DCTDecode``. Its stored
   bytes are run back through every filter before that last one (a PDF
   writer such as reportlab wraps each JPEG in ``/ASCII85Decode`` text), and
   what comes out is opened as the JPEG. When there is such a wrapper, the
   declared ``/Width`` x ``/Height`` is checked against the frame limit before
   any byte is decoded. If the filters fail to decode, or what they give
   doesn't open, the stored bytes themselves are tried as the JPEG. A list
   that has ``/DCTDecode`` anywhere but last is not a JPEG we can read and
   is skipped.

   On replacement, an image whose colour space is DeviceGray, DeviceRGB,
   CalGray, CalRGB or a 1- or 3-component ICCBased profile, and whose new
   JPEG has that many components, keeps its colour space and any ``/Decode``
   array. Any other image gets DeviceRGB or DeviceGray to match the new JPEG,
   and a CMYK JPEG, which is converted to RGB, also loses its ``/Decode``
   array, since that array describes four components.

   This pass is deliberately conservative: a raw raster that is not plainly
   RGB/gray 8-bit, any image that carries a soft mask / alpha (``/SMask``),
   or one that fails to decode for any reason is left **untouched** — one
   stubborn image never fails the whole run. Only images that come out
   *smaller* than their stored bytes after re-encoding are actually replaced.

**Why the XObject streams are mutated directly.** Replacement is done on the
underlying XObject ``StreamObject`` (``obj._data`` plus the ``/Filter`` /
``/Width`` / ``/Height`` / ``/ColorSpace`` / ``/BitsPerComponent`` dictionary
entries) rather than through pypdf's ``page.images[...].replace()`` helper.
``replace()`` re-encodes by round-tripping the image through a whole throwaway
one-page PDF (``PIL.Image.save(..., "PDF")``) and swapping the resulting
object in, which gives no way to *first* check that the new encoding is
actually smaller — the core rule of this pass — and it eagerly decodes every
image on the page, including the exotic ones we deliberately skip. The direct
mutation is verified to produce valid, readable PDFs for the RGB/gray raster
and JPEG cases handled here; exotic image encodings are intentionally out of
scope and skipped.

If the whole optimisation ends up producing a file *larger* than the input
(possible on already-optimised PDFs), the original bytes are kept verbatim and
a ratio of ``1.0`` is reported.
"""
import io
import math
import re
import shutil

from pypdf import PdfReader, PdfWriter
from pypdf.filters import decode_stream_data
from pypdf.generic import ArrayObject, NameObject, NumberObject

from pdf_ops import limits

try:  # Pillow is required for the image pass but never for the lossless pass.
    from PIL import Image
    _HAVE_PIL = True
except Exception:  # pragma: no cover - Pillow is a project dependency
    _HAVE_PIL = False


_NUM_RE = re.compile(r"-?\d*\.?\d+")
# Tokenise a content stream into the pieces we care about: names, numbers and
# the q / Q / cm / Do operators. Everything else is ignored.
_TOKEN_RE = re.compile(r"/[A-Za-z0-9._#+-]+|-?\d*\.?\d+|q|Q|cm|Do")


def _mat_mul(m, n):
    """Multiply two PDF affine matrices (a b c d e f), row-vector convention."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _content_display_sizes(page):
    """Map XObject name -> (display_width_pt, display_height_pt) on the page.

    Recovered by walking the page content stream, tracking the current
    transformation matrix across ``q``/``Q``/``cm`` and recording, for every
    ``/Name Do`` invocation, the size of the transformed unit square. When an
    image is painted more than once the largest placement wins (so we never
    downsample below the sharpest use). Returns ``{}`` if the content cannot be
    read — callers treat that as "display size unknown".
    """
    try:
        contents = page.get_contents()
        data = contents.get_data() if contents is not None else b""
    except Exception:
        return {}
    if not data:
        return {}
    text = data.decode("latin-1", "replace")

    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    stack = []
    nums = []
    last_name = None
    sizes = {}
    for tok in _TOKEN_RE.findall(text):
        if _NUM_RE.fullmatch(tok):
            nums.append(float(tok))
        elif tok.startswith("/"):
            last_name = tok[1:]
            nums = []
        elif tok == "q":
            stack.append(ctm)
            nums = []
        elif tok == "Q":
            if stack:
                ctm = stack.pop()
            nums = []
        elif tok == "cm":
            if len(nums) >= 6:
                ctm = _mat_mul(tuple(nums[-6:]), ctm)
            nums = []
        elif tok == "Do":
            if last_name is not None:
                dw = math.hypot(ctm[0], ctm[1])
                dh = math.hypot(ctm[2], ctm[3])
                prev = sizes.get(last_name, (0.0, 0.0))
                sizes[last_name] = (max(prev[0], dw), max(prev[1], dh))
            nums = []
        else:
            nums = []
    return sizes


def _filter_names(obj):
    """The stream's ``/Filter`` as a list of names (``[]`` when there is none)."""
    filt = obj.get("/Filter")
    if filt is None:
        return []
    filt = filt.get_object()
    if isinstance(filt, ArrayObject):
        return [str(f.get_object()) for f in filt]
    return [str(filt)]


def _jpeg_candidates(obj, filters):
    """The byte strings to try, in order, as the JPEG of an image whose
    filter list ends in ``/DCTDecode``.

    A bare ``/DCTDecode`` (or a one-element list) stores the JPEG as is. A
    longer list is decoded through the filters before ``/DCTDecode``, after
    the declared ``/Width`` x ``/Height`` passes ``limits.frame_ok``, so an
    oversized image is never decoded. The stored bytes follow as a second
    try, for a chain pypdf can't decode (a ``/Crypt`` step it doesn't run)
    or one a writer mislabelled (raw JPEG bytes declared as Flate, which
    pypdf decodes to nothing). Returns ``[]`` when the image is to be left
    alone: declared size missing, not a number or over the limit.
    """
    if len(filters) == 1:
        return [obj._data]
    try:
        w = int(obj["/Width"])
        h = int(obj["/Height"])
    except Exception:
        return []
    if not limits.frame_ok(w, h):
        return []
    try:
        # Unlike get_data(), this doesn't cache the decoded copy on the
        # object. Its /DCTDecode step hands the JPEG bytes back unchanged.
        return [decode_stream_data(obj), obj._data]
    except Exception:
        return [obj._data]


def _pil_from_xobject(obj):
    """Decode an image XObject into a Pillow image, or ``None`` if unsupported.

    Only the plainly re-encodable cases are handled: 8-bit DeviceRGB /
    DeviceGray rasters (``/FlateDecode`` etc.) and JPEG images, whose filter
    list ends in ``/DCTDecode`` (see :func:`_jpeg_candidates`). Anything with an
    alpha soft mask, a raster in another colour space or depth, or a filter
    list with ``/DCTDecode`` anywhere but last returns ``None`` so the caller
    skips it.
    """
    try:
        filters = _filter_names(obj)
    except Exception:
        return None
    color = obj.get("/ColorSpace")
    bpc = obj.get("/BitsPerComponent")

    # Alpha / soft-mask images would lose transparency through JPEG; skip them.
    if "/SMask" in obj or "/Mask" in obj:
        return None

    if "/DCTDecode" in filters:
        if filters[-1] != "/DCTDecode":
            return None   # the JPEG is wrapped in a later filter: left alone
        for data in _jpeg_candidates(obj, filters):
            try:
                im = Image.open(io.BytesIO(data))
            except Exception:
                continue   # not an image: try the next candidate
            if not limits.frame_ok(im.width, im.height):
                return None   # over FRAME_PIXELS: left alone, like one it can't open
            try:
                im.load()
                return im
            except Exception:
                continue
        return None

    # Raw raster: only handle 8-bit gray / RGB.
    if bpc is not None and int(bpc) != 8:
        return None
    if color == "/DeviceRGB":
        mode = "RGB"
    elif color == "/DeviceGray":
        mode = "L"
    else:
        return None
    try:
        w = int(obj["/Width"])
        h = int(obj["/Height"])
        # The declared size, before any byte is decoded: an
        # image over FRAME_PIXELS is left alone, like one it can't open.
        if not limits.frame_ok(w, h):
            return None
        raw = obj.get_data()  # applies the stream filters -> raw pixels
        return Image.frombytes(mode, (w, h), raw)
    except Exception:
        return None


_DEVICE_SPACES = {"/DeviceGray": 1, "/DeviceRGB": 3}
_CAL_SPACES = {"/CalGray": 1, "/CalRGB": 3}
_MODE_COMPONENTS = {"L": 1, "RGB": 3}


def _space_components(color):
    """Component count of a colour space whose samples a re-encoded JPEG with
    that many components still matches: 1 or 3 for DeviceGray / DeviceRGB,
    CalGray / CalRGB, or an ICCBased stream with ``/N`` 1 or 3. ``None`` for
    every other space, or when there is none."""
    if color is None:
        return None
    try:
        color = color.get_object()
        if isinstance(color, ArrayObject):
            if not color:
                return None
            family = str(color[0].get_object())
            if family == "/ICCBased" and len(color) > 1:
                n = color[1].get_object().get("/N")
                n = int(n) if n is not None else None
                return n if n in (1, 3) else None
            return _CAL_SPACES.get(family)
        return _DEVICE_SPACES.get(str(color))
    except Exception:
        return None


def _recompress_page_images(page, display_sizes, image_quality, image_max_dpi):
    """Re-encode eligible images on one writer page. Returns the count replaced."""
    resources = page.get("/Resources")
    if not resources:
        return 0
    resources = resources.get_object()
    xobjects = resources.get("/XObject")
    if not xobjects:
        return 0
    xobjects = xobjects.get_object()

    replaced = 0
    for name in list(xobjects.keys()):
        try:
            obj = xobjects[name].get_object()
        except Exception:
            continue
        if obj.get("/Subtype") != "/Image":
            continue

        pil = _pil_from_xobject(obj)
        if pil is None:
            continue

        source_bands = len(pil.getbands())
        if pil.mode not in ("RGB", "L"):
            pil = pil.convert("RGB")

        # Downsample so effective DPI <= image_max_dpi, if we know the display
        # size of this XObject (its dictionary key, without the leading '/').
        disp = display_sizes.get(str(name)[1:]) if display_sizes else None
        if disp and disp[0] > 0 and disp[1] > 0:
            eff_w = pil.width / (disp[0] / 72.0)
            eff_h = pil.height / (disp[1] / 72.0)
            scale = min(
                1.0,
                image_max_dpi / eff_w if eff_w > 0 else 1.0,
                image_max_dpi / eff_h if eff_h > 0 else 1.0,
            )
            if scale < 1.0:
                new_w = max(1, int(round(pil.width * scale)))
                new_h = max(1, int(round(pil.height * scale)))
                try:
                    pil = pil.resize((new_w, new_h), Image.LANCZOS)
                except Exception:
                    pass

        try:
            buf = io.BytesIO()
            pil.save(buf, format="JPEG", quality=int(image_quality), optimize=True)
            jpeg = buf.getvalue()
        except Exception:
            continue

        # Only replace when it actually saves space for this image.
        try:
            original_len = len(obj._data)
        except Exception:
            original_len = None
        if original_len is not None and len(jpeg) >= original_len:
            continue

        try:
            obj._data = jpeg
            obj[NameObject("/Filter")] = NameObject("/DCTDecode")
            obj[NameObject("/Width")] = NumberObject(pil.width)
            obj[NameObject("/Height")] = NumberObject(pil.height)
            # A space the new samples still match keeps its object (an
            # ICCBased profile, a CalRGB calibration) and any /Decode array.
            if _space_components(obj.get("/ColorSpace")) != _MODE_COMPONENTS[pil.mode]:
                obj[NameObject("/ColorSpace")] = NameObject(
                    "/DeviceRGB" if pil.mode == "RGB" else "/DeviceGray"
                )
            obj[NameObject("/BitsPerComponent")] = NumberObject(8)
            stale = ["/DecodeParms", "/DecodeParams", "/SMask"]
            if source_bands != len(pil.getbands()):
                # Converted (CMYK to RGB): /Decode describes the old components.
                stale.append("/Decode")
            for key in stale:
                if key in obj:
                    del obj[NameObject(key)]
            replaced += 1
        except Exception:
            # Leave this image as-is; it may now be partially mutated, but the
            # eligible cases above are known-good, so this is defensive only.
            continue

    return replaced


def compress_pdf(input_path, output_path, image_quality=60, image_max_dpi=150):
    """Compress a PDF, writing the result to ``output_path``.

    ``image_quality`` (JPEG quality, ~10-95) and ``image_max_dpi`` (target
    on-page resolution ceiling for embedded rasters) tune the image pass.

    Returns a dict::

        {
            "original_bytes": int,
            "compressed_bytes": int,
            "ratio": float,          # compressed / original (1.0 == no gain)
            "images_recompressed": int,
        }

    If the optimised output would be larger than the input, the original bytes
    are copied to ``output_path`` unchanged and ``ratio`` is reported as ``1.0``.
    """
    import os

    original_bytes = os.path.getsize(input_path)

    reader = PdfReader(input_path)
    writer = PdfWriter()

    # Capture per-page display sizes from the *original* pages before content
    # streams get re-packed (compress_content_streams rewrites them).
    per_page_sizes = [_content_display_sizes(page) for page in reader.pages]

    for page in reader.pages:
        writer.add_page(page)

    # --- Lossless pass ---
    for page in writer.pages:
        try:
            page.compress_content_streams()
        except Exception:
            # A page that resists content-stream compression is fine as-is.
            pass

    # Carry over document metadata.
    try:
        if reader.metadata:
            writer.add_metadata(
                {k: v for k, v in reader.metadata.items() if isinstance(k, str)}
            )
    except Exception:
        pass

    # --- Image pass (best-effort) ---
    images_recompressed = 0
    if _HAVE_PIL:
        for idx, page in enumerate(writer.pages):
            sizes = per_page_sizes[idx] if idx < len(per_page_sizes) else {}
            try:
                images_recompressed += _recompress_page_images(
                    page, sizes, image_quality, image_max_dpi
                )
            except Exception:
                # Never let the image pass abort the whole compression.
                continue

    with open(output_path, "wb") as fh:
        writer.write(fh)

    compressed_bytes = os.path.getsize(output_path)

    # If we made it bigger, keep the original bytes instead.
    if compressed_bytes >= original_bytes:
        shutil.copyfile(input_path, output_path)
        return {
            "original_bytes": original_bytes,
            "compressed_bytes": original_bytes,
            "ratio": 1.0,
            "images_recompressed": images_recompressed,
        }

    ratio = compressed_bytes / original_bytes if original_bytes else 1.0
    return {
        "original_bytes": original_bytes,
        "compressed_bytes": compressed_bytes,
        "ratio": ratio,
        "images_recompressed": images_recompressed,
    }
