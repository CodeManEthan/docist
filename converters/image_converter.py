"""Image converter plugin: turns raster images into PDF pages on the chosen paper.

Handles common raster formats. Each image is normalised to RGB (transparency
is composited onto a white background), then scaled to fit inside the printable
area of the page (Letter by default, with margins) without upscaling, and centred on a
white page. Multi-frame images (e.g. animated GIFs, multi-page TIFFs) produce
one PDF page per frame.
"""
import io

from PIL import Image, ImageSequence
from pypdf import PdfReader, PdfWriter

from converters import ConversionError
from converters.options import paper_size
from pdf_ops import limits

EXTENSIONS = ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tiff', '.tif']

# Page geometry in pixels at the working resolution below.
DPI = 150
MARGIN = int(0.5 * DPI)     # 75 px margin on every side


def page_pixels(opts=None):
    """Page ``(width, height)`` in pixels at DPI: 1275 x 1650 for Letter."""
    w_pt, h_pt = paper_size(opts)
    return round(w_pt * DPI / 72), round(h_pt * DPI / 72)


def _has_alpha(frame):
    return frame.mode in ('RGBA', 'LA') or (frame.mode == 'P' and 'transparency' in frame.info)


def _compose_page(frame, page_w, page_h):
    """Fit a single frame onto a centred, white page of ``page_w`` x ``page_h`` px (RGB).

    Rule D: a frame with transparency is pasted onto the white page with its
    alpha band as the mask, after scaling, instead of building an RGBA copy,
    a white RGBA background and their composite at full size. At 36
    megapixels that is about 309 MiB in the job instead of about 695
    (design launch-hardening §6.2).
    """
    if _has_alpha(frame):
        img = frame if frame.mode == 'RGBA' else frame.convert('RGBA')
    else:
        img = frame if frame.mode == 'RGB' else frame.convert('RGB')

    # Scale down to fit the printable area; never upscale small images.
    scale = min((page_w - 2 * MARGIN) / img.width,
                (page_h - 2 * MARGIN) / img.height, 1.0)
    if scale < 1.0:
        new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
        img = img.resize(new_size, Image.LANCZOS)

    page = Image.new('RGB', (page_w, page_h), (255, 255, 255))
    offset = ((page_w - img.width) // 2, (page_h - img.height) // 2)
    if img.mode == 'RGBA':
        page.paste(img.convert('RGB'), offset, mask=img.getchannel('A'))
    else:
        page.paste(img, offset)
    return page


def _page_pdf(page):
    """One composed page as a one-page PDF (Pillow writes RGB as a JPEG stream)."""
    buf = io.BytesIO()
    page.save(buf, 'PDF', resolution=float(DPI))
    buf.seek(0)
    return buf


def convert(input_path, output_path, opts=None):
    """Convert the image at input_path into a PDF written to output_path.

    One frame at a time (Rule D): each frame is checked against
    ``FRAME_PIXELS`` after its seek and before it is decoded, composed, saved
    as a one-page PDF and appended to the writer, then dropped. The writer
    holds compressed pages only.
    """
    try:
        image = Image.open(input_path)
    except Image.DecompressionBombError as exc:
        # Over Pillow's own limit (about 179 MP), before check_frame runs:
        # the same ruled sentence as any image over FRAME_PIXELS.
        raise limits.LimitError(limits.image_message(), kind='frame') from exc
    except Exception as exc:
        raise ConversionError(f"Could not open image '{input_path}': {exc}") from exc

    writer = PdfWriter()
    count = 0
    try:
        page_w, page_h = page_pixels(opts)
        for frame in ImageSequence.Iterator(image):
            limits.check_frame(frame.width, frame.height)
            page = _compose_page(frame, page_w, page_h)
            buf = _page_pdf(page)
            del page
            writer.append(PdfReader(buf))
            count += 1
    except limits.LimitError:
        raise
    except Image.DecompressionBombError as exc:
        raise limits.LimitError(limits.image_message(), kind='frame') from exc
    except Exception as exc:
        raise ConversionError(f"Could not process image '{input_path}': {exc}") from exc

    if not count:
        raise ConversionError(f"Image '{input_path}' contained no frames to convert.")

    try:
        with open(output_path, 'wb') as fh:
            writer.write(fh)
    except Exception as exc:
        raise ConversionError(f"Could not write PDF for '{input_path}': {exc}") from exc
