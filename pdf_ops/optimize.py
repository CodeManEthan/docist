"""PDF compression: lossless content-stream packing + best-effort image recompression.

Pure PDF-processing (no Flask dependency), built on pypdf and Pillow.

Two passes are applied by :func:`compress_pdf`:

1. **Lossless pass** — every page is cloned into a fresh ``PdfWriter`` and
   :meth:`PageObject.compress_content_streams` is called on it. This
   Flate-compresses page content streams that were stored uncompressed and
   drops redundancy without changing a single rendered pixel.
   Document metadata is carried over onto the writer.

2. **Image pass (best-effort)** — each page's ``/Resources /XObject`` image
   objects are walked. For images we can safely rasterise (8-bit DeviceRGB /
   DeviceGray, whether stored as ``/DCTDecode`` JPEG or ``/FlateDecode`` raw
   raster) the pixels are decoded with Pillow, optionally *downsampled* so the
   effective on-page resolution does not exceed ``image_max_dpi`` (the display
   size is recovered by tracking the CTM through the page content stream), and
   re-encoded as JPEG at ``image_quality``. The XObject stream is then rewritten
   in place to ``/DCTDecode``.

   This pass is deliberately conservative: any image that is not plainly
   RGB/gray 8-bit, that carries a soft mask / alpha (``/SMask``), that uses an
   indexed or CMYK colour space, or that fails to decode for any reason is left
   **untouched** — one stubborn image never fails the whole run. Only images
   that come out *smaller* after re-encoding are actually replaced.

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
from pypdf.generic import NameObject, NumberObject

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


def _pil_from_xobject(obj):
    """Decode an image XObject into a Pillow image, or ``None`` if unsupported.

    Only the plainly re-encodable cases are handled: 8-bit DeviceRGB /
    DeviceGray rasters (``/FlateDecode`` etc.) and baseline JPEG
    (``/DCTDecode``). Anything with an alpha soft mask, an indexed / CMYK
    colour space, or a non-8-bit depth returns ``None`` so the caller skips it.
    """
    filt = obj.get("/Filter")
    filt_s = str(filt)
    color = obj.get("/ColorSpace")
    bpc = obj.get("/BitsPerComponent")

    # Alpha / soft-mask images would lose transparency through JPEG; skip them.
    if "/SMask" in obj or "/Mask" in obj:
        return None

    if "/DCTDecode" in filt_s:
        # Already JPEG-encoded: let Pillow parse the raw stream bytes.
        try:
            im = Image.open(io.BytesIO(obj._data))
            im.load()
            return im
        except Exception:
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
        raw = obj.get_data()  # applies the stream filters -> raw pixels
        return Image.frombytes(mode, (w, h), raw)
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
            obj[NameObject("/ColorSpace")] = NameObject(
                "/DeviceRGB" if pil.mode == "RGB" else "/DeviceGray"
            )
            obj[NameObject("/BitsPerComponent")] = NumberObject(8)
            for key in ("/DecodeParms", "/DecodeParams", "/SMask"):
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
