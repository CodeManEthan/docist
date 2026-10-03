"""Launch-hardening limits through the routes (design v0.4 §3.5, §5 to §11, §15).

Limits are patched low in ``pdf_ops.limits``, the one place each is read, and
fixtures are small. Nothing here builds a full-size bomb.
"""
import base64
import gc
import io
import os
import tempfile
import time
import weakref

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

import app as flask_app_module
import converters
from converters import ConversionError, image_converter
from models import ApiKey, User, db
from pdf_ops import jobs, limits
from routes import api as api_routes

app = flask_app_module.app
MIB = 1024 * 1024


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
@pytest.fixture
def api(client):
    prev = app.config['API_ANONYMOUS']
    app.config['API_ANONYMOUS'] = True
    try:
        yield client
    finally:
        app.config['API_ANONYMOUS'] = prev


def post(client, path, files, **fields):
    data = dict(fields)
    for name, value in files.items():
        if isinstance(value, list):
            data[name] = [(io.BytesIO(b), n) for b, n in value]
        else:
            b, n = value
            data[name] = (io.BytesIO(b), n)
    return client.post(path, data=data, content_type='multipart/form-data')


def pdf_bytes(pages=1, size=(612, 792)):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=size[0], height=size[1])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def text_pdf_bytes(builders, tmp_path, pages=1):
    return builders.pdf(tmp_path / f'text{pages}.pdf', pages=pages).read_bytes()


def png_bytes(size=(64, 48), mode='RGB', color=(10, 120, 200)):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, 'PNG')
    return buf.getvalue()


def gif_bytes(frames=5, size=(80, 60)):
    images = [Image.new('RGB', size, (i * 40 % 255, 90, 30)) for i in range(frames)]
    buf = io.BytesIO()
    images[0].save(buf, 'GIF', save_all=True, append_images=images[1:])
    return buf.getvalue()


def heic_two(path, first=(64, 48), second=(400, 300)):
    import pillow_heif
    pillow_heif.register_heif_opener()
    a = Image.new('RGB', first, (200, 0, 0))
    b = Image.new('RGB', second, (0, 200, 0))
    a.save(path, save_all=True, append_images=[b])
    return path


def error(response):
    body = response.get_json()
    assert body is not None, response.get_data()[:200]
    return body['error']


def outputs(client):
    return list(client.output_dir.iterdir())


# --------------------------------------------------------------------------
# §3.5, §15: every route answers a limit with a 400 and its message
# --------------------------------------------------------------------------
def _route_cases(builders, tmp_path):
    pdf = text_pdf_bytes(builders, tmp_path, pages=2)
    md = b'# Title\n\nSome text.\n'
    return [
        ('/upload', {'files[]': [(pdf, 'a.pdf'), (md, 'b.md')]}, {}),
        ('/convert/run', {'file': (md, 'a.md')}, {'target': '.pdf'}),
        ('/pages/run', {'file': (pdf, 'a.pdf')}, {'operation': 'extract', 'ranges': '1'}),
        ('/export/run', {'file': (pdf, 'a.pdf')}, {'operation': 'text'}),
        ('/print/run', {'file': (pdf, 'a.pdf')}, {'operation': 'nup', 'n': '2'}),
        ('/security/run', {'file': (pdf, 'a.pdf')}, {'operation': 'watermark', 'text': 'X'}),
        ('/preview/thumbs', {'file': (pdf, 'a.pdf')}, {}),
        ('/api/v1/merge', {'files[]': [(pdf, 'a.pdf'), (md, 'b.md')]}, {}),
        ('/api/v1/convert', {'file': (md, 'a.md')}, {'target': '.pdf'}),
        ('/api/v1/pages/extract', {'file': (pdf, 'a.pdf')}, {'ranges': '1'}),
        ('/api/v1/pages/split', {'file': (pdf, 'a.pdf')}, {'mode': 'every_n', 'value': '1'}),
        ('/api/v1/watermark', {'file': (pdf, 'a.pdf')}, {'text': 'X'}),
    ]


