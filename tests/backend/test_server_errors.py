"""A failure the visitor can't fix answers one fixed message and leaks nothing.

Two layers are pinned here. Each tool route's own ``except Exception`` branch
answers the fixed message as JSON with 500. A failure outside any route's
``try`` (a full disk while saving an upload, say) reaches the app-wide 500
handler, which answers JSON under /api/v1/ and for every non-GET request, and
the HTML message page for a page load.

Under ``TESTING`` Flask re-raises an unhandled exception instead of calling
the 500 handler, so the tests of that handler set ``PROPAGATE_EXCEPTIONS``
to False, as production runs.
"""
import errno
import io

import pytest
from werkzeug.datastructures import FileStorage

import app as flask_app_module
import packet_helpers as ph
from routes import api, convert, errors, export, merge, pages, preview, security
from routes import print as print_routes
from utils import staging

APP = flask_app_module.app
FIXED = "Docist couldn't finish this. Try again, or try another file."
SECRET = "internal-detail-xyz"
DISK_FULL = "No space left on device"


def boom(*args, **kwargs):
    raise RuntimeError(SECRET)


def disk_full(*args, **kwargs):
    raise OSError(errno.ENOSPC, DISK_FULL, "/srv/docist/staging/secret-path")


def pdf_file(name="report.pdf", pages_=1):
    return (io.BytesIO(ph.pdf_bytes(pages_)), name)


@pytest.fixture
def api_open(client):
    prev = APP.config["API_ANONYMOUS"]
    APP.config["API_ANONYMOUS"] = True
    try:
        yield
    finally:
        APP.config["API_ANONYMOUS"] = prev


@pytest.fixture
def unpropagated(client, monkeypatch):
    """Exceptions reach the app's 500 handler, as outside tests."""
    monkeypatch.setitem(APP.config, "PROPAGATE_EXCEPTIONS", False)
    return client


def assert_fixed_json(response, *leaks):
    assert response.status_code == 500, response.data[:300]
    assert response.mimetype == "application/json", response.data[:300]
    body = response.get_json()
    assert body == {"error": FIXED, "code": "server_error"}
    for leak in (SECRET,) + leaks:
        assert leak.encode() not in response.data


# --------------------------------------------------------------------------
# Each route's own generic handler
# --------------------------------------------------------------------------
# (module whose call is patched, the call, method path, form data)
ROUTE_CASES = [
    pytest.param(merge, "run_in_job", "/upload",
                 lambda: {"files[]": [pdf_file("a.pdf"), pdf_file("b.pdf")]}, id="merge"),
    pytest.param(convert, "run_in_job", "/convert/run",
                 lambda: {"file": pdf_file(), "target": ".png"}, id="convert"),
    pytest.param(export, "run_in_job", "/export/run",
                 lambda: {"file": pdf_file(), "operation": "text"}, id="export"),
    pytest.param(print_routes, "run_in_job", "/print/run",
                 lambda: {"file": pdf_file(), "operation": "booklet"}, id="print"),
    pytest.param(pages, "run_in_job", "/pages/run",
                 lambda: {"file": pdf_file(pages_=2), "operation": "extract", "ranges": "1"},
                 id="pages"),
    # The job's own failure is the "could not read" 400; the generic branch
    # catches anything around it, here the upload check.
    pytest.param(preview, "validate_upload", "/preview/thumbs",
                 lambda: {"file": pdf_file()}, id="preview"),
    pytest.param(security, "run_in_job", "/security/run",
                 lambda: {"file": pdf_file(), "operation": "watermark", "text": "DRAFT"},
                 id="security"),
    pytest.param(api, "run_in_job", "/api/v1/merge",
                 lambda: {"files[]": [pdf_file("a.pdf"), pdf_file("b.pdf")]}, id="api-merge"),
    pytest.param(api, "run_in_job", "/api/v1/convert",
                 lambda: {"file": pdf_file(), "target": ".png"}, id="api-convert"),
    pytest.param(api, "run_in_job", "/api/v1/watermark",
                 lambda: {"file": pdf_file(), "text": "DRAFT"}, id="api-watermark"),
]


@pytest.mark.parametrize("module, call, path, data", ROUTE_CASES)
def test_a_route_failure_answers_the_fixed_message(client, api_open, monkeypatch, caplog,
                                                   module, call, path, data):
    monkeypatch.setattr(module, call, boom)
    response = client.post(path, data=data(), content_type="multipart/form-data")
    assert_fixed_json(response)
    # The exception goes to the log instead.
    assert SECRET in caplog.text
    assert list(client.output_dir.iterdir()) == []


# --------------------------------------------------------------------------
# The app-wide handler: failures outside any route's try
# --------------------------------------------------------------------------
def test_a_disk_failure_staging_a_packet_file_answers_json_with_a_code(
        unpropagated, monkeypatch, caplog):
    """POST /packet/stage with the staging store's add failing on a full
    disk: the fixed message as JSON with ``code: server_error``, not Flask's
    HTML 500 with no code."""
    monkeypatch.setattr(staging.Store, "add", disk_full)
    response = ph.stage(unpropagated, ph.pdf_bytes(1))
    assert_fixed_json(response, DISK_FULL, "/srv/docist")
    assert DISK_FULL in caplog.text


def test_a_disk_failure_saving_a_merge_upload_answers_json(unpropagated, monkeypatch, caplog):
    """The same on a tool route: /upload saves each upload before its try,
    so a full disk there reaches the app-wide handler."""
    monkeypatch.setattr(FileStorage, "save", disk_full)
    response = unpropagated.post("/upload", data={"files[]": [pdf_file()]},
                                 content_type="multipart/form-data")
    assert_fixed_json(response, DISK_FULL, "/srv/docist")
    assert DISK_FULL in caplog.text


def test_a_failure_on_an_api_get_answers_json(unpropagated, monkeypatch):
    monkeypatch.setattr(api, "supported_sources", disk_full)
    response = unpropagated.get("/api/v1/formats")
    assert_fixed_json(response, DISK_FULL)


def test_a_failure_loading_a_page_answers_the_html_error_page(unpropagated, monkeypatch, caplog):
    monkeypatch.setattr(merge, "render_template", disk_full)
    response = unpropagated.get("/")
    assert response.status_code == 500
    assert response.mimetype == "text/html"
    text = response.get_data(as_text=True)
    assert "Docist couldn&#39;t finish this. Try again, or try another file." in text
    assert "<nav" in text  # the site's own message page, not Flask's
    assert DISK_FULL not in text and "/srv/docist" not in text
    assert DISK_FULL in caplog.text


def test_the_error_page_falls_back_to_plain_html(unpropagated, monkeypatch):
    """When the message page itself can't render, a plain page still says
    the fixed message."""
    monkeypatch.setattr(merge, "render_template", disk_full)
    monkeypatch.setattr(errors, "render_template", boom)
    response = unpropagated.get("/")
    assert response.status_code == 500
    assert response.mimetype == "text/html"
    text = response.get_data(as_text=True)
    assert "Docist couldn&#39;t finish this." in text
    assert SECRET not in text and DISK_FULL not in text


def test_other_http_errors_keep_their_status(unpropagated):
    assert unpropagated.get("/no-such-page").status_code == 404
    assert unpropagated.get("/api/v1/merge").status_code == 405
