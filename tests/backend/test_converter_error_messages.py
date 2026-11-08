"""A corrupt upload's error names the upload and carries no server detail.

For each converter family (docx, image, svg, spreadsheet, markdown) a
damaged upload goes through the convert routes (page and API) and the merge
routes (page and API). Each answers 400 with the upload's name, and the
message holds neither the request's temporary directory, nor "original
error", nor the library's own text; that text goes to the log at warning
level instead. A converter that fails with a library exception of its own
(not a ConversionError) gives merge's fixed sentence.
"""
import io
import logging
import os
import tempfile
import zipfile

import pytest

import converters
from app import app


def _zip(parts):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return buf.getvalue()


# name -> (bytes, a stable piece of the library's text for that damage)
CORRUPT = {
    'docx': (
        'broken.docx',
        _zip({'[Content_Types].xml': '<Types/>', 'word/document.xml': '<notxml'}),
        'unclosed token',
    ),
    'image': (
        'broken.png',
        b'\x89PNG\r\n\x1a\n' + b'garbage' * 20,
        'Truncated File Read',
    ),
    'svg': (
        'broken.svg',
        b"<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'>"
        b"<rect width='5' height='x'/></svg",
        "Can't convert",
    ),
    'spreadsheet': (
        'broken.xlsx',
        _zip({'[Content_Types].xml':
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
              'content-types"/>',
              'xl/workbook.xml': '<x/>'}),
        'no valid workbook part',
    ),
    # Markdown text always decodes and parses; inline HTML that the PDF
    # renderer can't lay out is what breaks it.
    'markdown': (
        'broken.md',
        b'# Title\n\n<table><tr><td colspan="-5">x</td></tr></table>\n',
        'spanning problem',
    ),
}

ROUTES = [
    ('/convert/run', 'file', {'target': '.pdf'}),
    ('/api/v1/convert', 'file', {'target': '.pdf'}),
    ('/upload', 'files[]', {}),
    ('/api/v1/merge', 'files[]', {}),
]


@pytest.fixture
def api(client):
    prev = app.config['API_ANONYMOUS']
    app.config['API_ANONYMOUS'] = True
    try:
        yield client
    finally:
        app.config['API_ANONYMOUS'] = prev


@pytest.fixture
def system_tmp(tmp_path, monkeypatch):
    """Point the request's temporary directories somewhere the test knows."""
    path = tmp_path / 'system-tmp'
    path.mkdir()
    monkeypatch.setenv('TMPDIR', str(path))
    monkeypatch.setattr(tempfile, 'tempdir', str(path))
    return path


def _post(client, path, field, name, data, fields):
    form = dict(fields)
    form[field] = (io.BytesIO(data), name)
    return client.post(path, data=form, content_type='multipart/form-data')


def _assert_clean(message, name, marker, system_tmp):
    assert name in message, message
    assert str(system_tmp) not in message
    assert 'system-tmp' not in message
    assert tempfile.gettempdir() not in message
    assert 'original error' not in message.lower()
    assert marker not in message
    assert os.sep + 'tmp' not in message


@pytest.mark.parametrize('family', sorted(CORRUPT))
def test_library_text_goes_to_the_log(family, tmp_path, caplog):
    """The converter's error names the file and drops the library's text,
    which is logged at warning level; the marker really is that text."""
    name, data, marker = CORRUPT[family]
    src = tmp_path / name
    src.write_bytes(data)
    convert = converters.get_converter(os.path.splitext(name)[1])
    with caplog.at_level(logging.WARNING, logger='converters'):
        with pytest.raises(converters.ConversionError) as info:
            convert(str(src), str(tmp_path / 'out.pdf'))
    assert marker in str(info.value.__cause__)        # the library said it
    _assert_clean(str(info.value), name, marker, tmp_path)
    logged = [r for r in caplog.records
              if r.name == 'converters' and r.levelno == logging.WARNING]
    assert any(marker in r.getMessage() for r in logged), caplog.text


@pytest.mark.parametrize('route,field,fields', ROUTES, ids=[r[0] for r in ROUTES])
@pytest.mark.parametrize('family', sorted(CORRUPT))
def test_corrupt_upload_answers_400_with_its_name(api, system_tmp, family, route,
                                                   field, fields):
    name, data, marker = CORRUPT[family]
    response = _post(api, route, field, name, data, fields)
    assert response.status_code == 400, response.get_data()[:300]
    body = response.get_json()
    assert body is not None, response.get_data()[:300]
    _assert_clean(body['error'], name, marker, system_tmp)
    assert marker not in response.get_data(as_text=True)


@pytest.mark.parametrize('route', ['/upload', '/api/v1/merge'])
def test_merge_hides_a_library_exception_from_a_converter(api, system_tmp,
                                                          monkeypatch, route):
    """A converter that lets a library exception out (not a ConversionError):
    merge's SourceError names the file with its fixed sentence."""
    def explode(input_path, output_path, opts=None):
        raise RuntimeError(f'internal-detail-xyz reading {input_path}')
    monkeypatch.setitem(converters._REGISTRY, '.md', explode)

    response = _post(api, route, 'files[]', 'notes.md', b'# hi\n', {})
    assert response.status_code == 400, response.get_data()[:300]
    message = response.get_json()['error']
    assert message == ('Could not convert notes.md. It may be damaged, or not '
                       'the kind of file its name says.')
    assert 'internal-detail-xyz' not in response.get_data(as_text=True)
    assert str(system_tmp) not in response.get_data(as_text=True)


def test_merge_names_the_file_when_the_converter_message_does_not(monkeypatch, tmp_path):
    """A converter's own sentence that doesn't name the file (a ruled limit
    message, say) gets the name in front of it."""
    from pdf_ops import merge

    def refuse(input_path, output_path, opts=None):
        raise converters.ConversionError('The file is too big.')
    monkeypatch.setitem(converters._REGISTRY, '.md', refuse)
    src = tmp_path / 'notes.md'
    src.write_text('# hi\n')
    with pytest.raises(merge.SourceError) as info:
        merge.convert_and_merge([(str(src), '.md', 'notes', 'notes.md')],
                                merge.parse_options({}), None, str(tmp_path / 'out.pdf'))
    assert str(info.value) == 'Could not convert notes.md: The file is too big.'