def test_every_route_answers_the_time_limit_with_400(api, builders, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config, 'RENDER_BUDGET', 0)   # the deadline has passed
    for path, files, fields in _route_cases(builders, tmp_path):
        response = post(api, path, files, **fields)
        assert response.status_code == 400, (path, response.get_data()[:300])
        assert error(response) == limits.time_message(0), path
    assert outputs(api) == []


def test_every_route_answers_the_memory_limit_with_400(api, builders, tmp_path, monkeypatch):
    """A MemoryError wrapped the way the converters wrap it, raised inside
    each route's job. Merge and Convert caught Exception and answered 500."""
    import pypdf
    from converters import markdown_converter

    def wrapped_memory_error(*_a, **_k):
        try:
            raise MemoryError()
        except MemoryError as exc:
            raise ConversionError('Failed to render PDF from Markdown: ') from exc

    def reader_out_of_memory(*_a, **_k):
        raise MemoryError()

    monkeypatch.setattr(markdown_converter, 'convert', wrapped_memory_error)
    monkeypatch.setitem(converters._REGISTRY, '.md', wrapped_memory_error)
    from transforms import pdf_bridge
    monkeypatch.setattr(pdf_bridge.converters, 'get_converter',
                        lambda ext: wrapped_memory_error)
    for module in ('routes.pages', 'routes.export', 'routes.print', 'routes.api',
                   'pdf_ops.pages', 'pdf_ops.watermark', 'pdf_ops.preview'):
        mod = __import__(module, fromlist=['x'])
        if hasattr(mod, 'PdfReader'):
            monkeypatch.setattr(mod, 'PdfReader', reader_out_of_memory)
    import pypdfium2
    monkeypatch.setattr(pypdfium2, 'PdfDocument', reader_out_of_memory)
    monkeypatch.setattr(pypdf, 'PdfReader', reader_out_of_memory)
    for path, files, fields in _route_cases(builders, tmp_path):
        response = post(api, path, files, **fields)
        assert response.status_code == 400, (path, response.get_data()[:300])
        assert error(response) == limits.memory_message(), path
    assert outputs(api) == []


def test_a_limit_error_reaching_the_app_handler_is_a_json_400(client):
    with app.test_request_context('/anything'):
        response = app.make_response(
            app.handle_user_exception(limits.LimitError('over the test limit')))
    assert response.status_code == 400
    assert response.get_json() == {'error': 'over the test limit'}


# --------------------------------------------------------------------------
# §5 YAML
# --------------------------------------------------------------------------
CRITIC_YAML = (
    'a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]\n'
    'b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\n'
    'c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]\n'
    'd: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c]\n'
    'e: &e [*d,*d,*d,*d,*d,*d,*d,*d,*d]\n'
    'f: &f [*e,*e,*e,*e,*e,*e,*e,*e,*e]\n'
    'g: &g [*f,*f,*f,*f,*f,*f,*f,*f,*f]\n'
    'h: &h [*g,*g,*g,*g,*g,*g,*g,*g,*g]\n'
    'i: &i [*h,*h,*h,*h,*h,*h,*h,*h,*h]\n'
).encode()


def test_yaml_the_critics_alias_file_is_refused_at_the_file_cap(api, monkeypatch):
    monkeypatch.setattr(limits, 'RESULT_BYTES', 1 * MIB)
    assert len(CRITIC_YAML) < 400
    for path in ('/convert/run', '/api/v1/convert'):
        started = time.monotonic()
        response = post(api, path, {'file': (CRITIC_YAML, 'lol.yaml')}, target='.json')
        assert response.status_code == 400, path
        assert error(response) == limits.file_message(), path
        assert time.monotonic() - started < 20
    assert outputs(api) == []


def test_yaml_ordinary_anchors_convert(client):
    src = b'base: &base {x: 1, y: 2}\none: *base\ntwo: {<<: *base, y: 3}\n'
    response = post(client, '/convert/run', {'file': (src, 'a.yaml')}, target='.json')
    assert response.status_code == 200, error(response)


