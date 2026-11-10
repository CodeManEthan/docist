"""Tests for PDF compression: pure ``compress_pdf`` + the /pages/run route.

The bloated fixture is generated programmatically in ``tmp_path`` (reportlab +
a large, hard-to-compress Pillow raster) so no binary samples are committed.
Reuses the shared ``client`` / ``builders`` fixtures from conftest.py.
"""
import io
import os
import random

import pytest
from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NullObject, NumberObject

from pdf_ops.optimize import compress_pdf


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
def _make_bloated_pdf(path, pages=2, marker="CompressMarker"):
    """A large PDF: a high-res noisy raster (stored FlateDecode) on each page
    plus repeated extractable text. Noisy pixels defeat lossless compression,
    so the image pass has real work to do."""
    rng = random.Random(1234)
    w, h = 1200, 900
    img = Image.new("RGB", (w, h))
    img.putdata([
        (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
        for _ in range(w * h)
    ])
    img_path = str(path) + ".src.png"
    img.save(img_path, "PNG")

    c = canvas.Canvas(str(path), pagesize=letter)
    for i in range(pages):
        c.drawImage(img_path, 50, 300, width=500, height=375)
        c.setFont("Helvetica", 12)
        c.drawString(72, 100, f"{marker} page {i + 1} " + ("lorem ipsum " * 15))
        c.showPage()
    c.save()
    os.remove(img_path)
    return path


def _make_minimal_pdf(path):
    """A hand-written, already-tiny single-page PDF that PdfWriter cannot beat
    (exercises the larger-output fallback)."""
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        (b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
         b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>"),
        b"<</Length 33>>\nstream\nBT /F1 24 Tf 20 100 Td (Hi) Tj ET\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_off = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<</Size %d/Root 1 0 R>>\n" % (len(objs) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref_off
    path.write_bytes(bytes(out))
    return path


# --------------------------------------------------------------------------
# compress_pdf — core behaviour
# --------------------------------------------------------------------------
def test_compress_shrinks_and_stays_valid(tmp_path):
    src = _make_bloated_pdf(tmp_path / "bloat.pdf", pages=2)
    out = tmp_path / "out.pdf"
    original = os.path.getsize(str(src))

    stats = compress_pdf(str(src), str(out))

    # Output is a valid, readable PDF with the same page count.
    reader = PdfReader(str(out))
    assert len(reader.pages) == 2
    # Not larger than the input.
    assert os.path.getsize(str(out)) <= original


def test_compress_preserves_text(tmp_path):
    src = _make_bloated_pdf(tmp_path / "bloat.pdf", pages=2, marker="KeepMe")
    out = tmp_path / "out.pdf"
    compress_pdf(str(src), str(out))

    reader = PdfReader(str(out))
    text = "\n".join(p.extract_text() or "" for p in reader.pages)
    assert "KeepMe" in text


def test_compress_recompresses_images(tmp_path):
    src = _make_bloated_pdf(tmp_path / "bloat.pdf", pages=2)
    out = tmp_path / "out.pdf"
    stats = compress_pdf(str(src), str(out))
    # Both page rasters should be recompressed.
    assert stats["images_recompressed"] >= 1


def test_compress_stats_dict_is_sane(tmp_path):
    src = _make_bloated_pdf(tmp_path / "bloat.pdf", pages=1)
    out = tmp_path / "out.pdf"
    stats = compress_pdf(str(src), str(out))

    assert set(stats) == {
        "original_bytes", "compressed_bytes", "ratio", "images_recompressed",
    }
    assert stats["original_bytes"] == os.path.getsize(str(src))
    assert stats["compressed_bytes"] == os.path.getsize(str(out))
    assert 0.0 < stats["ratio"] <= 1.0
    assert stats["compressed_bytes"] <= stats["original_bytes"]
    assert isinstance(stats["images_recompressed"], int)
    assert stats["images_recompressed"] >= 0


def test_compress_dpi_downsamples_images(tmp_path):
    # A 1200x900 image displayed at 500x375pt is ~173 DPI; capping at 96 must
    # shrink the stored pixel dimensions.
    src = _make_bloated_pdf(tmp_path / "bloat.pdf", pages=1)
    out = tmp_path / "out.pdf"
    compress_pdf(str(src), str(out), image_quality=50, image_max_dpi=96)

    reader = PdfReader(str(out))
    xobjs = reader.pages[0]["/Resources"]["/XObject"]
    widths = [int(xobjs[n].get_object()["/Width"]) for n in xobjs
              if xobjs[n].get_object().get("/Subtype") == "/Image"]
    assert widths and all(w < 1200 for w in widths)


def test_compress_quality_affects_size(tmp_path):
    src = _make_bloated_pdf(tmp_path / "bloat.pdf", pages=2)
    low = tmp_path / "low.pdf"
    high = tmp_path / "high.pdf"
    compress_pdf(str(src), str(low), image_quality=20)
    compress_pdf(str(src), str(high), image_quality=90)
    # Lower JPEG quality yields a smaller file.
    assert os.path.getsize(str(low)) < os.path.getsize(str(high))


# --------------------------------------------------------------------------
# compress_pdf — larger-output fallback
# --------------------------------------------------------------------------
def test_compress_fallback_keeps_original_when_larger(tmp_path):
    src = _make_minimal_pdf(tmp_path / "mini.pdf")
    out = tmp_path / "out.pdf"
    original_bytes = src.read_bytes()

    stats = compress_pdf(str(src), str(out))

    assert stats["ratio"] == 1.0
    assert stats["compressed_bytes"] == stats["original_bytes"]
    # The output is the original bytes, byte-for-byte, and still readable.
    assert out.read_bytes() == original_bytes
    assert len(PdfReader(str(out)).pages) == 1


# --------------------------------------------------------------------------
# Route: POST /pages/run  (operation=compress)
# --------------------------------------------------------------------------
def _bloated_bytes(tmp_path):
    src = _make_bloated_pdf(tmp_path / "input.pdf", pages=2)
    return src.read_bytes()


def test_run_compress_success(client, tmp_path):
    data = _bloated_bytes(tmp_path)
    resp = client.post("/pages/run", data={
        "operation": "compress",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["success"] is True
    assert payload["filename"].endswith(".pdf")
    # Message carries human-readable sizes.
    assert "MB" in payload["message"] or "KB" in payload["message"] or "B" in payload["message"]

    out = client.output_dir / payload["filename"]
    assert out.exists()
    assert payload["download_url"] == "/download?filename=" + payload["filename"]
    assert len(PdfReader(str(out)).pages) == 2


def test_run_compress_with_params(client, tmp_path):
    data = _bloated_bytes(tmp_path)
    resp = client.post("/pages/run", data={
        "operation": "compress",
        "image_quality": "40",
        "image_max_dpi": "100",
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


@pytest.mark.parametrize("quality", ["5", "99", "abc", "0"])
def test_run_compress_bad_quality_is_400(client, tmp_path, quality):
    data = _bloated_bytes(tmp_path)
    resp = client.post("/pages/run", data={
        "operation": "compress",
        "image_quality": quality,
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


@pytest.mark.parametrize("dpi", ["10", "999", "xyz"])
def test_run_compress_bad_dpi_is_400(client, tmp_path, dpi):
    data = _bloated_bytes(tmp_path)
    resp = client.post("/pages/run", data={
        "operation": "compress",
        "image_max_dpi": dpi,
        "file": (io.BytesIO(data), "input.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# --------------------------------------------------------------------------
# compress_pdf — JPEG images behind a filter chain, and their colour spaces
# --------------------------------------------------------------------------
def _noisy_jpeg(mode="RGB", size=(1200, 900), quality=95):
    """A JPEG of random noise: large at high quality, so the image pass can
    always beat it at quality 50."""
    rng = random.Random(99)
    bands = len(Image.new(mode, (1, 1)).getbands())
    img = Image.frombytes(mode, size, rng.randbytes(size[0] * size[1] * bands))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _make_jpeg_pdf(path, jpeg, monkeypatch=None, a85=True, pages=1):
    """A reportlab PDF that embeds ``jpeg`` as is, drawn at 500 x 375 pt.

    reportlab stores the JPEG as ``[/ASCII85Decode /DCTDecode]`` by default
    and as ``[/DCTDecode]`` with ``rl_config.useA85 = 0``."""
    from reportlab import rl_config
    from reportlab.lib.utils import ImageReader

    if not a85:
        monkeypatch.setattr(rl_config, "useA85", 0)
    c = canvas.Canvas(str(path), pagesize=letter)
    for i in range(pages):
        c.drawImage(ImageReader(io.BytesIO(jpeg)), 50, 300, width=500, height=375)
        c.drawString(72, 100, f"photo page {i + 1}")
        c.showPage()
    c.save()
    return path


def _images(path):
    """Every image XObject in the PDF at ``path``, in page order."""
    out = []
    for page in PdfReader(str(path)).pages:
        xobjs = page["/Resources"]["/XObject"]
        for name in xobjs:
            obj = xobjs[name].get_object()
            if obj.get("/Subtype") == "/Image":
                out.append(obj)
    return out


def _edit_images(src, dst, edit):
    """Copy ``src`` to ``dst``, calling ``edit(writer, image)`` on every image
    XObject first."""
    from pypdf import PdfWriter

    writer = PdfWriter(clone_from=str(src))
    for page in writer.pages:
        xobjs = page["/Resources"]["/XObject"]
        for name in xobjs:
            obj = xobjs[name].get_object()
            if obj.get("/Subtype") == "/Image":
                edit(writer, obj)
    writer.write(str(dst))
    return dst


def _icc_space(writer, n):
    """``[/ICCBased <stream /N n>]``. The compressor reads only ``/N``, so the
    profile bytes are a placeholder."""
    from pypdf.generic import StreamObject

    stream = StreamObject()
    stream.set_data(b"placeholder ICC profile")
    stream[NameObject("/N")] = NumberObject(n)
    return ArrayObject([NameObject("/ICCBased"), writer._add_object(stream)])


def _filters(obj):
    filt = obj["/Filter"]
    return [str(f) for f in filt] if isinstance(filt, list) else [str(filt)]


def test_compress_shrinks_jpeg_behind_ascii85(tmp_path):
    src = _make_jpeg_pdf(tmp_path / "a85.pdf", _noisy_jpeg(), pages=2)
    assert [_filters(im) for im in _images(src)] == [["/ASCII85Decode", "/DCTDecode"]] * 2
    out = tmp_path / "out.pdf"

    stats = compress_pdf(str(src), str(out), image_quality=50, image_max_dpi=100)

    assert stats["images_recompressed"] >= 1
    assert stats["ratio"] < 0.5
    assert len(PdfReader(str(out)).pages) == 2
    assert all(_filters(im) == ["/DCTDecode"] for im in _images(out))


def test_compress_one_element_dct_array_still_recompresses(tmp_path, monkeypatch):
    src = _make_jpeg_pdf(tmp_path / "dct.pdf", _noisy_jpeg(), monkeypatch, a85=False)
    (image,) = _images(src)
    assert isinstance(image["/Filter"], list) and _filters(image) == ["/DCTDecode"]
    out = tmp_path / "out.pdf"

    stats = compress_pdf(str(src), str(out), image_quality=50, image_max_dpi=100)

    assert stats["images_recompressed"] == 1
    (image,) = _images(out)
    assert int(image["/Width"]) < 1200


@pytest.mark.parametrize("mode, n", [("RGB", 3), ("L", 1)])
def test_compress_keeps_icc_colour_space(tmp_path, monkeypatch, mode, n):
    src = _make_jpeg_pdf(tmp_path / "plain.pdf", _noisy_jpeg(mode), monkeypatch, a85=False)

    def to_icc(writer, image):
        image[NameObject("/ColorSpace")] = _icc_space(writer, n)

    icc = _edit_images(src, tmp_path / "icc.pdf", to_icc)
    out = tmp_path / "out.pdf"

    stats = compress_pdf(str(icc), str(out), image_quality=50, image_max_dpi=100)

    assert stats["images_recompressed"] == 1
    (image,) = _images(out)
    space = image["/ColorSpace"]
    assert space[0] == "/ICCBased"
    assert space[1].get_object()["/N"] == n
    assert int(image["/Width"]) < 1200


def test_compress_keeps_decode_array_on_rgb(tmp_path, monkeypatch):
    src = _make_jpeg_pdf(tmp_path / "plain.pdf", _noisy_jpeg(), monkeypatch, a85=False)
    decode = [1, 0, 1, 0, 1, 0]

    def add_decode(writer, image):
        image[NameObject("/ColorSpace")] = NameObject("/DeviceRGB")
        image[NameObject("/Decode")] = ArrayObject(NumberObject(v) for v in decode)

    edited = _edit_images(src, tmp_path / "decode.pdf", add_decode)
    out = tmp_path / "out.pdf"

    stats = compress_pdf(str(edited), str(out), image_quality=50, image_max_dpi=100)

    assert stats["images_recompressed"] == 1
    (image,) = _images(out)
    assert image["/ColorSpace"] == "/DeviceRGB"
    assert [int(v) for v in image["/Decode"]] == decode


def test_compress_cmyk_jpeg_becomes_rgb_without_decode(tmp_path, monkeypatch):
    import pypdfium2 as pdfium

    src = _make_jpeg_pdf(tmp_path / "cmyk.pdf", _noisy_jpeg("CMYK"), monkeypatch, a85=False)

    def cmyk_decode(writer, image):
        image[NameObject("/ColorSpace")] = NameObject("/DeviceCMYK")
        image[NameObject("/Decode")] = ArrayObject(NumberObject(v) for v in [1, 0] * 4)

    edited = _edit_images(src, tmp_path / "cmyk-decode.pdf", cmyk_decode)
    out = tmp_path / "out.pdf"

    stats = compress_pdf(str(edited), str(out), image_quality=50, image_max_dpi=100)

    assert stats["images_recompressed"] == 1
    (image,) = _images(out)
    assert image["/ColorSpace"] == "/DeviceRGB"
    assert "/Decode" not in image
    pdf = pdfium.PdfDocument(str(out))
    try:
        rendered = pdf[0].render(scale=0.5).to_pil()
    finally:
        pdf.close()
    # The photo's area (500 x 375 pt at 50, 117 from the top, half scale)
    # renders as noise, not as one flat colour.
    photo = rendered.convert("L").crop((25, 59, 275, 246))
    low, high = photo.getextrema()
    assert high - low > 64


@pytest.mark.parametrize("space", ["none", "icc-without-n"])
def test_compress_overwrites_other_colour_spaces_from_jpeg(tmp_path, monkeypatch, space):
    src = _make_jpeg_pdf(tmp_path / "plain.pdf", _noisy_jpeg(), monkeypatch, a85=False)

    def set_space(writer, image):
        if space == "none":
            del image[NameObject("/ColorSpace")]
        else:
            icc = _icc_space(writer, 3)
            del icc[1].get_object()[NameObject("/N")]
            image[NameObject("/ColorSpace")] = icc

    edited = _edit_images(src, tmp_path / "space.pdf", set_space)
    out = tmp_path / "out.pdf"

    stats = compress_pdf(str(edited), str(out), image_quality=50, image_max_dpi=100)

    assert stats["images_recompressed"] == 1
    (image,) = _images(out)
    assert image["/ColorSpace"] == "/DeviceRGB"


def test_compress_leaves_chain_with_dct_first_alone(tmp_path):
    jpeg = _noisy_jpeg()
    src = _make_jpeg_pdf(tmp_path / "plain.pdf", jpeg)

    def dct_first(writer, image):
        # The JPEG bytes as stored, but declared as DCT then Flate: a reader
        # that ignored the order would open and recompress them.
        image._data = jpeg
        image[NameObject("/Filter")] = ArrayObject(
            [NameObject("/DCTDecode"), NameObject("/FlateDecode")]
        )

    edited = _edit_images(src, tmp_path / "dct-first.pdf", dct_first)
    stored = _images(edited)[0]._data
    out = tmp_path / "out.pdf"

    stats = compress_pdf(str(edited), str(out), image_quality=50, image_max_dpi=100)

    assert stats["images_recompressed"] == 0
    (image,) = _images(out)
    assert image._data == stored == jpeg
    assert _filters(image) == ["/DCTDecode", "/FlateDecode"]


def test_compress_chained_jpeg_over_frame_limit_is_never_decoded(tmp_path, monkeypatch):
    from pdf_ops import limits
    from pdf_ops import optimize

    src = _make_jpeg_pdf(tmp_path / "a85.pdf", _noisy_jpeg())
    monkeypatch.setattr(limits, "FRAME_PIXELS", 1000)
    decoded = []
    monkeypatch.setattr(optimize, "decode_stream_data", lambda obj: decoded.append(obj))

    stats = compress_pdf(str(src), str(tmp_path / "out.pdf"), image_quality=50, image_max_dpi=100)

    assert stats["images_recompressed"] == 0
    assert decoded == []


def _crypt_identity(image):
    image[NameObject("/Filter")] = ArrayObject(
        [NameObject("/Crypt"), NameObject("/DCTDecode")]
    )
    image[NameObject("/DecodeParms")] = ArrayObject([
        DictionaryObject({
            NameObject("/Type"): NameObject("/CryptFilterDecodeParms"),
            NameObject("/Name"): NameObject("/Identity"),
        }),
        NullObject(),
    ])


def _flate_label(image):
    # A broken writer: the stream says Flate then DCT but holds the JPEG as is.
    image[NameObject("/Filter")] = ArrayObject(
        [NameObject("/FlateDecode"), NameObject("/DCTDecode")]
    )


@pytest.mark.parametrize("relabel", [_crypt_identity, _flate_label],
                         ids=["identity-crypt", "flate-label-on-raw-jpeg"])
def test_compress_chain_that_fails_to_decode_falls_back_to_stored_jpeg(tmp_path, relabel):
    from pypdf.filters import decode_stream_data

    jpeg = _noisy_jpeg()
    src = _make_jpeg_pdf(tmp_path / "plain.pdf", jpeg)

    def edit(writer, image):
        image._data = jpeg
        relabel(image)

    edited = _edit_images(src, tmp_path / "chain.pdf", edit)
    (stored,) = _images(edited)
    try:
        decoded = decode_stream_data(stored)
    except Exception:
        decoded = None
    assert not (decoded or b"").startswith(b"\xff\xd8")   # the chain gives no JPEG
    out = tmp_path / "out.pdf"

    stats = compress_pdf(str(edited), str(out), image_quality=50, image_max_dpi=100)

    assert stats["images_recompressed"] == 1
    (image,) = _images(out)
    assert _filters(image) == ["/DCTDecode"]
    assert len(image._data) < len(jpeg)
    assert int(image["/Width"]) < 1200
