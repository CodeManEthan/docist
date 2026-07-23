"""Tests for the raster image-to-image transform matrix (transforms/images.py).

All fixtures are generated programmatically into ``tmp_path`` -- no binary
sample files are committed. Each test builds its source image, runs the
registered transform, and reopens the output in Pillow to assert format,
dimensions and (where relevant) pixel content.
"""
import pytest
from PIL import Image

import transforms
from transforms import TransformError, images


# --------------------------------------------------------------------------
# Programmatic image builders
# --------------------------------------------------------------------------
def make_rgba_png(path, size=(320, 240), color=(200, 30, 30, 128)):
    """RGBA PNG with a semi-transparent fill."""
    Image.new("RGBA", size, color).save(path, "PNG")
    return path


def make_rgb_jpg(path, size=(160, 120), color=(20, 140, 60)):
    """Opaque RGB JPEG."""
    Image.new("RGB", size, color).save(path, "JPEG", quality=95)
    return path


def make_palette_gif(path, size=(100, 80), color=(10, 200, 50)):
    """Single-frame palette (P-mode) GIF."""
    Image.new("RGB", size, color).convert(
        "P", palette=Image.ADAPTIVE, colors=64
    ).save(path, "GIF")
    return path


def make_webp(path, size=(120, 90), color=(30, 60, 200, 200)):
    """RGBA WebP."""
    Image.new("RGBA", size, color).save(path, "WEBP")
    return path


def make_tiff(path, size=(140, 100), color=(10, 20, 30)):
    """RGB TIFF (single frame)."""
    Image.new("RGB", size, color).save(path, "TIFF")
    return path


def make_heic(path, size=(128, 96), color=(60, 30, 220, 255)):
    """HEIC image saved via pillow-heif (RGBA)."""
    Image.new("RGBA", size, color).save(path, "HEIF")
    return path