@pytest.mark.parametrize('src', [
    b'a: &a [*a]\n',
    b'[' * 3000 + b']' * 3000 + b'\n',
])
def test_yaml_a_recursive_alias_or_deep_nesting_is_a_400(client, src):
    response = post(client, '/convert/run', {'file': (src, 'a.yaml')}, target='.json')
    assert response.status_code == 400
    assert error(response) == limits.YAML_MESSAGE


# --------------------------------------------------------------------------
# §6 rasters
# --------------------------------------------------------------------------
def test_a_frame_over_the_limit_is_refused_on_every_image_path(api, tmp_path, monkeypatch):
    from pdf_ops import ocr as ocr_ops
    from transforms import ocr_text
    import pytesseract
    monkeypatch.setattr(limits, 'FRAME_PIXELS', 1000)
    monkeypatch.setattr(ocr_text, 'is_available', lambda: True)
    monkeypatch.setattr(ocr_ops, 'is_available', lambda: True)
    monkeypatch.setattr(ocr_ops, 'installed_languages', lambda: ['eng'])
    monkeypatch.setattr(pytesseract, 'image_to_string', lambda *a, **k: 'text')
    big = png_bytes((64, 48))          # 3,072 pixels
    gif = gif_bytes(3, (64, 48))
    cases = [
        ('/convert/run', {'file': (big, 'a.png')}, {'target': '.pdf'}),
        ('/convert/run', {'file': (big, 'a.png')}, {'target': '.jpg'}),
        ('/convert/run', {'file': (big, 'a.png')}, {'target': '.txt'}),
        ('/convert/run', {'file': (gif, 'a.gif')}, {'target': '.pdf'}),
        ('/convert/run', {'file': (gif, 'a.gif')}, {'target': '.txt'}),
        ('/upload', {'files[]': [(big, 'a.png')]}, {}),
        ('/api/v1/convert', {'file': (big, 'a.png')}, {'target': '.webp'}),
        ('/api/v1/merge', {'files[]': [(gif, 'a.gif')]}, {}),
    ]
    for path, files, fields in cases:
        response = post(api, path, files, **fields)
        assert response.status_code == 400, (path, fields)
        assert error(response) == limits.image_message(), (path, fields)
    assert 'over 0.001 megapixels' in limits.image_message()
    assert outputs(api) == []


def test_the_second_image_of_a_heic_is_checked_after_its_seek(api, tmp_path, monkeypatch):
    from pdf_ops import ocr as ocr_ops
    from transforms import ocr_text
    import pytesseract
    src = heic_two(tmp_path / 'two.heic').read_bytes()
    monkeypatch.setattr(limits, 'FRAME_PIXELS', 64 * 48)   # the first fits, the second doesn't
    monkeypatch.setattr(ocr_text, 'is_available', lambda: True)
    monkeypatch.setattr(ocr_ops, 'is_available', lambda: True)
    monkeypatch.setattr(ocr_ops, 'installed_languages', lambda: ['eng'])
    monkeypatch.setattr(pytesseract, 'image_to_string', lambda *a, **k: 'text')
    for target in ('.pdf', '.txt'):
        response = post(api, '/convert/run', {'file': (src, 'two.heic')}, target=target)
        assert response.status_code == 400, target
        assert error(response) == limits.image_message(), target
    # With room for both, both images convert.
    monkeypatch.setattr(limits, 'FRAME_PIXELS', 400 * 300)
    response = post(api, '/api/v1/convert', {'file': (src, 'two.heic')}, target='.pdf')
    assert response.status_code == 200
    assert len(PdfReader(io.BytesIO(response.get_data())).pages) == 2


def test_a_multi_frame_gif_keeps_one_composed_page_alive(tmp_path, monkeypatch):
    src = tmp_path / 'many.gif'
    src.write_bytes(gif_bytes(12, (300, 200)))
    pages = []
    most = [0]
    real = image_converter._compose_page

    def counting(frame, page_w, page_h):
        gc.collect()
        page = real(frame, page_w, page_h)
        pages.append(weakref.ref(page))
        most[0] = max(most[0], sum(1 for ref in pages if ref() is not None))
        return page
    monkeypatch.setattr(image_converter, '_compose_page', counting)
    out = tmp_path / 'out.pdf'
    image_converter.convert(str(src), str(out))
    assert most[0] == 1
    assert len(PdfReader(str(out)).pages) == 12


