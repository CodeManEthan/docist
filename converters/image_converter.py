"""Image converter plugin: turns raster images into letter-size PDF pages.

Handles common raster formats. Each image is normalised to RGB (transparency
is composited onto a white background), then scaled to fit inside the printable
area of a US-letter page (with margins) without upscaling, and centred on a
white page. Multi-frame images (e.g. animated GIFs, multi-page TIFFs) produce
one PDF page per frame.
"""
from PIL import Image, ImageSequence

from converters import ConversionError

EXTENSIONS = ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tiff', '.tif']

# US-letter page geometry, in pixels at the working resolution below.
DPI = 150
LETTER_W = int(8.5 * DPI)   # 1275 px
LETTER_H = int(11 * DPI)    # 1650 px
MARGIN = int(0.5 * DPI)     # 75 px margin on every side
PRINTABLE_W = LETTER_W - 2 * MARGIN
PRINTABLE_H = LETTER_H - 2 * MARGIN


def _flatten_to_rgb(frame):
    """Return an RGB copy of a frame, compositing any transparency onto white."""
    if frame.mode in ('RGBA', 'LA') or (frame.mode == 'P' and 'transparency' in frame.info):
        rgba = frame.convert('RGBA')
        background = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert('RGB')
    return frame.convert('RGB')


def _compose_page(frame):
    """Fit a single frame onto a centred, white US-letter page (RGB)."""
    img = _flatten_to_rgb(frame)

    # Scale down to fit the printable area; never upscale small images.
    scale = min(PRINTABLE_W / img.width, PRINTABLE_H / img.height, 1.0)
    if scale < 1.0:
        new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
        img = img.resize(new_size, Image.LANCZOS)

    page = Image.new('RGB', (LETTER_W, LETTER_H), (255, 255, 255))
    offset = ((LETTER_W - img.width) // 2, (LETTER_H - img.height) // 2)
    page.paste(img, offset)
    return page


def convert(input_path, output_path):
    """Convert the image at input_path into a PDF written to output_path."""
    try:
        image = Image.open(input_path)
    except Exception as exc:
        raise ConversionError(f"Could not open image '{input_path}': {exc}") from exc

    try:
        pages = [_compose_page(frame) for frame in ImageSequence.Iterator(image)]
    except Exception as exc:
        raise ConversionError(f"Could not process image '{input_path}': {exc}") from exc

    if not pages:
        raise ConversionError(f"Image '{input_path}' contained no frames to convert.")

    try:
        first, rest = pages[0], pages[1:]
        first.save(
            output_path,
            'PDF',
            resolution=float(DPI),
            save_all=True,
            append_images=rest,
        )
    except Exception as exc:
        raise ConversionError(f"Could not write PDF for '{input_path}': {exc}") from exc