def make_animated_gif(path, size=(64, 48)):
    """A 3-frame animated palette GIF."""
    frames = [
        Image.new("RGB", size, c).convert("P", palette=Image.ADAPTIVE, colors=16)
        for c in [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    ]
    frames[0].save(
        path, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0
    )
    return path


def run(src_ext, dst_ext, in_path, out_path):
    """Fetch the registered transform and run it."""
    fn = transforms.get_transform(src_ext, dst_ext)
    assert fn is not None, f"no transform for {src_ext} -> {dst_ext}"
    return fn(str(in_path), str(out_path))


# --------------------------------------------------------------------------
# Representative pair conversions
# --------------------------------------------------------------------------
def test_png_to_jpg_flattens_alpha_onto_white(tmp_path):
    # Fully transparent PNG -> the whole image must become white in JPEG.
    src = make_rgba_png(tmp_path / "a.png", size=(64, 64), color=(0, 0, 0, 0))
    out = tmp_path / "a.jpg"
    run(".png", ".jpg", src, out)

    with Image.open(out) as im:
        assert im.format == "JPEG"
        assert im.size == (64, 64)
        assert im.mode == "RGB"
        # A corner pixel of a fully transparent source flattens to white.
        corner = im.getpixel((0, 0))
        assert all(c >= 250 for c in corner), corner


def test_png_to_jpg_semi_transparent_blends_toward_white(tmp_path):
    # 50%-alpha red over white -> a light pinkish red, not pure red.
    src = make_rgba_png(tmp_path / "b.png", size=(32, 32), color=(255, 0, 0, 128))
    out = tmp_path / "b.jpg"
    run(".png", ".jpg", src, out)
    with Image.open(out) as im:
        r, g, b = im.getpixel((16, 16))
        assert r > 200 and g > 90 and b > 90  # blended toward white


def test_jpg_to_png(tmp_path):
    src = make_rgb_jpg(tmp_path / "c.jpg", size=(160, 120))
    out = tmp_path / "c.png"
    run(".jpg", ".png", src, out)
    with Image.open(out) as im:
        assert im.format == "PNG"
        assert im.size == (160, 120)


def test_png_to_webp_keeps_alpha(tmp_path):
    src = make_rgba_png(tmp_path / "d.png", size=(80, 60), color=(10, 20, 30, 0))
    out = tmp_path / "d.webp"
    run(".png", ".webp", src, out)
    with Image.open(out) as im:
        assert im.format == "WEBP"
        assert im.size == (80, 60)
        assert im.mode in ("RGBA", "LA"), im.mode
        # Alpha preserved: the transparent source stays transparent.
        assert im.convert("RGBA").getpixel((0, 0))[3] == 0


def test_webp_to_bmp(tmp_path):
    src = make_webp(tmp_path / "e.webp", size=(120, 90))
    out = tmp_path / "e.bmp"
    run(".webp", ".bmp", src, out)
    with Image.open(out) as im:
        assert im.format == "BMP"
        assert im.size == (120, 90)
        assert im.mode == "RGB"


def test_gif_to_png(tmp_path):
    src = make_palette_gif(tmp_path / "f.gif", size=(100, 80))
    out = tmp_path / "f.png"
    run(".gif", ".png", src, out)
    with Image.open(out) as im:
        assert im.format == "PNG"
        assert im.size == (100, 80)


def test_tiff_to_png(tmp_path):
    src = make_tiff(tmp_path / "g.tiff", size=(140, 100))
    out = tmp_path / "g.png"
    run(".tiff", ".png", src, out)
    with Image.open(out) as im:
        assert im.format == "PNG"
        assert im.size == (140, 100)


def test_tif_alias_to_png(tmp_path):
    # The .tif alias must resolve to the same codec as .tiff.
    src = make_tiff(tmp_path / "g2.tif", size=(50, 40))
    out = tmp_path / "g2.png"
    run(".tif", ".png", src, out)
    with Image.open(out) as im:
        assert im.format == "PNG"
        assert im.size == (50, 40)


def test_png_to_gif_produces_palette(tmp_path):
    src = make_rgba_png(tmp_path / "h.png", size=(48, 48), color=(12, 200, 90, 255))
    out = tmp_path / "h.gif"
    run(".png", ".gif", src, out)
    with Image.open(out) as im:
        assert im.format == "GIF"
        assert im.size == (48, 48)
        assert im.mode == "P"


def test_animated_gif_to_png_takes_first_frame(tmp_path):
    src = make_animated_gif(tmp_path / "anim.gif", size=(64, 48))
    out = tmp_path / "anim.png"
    run(".gif", ".png", src, out)
    with Image.open(out) as im:
        assert im.format == "PNG"
        assert im.size == (64, 48)
        # First frame was solid red.
        r, g, b = im.convert("RGB").getpixel((32, 24))
        assert r > 200 and g < 60 and b < 60, (r, g, b)


# --------------------------------------------------------------------------
# HEIC round-trip
# --------------------------------------------------------------------------
def test_png_to_heic(tmp_path):
    src = make_rgba_png(tmp_path / "i.png", size=(128, 96), color=(60, 30, 220, 255))
    out = tmp_path / "i.heic"
    run(".png", ".heic", src, out)
    with Image.open(out) as im:
        assert im.format == "HEIF"
        assert im.size == (128, 96)


def test_heic_to_jpg(tmp_path):
    src = make_heic(tmp_path / "j.heic", size=(128, 96), color=(60, 30, 220, 255))
    out = tmp_path / "j.jpg"
    run(".heic", ".jpg", src, out)
    with Image.open(out) as im:
        assert im.format == "JPEG"
        assert im.size == (128, 96)
        assert im.mode == "RGB"


def test_png_heic_roundtrip_preserves_dimensions_and_color(tmp_path):
    png = make_rgba_png(tmp_path / "k.png", size=(100, 100), color=(40, 160, 90, 255))
    heic = tmp_path / "k.heic"
    back = tmp_path / "k_back.png"
    run(".png", ".heic", png, heic)
    run(".heic", ".png", heic, back)
    with Image.open(back) as im:
        assert im.format == "PNG"
        assert im.size == (100, 100)
        r, g, b = im.convert("RGB").getpixel((50, 50))
        # HEIC is lossy but the solid color should survive roughly intact.
        assert abs(r - 40) < 25 and abs(g - 160) < 25 and abs(b - 90) < 25, (r, g, b)


def test_heif_alias_source(tmp_path):
    # The .heif alias resolves to the same codec as .heic.
    src = make_heic(tmp_path / "l.heif", size=(64, 64))
    out = tmp_path / "l.png"
    run(".heif", ".png", src, out)
    with Image.open(out) as im:
        assert im.format == "PNG"
        assert im.size == (64, 64)


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------
def test_corrupt_input_raises_transform_error(tmp_path):
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n this is not a real png body")
    out = tmp_path / "broken.jpg"
    with pytest.raises(TransformError):
        run(".png", ".jpg", bad, out)


def test_empty_input_raises_transform_error(tmp_path):
    bad = tmp_path / "empty.png"
    bad.write_bytes(b"")
    out = tmp_path / "empty.jpg"
    with pytest.raises(TransformError):
        run(".png", ".jpg", bad, out)


# --------------------------------------------------------------------------
# Registry integration
# --------------------------------------------------------------------------
def test_registry_get_transform_png_jpg():
    assert transforms.get_transform(".png", ".jpg") is not None


def test_registry_identity_and_same_codec_pairs_skipped():
    # Same string -> None (registry rule). Alias pairs share a codec so are
    # not registered as direct transforms either.
    assert transforms.get_transform(".png", ".png") is None
    assert (".jpg", ".jpeg") not in images.TRANSFORMS
    assert (".tif", ".tiff") not in images.TRANSFORMS
    assert (".heic", ".heif") not in images.TRANSFORMS


def test_targets_for_png_includes_family():
    targets = transforms.targets_for(".png")
    for ext in [".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif",
                ".heic", ".heif"]:
        assert ext in targets, ext


def test_matrix_populated_for_image_sources():
    m = transforms.matrix()
    for ext in images.FORMATS:
        assert ext in m, ext
        assert m[ext], f"{ext} has no targets"


def test_transform_count_is_full_cross_codec_matrix():
    # 10 extensions, 7 codecs of sizes 1,2,1,1,2,1,2 -> 16 same-codec pairs.
    # 10*10 - 16 = 84 registered cross-codec transforms.
    assert len(images.TRANSFORMS) == 84