def test_transparency_is_composited_onto_white(tmp_path):
    src = tmp_path / 'half.png'
    Image.new('RGBA', (100, 80), (255, 0, 0, 128)).save(src)
    out = tmp_path / 'out.pdf'
    image_converter.convert(str(src), str(out))
    page = image_converter._compose_page(Image.open(src), 300, 300)
    r, g, b = page.getpixel((150, 150))
    assert r == 255 and 120 <= g <= 135 and 120 <= b <= 135
    from transforms.ocr_text import _flatten_to_white
    flat = _flatten_to_white(Image.open(src))
    r, g, b = flat.getpixel((5, 5))
    assert r == 255 and 120 <= g <= 135 and 120 <= b <= 135


def test_optimize_leaves_an_image_over_the_limit_alone(tmp_path, monkeypatch):
    from pdf_ops.optimize import compress_pdf
    img = tmp_path / 'img.pdf'
    Image.new('RGB', (400, 300), (30, 60, 90)).save(img, 'PDF', resolution=72.0)
    monkeypatch.setattr(limits, 'FRAME_PIXELS', 1000)
    stats = compress_pdf(str(img), str(tmp_path / 'out.pdf'), image_quality=30, image_max_dpi=72)
    assert stats['images_recompressed'] == 0


# --------------------------------------------------------------------------
# §7 PDF rendering
# --------------------------------------------------------------------------
TALL = (30, 300_000)   # points: 120 x 1,200,000 pixels at thumbnail scale 4


def test_export_refuses_a_tall_page_with_the_dpi_message(client):
    src = pdf_bytes(1, TALL)
    response = post(client, '/export/run', {'file': (src, 'tall.pdf')},
                    operation='images', fmt='png', dpi='150')
    assert response.status_code == 400
    assert error(response) == limits.page_dpi_message(1, 150)
    assert 'Choose a lower DPI' in error(response)
    assert outputs(client) == []


@pytest.mark.parametrize('path', ['/convert/run', '/api/v1/convert'])
@pytest.mark.parametrize('target', ['.png', '.jpg'])
def test_convert_pdf_to_image_refuses_a_huge_page_without_dpi(api, path, target):
    src = pdf_bytes(1, (14400, 14400))     # 30,000 x 30,000 px at Convert's fixed 150 DPI
    response = post(api, path, {'file': (src, 'huge.pdf')}, target=target)
    assert response.status_code == 400
    assert error(response) == limits.page_fixed_message(1)
    assert error(response) == ('Page 1 is too large to render. Docist can render pages '
                               'up to 36 megapixels.')
    assert outputs(api) == []


def test_ocr_fallback_refuses_a_tall_page_without_dpi(client, monkeypatch):
    from pdf_ops import export as export_ops
    from routes import export as export_routes
    import pytesseract
    monkeypatch.setattr(export_ops, '_ocr_is_available', lambda: True)
    monkeypatch.setattr(export_routes, 'ocr_is_available', lambda: True)
    monkeypatch.setattr(export_ops, 'validate_language', lambda lang: 'eng')
    from utils import render_opts
    monkeypatch.setattr(render_opts, 'parse_language', lambda raw: 'eng')
    monkeypatch.setattr(pytesseract, 'image_to_string', lambda *a, **k: 'x')
    src = pdf_bytes(1, TALL)
    response = post(client, '/export/run', {'file': (src, 'tall.pdf')},
                    operation='text', ocr_fallback='1')
    assert response.status_code == 400
    assert error(response) == limits.page_fixed_message(1)
    assert 'DPI' not in error(response)


