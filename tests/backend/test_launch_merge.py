"""Merge (design launch-hardening v0.4 §13, critic majors 2 and 3)."""
import io

import pytest
from pypdf import PdfReader, PdfWriter

import app as flask_app_module

app = flask_app_module.app


@pytest.fixture
def api(client):
    prev = app.config['API_ANONYMOUS']
    app.config['API_ANONYMOUS'] = True
    try:
        yield client
    finally:
        app.config['API_ANONYMOUS'] = prev


def pdf_of_width(width):
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=300)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def merged_pdf(client, response):
    assert response.status_code == 200, response.get_data()[:300]
    if response.is_json:
        name = response.get_json()['filename']
        return PdfReader(str(client.output_dir / name))
    return PdfReader(io.BytesIO(response.get_data()))


@pytest.mark.parametrize('path', ['/upload', '/api/v1/merge'])
def test_same_named_uploads_both_survive(api, path):
    response = api.post(path, data={'files[]': [
        (io.BytesIO(pdf_of_width(100)), 'same.pdf'),
        (io.BytesIO(pdf_of_width(200)), 'same.pdf'),
    ], 'page_numbers': 'false'}, content_type='multipart/form-data')
    reader = merged_pdf(api, response)
    assert [float(p.mediabox.width) for p in reader.pages] == [100.0, 200.0]


@pytest.mark.parametrize('path', ['/upload', '/api/v1/merge'])
def test_an_unsupported_file_refuses_the_whole_request(api, path):
    response = api.post(path, data={'files[]': [
        (io.BytesIO(pdf_of_width(100)), 'kept.pdf'),
        (io.BytesIO(b'whatever'), 'dropped.xyz'),
        (io.BytesIO(b'more'), 'Notes 2.abc'),
    ]}, content_type='multipart/form-data')
    assert response.status_code == 400
    assert response.get_json()['error'] == (
        "Docist can't merge dropped.xyz, Notes 2.abc. Remove them and try again.")
    assert list(api.output_dir.iterdir()) == []


@pytest.mark.parametrize('path', ['/upload', '/api/v1/merge'])
def test_a_non_ascii_pdf_name_merges(api, path):
    response = api.post(path, data={'files[]': [
        (io.BytesIO(pdf_of_width(150)), 'отчёт.pdf'),
        (io.BytesIO(pdf_of_width(250)), 'zweite.pdf'),
    ], 'page_numbers': 'false'}, content_type='multipart/form-data')
    reader = merged_pdf(api, response)
    assert [float(p.mediabox.width) for p in reader.pages] == [150.0, 250.0]


def test_a_non_ascii_name_keeps_its_bookmark_title(api):
    response = api.post('/api/v1/merge', data={'files[]': [
        (io.BytesIO(pdf_of_width(150)), 'отчёт.pdf'),
    ], 'bookmarks': 'true'}, content_type='multipart/form-data')
    reader = merged_pdf(api, response)
    assert [item.title for item in reader.outline] == ['отчёт']


def test_an_empty_file_part_is_still_ignored(api):
    response = api.post('/upload', data={'files[]': [
        (io.BytesIO(pdf_of_width(100)), 'one.pdf'),
        (io.BytesIO(b''), ''),
    ]}, content_type='multipart/form-data')
    assert response.status_code == 200