def test_thumbnails_put_a_placeholder_in_an_over_limit_pages_slot(client, monkeypatch):
    from pdf_ops import preview
    monkeypatch.setattr(limits, 'FRAME_PIXELS', 120 * 200)   # a Letter thumb is 120 x 156
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=2400)              # 120 x 471 at thumbnail scale
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    response = post(client, '/preview/thumbs', {'file': (buf.getvalue(), 'mixed.pdf')})
    assert response.status_code == 200
    body = response.get_json()
    assert body['pages'] == 3
    assert len(body['thumbs']) == 3
    assert body['thumbs'][1] == preview._placeholder(preview.TARGET_WIDTH)
    assert body['thumbs'][0] != body['thumbs'][1] != body['thumbs'][2]
    tile = Image.open(io.BytesIO(base64.b64decode(body['thumbs'][1].split(',', 1)[1])))
    assert tile.size == (120, 155)


def test_thumbnails_of_a_huge_page_at_the_real_limit(client):
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=TALL[0], height=TALL[1])
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    response = post(client, '/preview/thumbs', {'file': (buf.getvalue(), 'mixed.pdf')})
    assert response.status_code == 200
    body = response.get_json()
    from pdf_ops import preview
    assert body['pages'] == 3 and len(body['thumbs']) == 3
    assert body['thumbs'][1] == preview._placeholder(preview.TARGET_WIDTH)
    assert body['thumbs'][0] != body['thumbs'][1]


def test_letter_at_600_dpi_still_exports(client):
    src = pdf_bytes(1, (612, 792))
    assert limits.frame_ok(5100, 6600)
    response = post(client, '/export/run', {'file': (src, 'letter.pdf')},
                    operation='images', fmt='jpg', dpi='600')
    assert response.status_code == 200, error(response)


def test_export_counts_bytes_across_pages(client, builders, tmp_path, monkeypatch):
    monkeypatch.setattr(limits, 'RESULT_BYTES', 20_000)
    src = text_pdf_bytes(builders, tmp_path, pages=6)
    response = post(client, '/export/run', {'file': (src, 'six.pdf')},
                    operation='images', fmt='png', dpi='72')
    assert response.status_code == 400
    assert error(response) == limits.file_message()
    assert outputs(client) == []


# --------------------------------------------------------------------------
# §8 split
# --------------------------------------------------------------------------
def test_split_parts_are_counted_before_any_range_is_parsed(api, builders, tmp_path,
                                                           monkeypatch):
    monkeypatch.setattr(limits, 'SPLIT_PARTS', 3)
    src = text_pdf_bytes(builders, tmp_path, pages=10)
    ranges = '1;2;nonsense;4'
    browser = post(api, '/pages/run', {'file': (src, 'a.pdf')},
                   operation='split', split_mode='ranges', split_value=ranges)
    rest = post(api, '/api/v1/pages/split', {'file': (src, 'a.pdf')},
                mode='ranges', value=ranges)
    for response in (browser, rest):
        assert response.status_code == 400
        assert error(response) == 'A split can make at most 3 files.'
    browser = post(api, '/pages/run', {'file': (src, 'a.pdf')},
                   operation='split', split_mode='every_n', split_value='3')   # 4 parts
    rest = post(api, '/api/v1/pages/split', {'file': (src, 'a.pdf')},
                mode='every_n', value='3')
    for response in (browser, rest):
        assert response.status_code == 400
        assert error(response) == 'A split can make at most 3 files.'
    ok = post(api, '/api/v1/pages/split', {'file': (src, 'a.pdf')}, mode='every_n', value='4')
    assert ok.status_code == 200
    assert outputs(api) == []


def test_split_bytes_are_counted_across_parts(api, builders, tmp_path, monkeypatch):
    src = text_pdf_bytes(builders, tmp_path, pages=1)
    monkeypatch.setattr(limits, 'RESULT_BYTES', 5 * len(src))
    ranges = ';'.join(['1'] * 20)      # duplicates stay allowed
    browser = post(api, '/pages/run', {'file': (src, 'a.pdf')},
                   operation='split', split_mode='ranges', split_value=ranges)
    rest = post(api, '/api/v1/pages/split', {'file': (src, 'a.pdf')},
                mode='ranges', value=ranges)
    for response in (browser, rest):
        assert response.status_code == 400
        assert error(response) == limits.file_message()
    assert outputs(api) == []
    ok = post(api, '/pages/run', {'file': (src, 'a.pdf')},
              operation='split', split_mode='ranges', split_value='1;1')
    assert ok.status_code == 200


# --------------------------------------------------------------------------
# §9 SVG temp files
# --------------------------------------------------------------------------
_PNG_URI = 'data:image/png;base64,' + base64.b64encode(png_bytes((8, 8))).decode()
SVG = (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
       f'width="100" height="80"><rect width="100" height="80" fill="#08c"/>'
       f'<image x="10" y="10" width="8" height="8" xlink:href="{_PNG_URI}"/></svg>').encode()


@pytest.fixture
def system_tmp(tmp_path, monkeypatch):
    path = tmp_path / 'system-tmp'
    path.mkdir()
    monkeypatch.setenv('TMPDIR', str(path))
    monkeypatch.setattr(tempfile, 'tempdir', str(path))
    return path


def _svg_job(tmp_path, made, deadline=30):
    src = tmp_path / 'pic.svg'
    src.write_bytes(SVG)
    out = tmp_path / 'pic.pdf'
    real_mkstemp = tempfile.mkstemp

    def recording(*a, **k):
        fd, name = real_mkstemp(*a, **k)
        made.append(name)
        return fd, name

    def work():
        tempfile.mkstemp = recording     # in the child only
        converters.get_converter('.svg')(str(src), str(out))
        return str(out)

    return jobs.run_job(work, deadline=time.monotonic() + deadline,
                        job_dir=str(tmp_path / 'job'))


def test_svg_temp_files_go_with_the_job_on_success(tmp_path, system_tmp, shared_list):
    made = shared_list()
    out = _svg_job(tmp_path, made)
    assert os.path.getsize(out) > 0
    assert len(made) >= 1                              # svglib wrote its PNG
    assert all(name.startswith(str(tmp_path / 'job')) for name in made)
    assert not (tmp_path / 'job').exists()
    assert list(system_tmp.iterdir()) == []


def test_svg_temp_files_go_when_rendering_raises(tmp_path, system_tmp, shared_list,
                                                 monkeypatch):
    from reportlab.graphics import renderPDF
    monkeypatch.setattr(renderPDF, 'draw', lambda *a, **k: 1 / 0)
    made = shared_list()
    with pytest.raises(ConversionError):
        _svg_job(tmp_path, made)
    assert len(made) >= 1
    assert not (tmp_path / 'job').exists()
    assert list(system_tmp.iterdir()) == []


def test_svg_temp_files_go_when_the_job_is_killed(tmp_path, system_tmp, shared_list,
                                                  monkeypatch):
    from reportlab.graphics import renderPDF
    monkeypatch.setattr(renderPDF, 'draw', lambda *a, **k: time.sleep(600))
    made = shared_list()
    with pytest.raises(limits.LimitError):
        _svg_job(tmp_path, made, deadline=1.5)
    assert len(made) >= 1
    assert not (tmp_path / 'job').exists()
    assert list(system_tmp.iterdir()) == []


# --------------------------------------------------------------------------
# §10 results
# --------------------------------------------------------------------------
def test_refused_export_split_and_merge_leave_nothing_in_output(client, builders, tmp_path,
                                                                monkeypatch):
    monkeypatch.setattr(limits, 'FRAME_PIXELS', 1000)
    monkeypatch.setattr(limits, 'SPLIT_PARTS', 1)
    pdf = text_pdf_bytes(builders, tmp_path, pages=2)
    cases = [
        ('/export/run', {'file': (pdf, 'a.pdf')}, {'operation': 'images'}),
        ('/pages/run', {'file': (pdf, 'a.pdf')},
         {'operation': 'split', 'split_mode': 'every_n', 'split_value': '1'}),
        ('/upload', {'files[]': [(pdf, 'a.pdf'), (png_bytes(), 'b.png')]}, {}),
    ]
    for path, files, fields in cases:
        response = post(client, path, files, **fields)
        assert response.status_code == 400, path
    assert outputs(client) == []


@pytest.fixture
def sent_files(monkeypatch):
    sent = []
    real = api_routes.send_file

    def spy(path_or_file, *a, **k):
        sent.append(path_or_file)
        return real(path_or_file, *a, **k)
    monkeypatch.setattr(api_routes, 'send_file', spy)
    return sent


@pytest.mark.parametrize('path,fields', [
    ('/api/v1/pages/extract', {'ranges': '1'}),
    ('/api/v1/pages/split', {'mode': 'every_n', 'value': '1'}),
    ('/api/v1/watermark', {'text': 'DRAFT'}),
])
def test_the_three_small_api_endpoints_stream(api, builders, tmp_path, sent_files, path,
                                              fields):
    assert not hasattr(api_routes, '_send_path')
    assert not hasattr(api_routes, '_send_bytes')
    pdf = text_pdf_bytes(builders, tmp_path, pages=2)
    response = post(api, path, {'file': (pdf, 'a.pdf')}, **fields)
    assert response.status_code == 200
    assert len(sent_files) == 1
    handle = sent_files[0]
    assert not isinstance(handle, (bytes, io.BytesIO))
    assert handle.fileno() >= 0
    data = response.get_data()
    assert int(response.headers['Content-Length']) == len(data)


# --------------------------------------------------------------------------
# §11 text fields
# --------------------------------------------------------------------------
def _paid_key(make_user):
    user = make_user(email='key@x.io', plan='monthly')
    with app.app_context():
        raw, _ = ApiKey.issue(db.session.get(User, user.id), 'k')
        db.session.commit()
    return {'Authorization': f'Bearer {raw}'}


def test_a_text_field_over_the_cap_is_a_json_413(client, make_user, builders, tmp_path):
    assert limits.FORM_FIELD_BYTES == 1 * MIB
    pdf = text_pdf_bytes(builders, tmp_path)
    over = 'x' * (limits.FORM_FIELD_BYTES + 1)
    at = 'x' * 100
    # A free endpoint (watermark) and a paid one (merge, with a paid key).
    free = client.post('/security/run', data={
        'file': (io.BytesIO(pdf), 'a.pdf'), 'operation': 'watermark', 'text': over,
    }, content_type='multipart/form-data')
    assert free.status_code == 413
    assert free.get_json()['error'] == limits.FORM_MESSAGE
    paid = client.post('/api/v1/merge', data={
        'files[]': (io.BytesIO(pdf), 'a.pdf'), 'paper': over,
    }, content_type='multipart/form-data', headers=_paid_key(make_user))
    assert paid.status_code == 413
    assert paid.get_json()['error'] == limits.FORM_MESSAGE
    ok = client.post('/security/run', data={
        'file': (io.BytesIO(pdf), 'a.pdf'), 'operation': 'watermark', 'text': at,
    }, content_type='multipart/form-data')
    assert ok.status_code == 200


def test_a_text_field_one_byte_over_is_refused(client, builders, tmp_path):
    """At the real cap: Werkzeug 3.0.6 compares its parse buffer with
    max_form_memory_size, and reads 64 KiB at a time, so the cap can't be
    patched below that without refusing ordinary file parts too."""
    pdf = text_pdf_bytes(builders, tmp_path)
    cap = limits.FORM_FIELD_BYTES
    for size, status in ((cap - 64 * 1024, 200), (cap + 1, 413)):
        response = client.post('/security/run', data={
            'file': (io.BytesIO(pdf), 'a.pdf'), 'operation': 'watermark', 'text': 'x' * size,
        }, content_type='multipart/form-data')
        assert response.status_code == status, size


def test_minor_1_a_small_body_limit_still_answers_json(api, monkeypatch):
    monkeypatch.setitem(app.config, 'MAX_CONTENT_LENGTH', 100)
    response = api.post('/api/v1/convert', data={
        'file': (io.BytesIO(b'#' * 200), 'a.md'), 'target': '.pdf',
    }, content_type='multipart/form-data')
    assert response.status_code == 413
    assert response.is_json
    assert response.get_json()['code'] == 'upload_too_large'
