"""OCR, export, protect and unlock on the REST API (/api/v1).

Each endpoint takes one PDF and the form fields its tool page sends, runs the
same pdf_ops function as the browser route, and answers the finished file: a
searchable PDF, a zip of page images, the text, or the protected or unlocked
PDF. A table drives the shared checks: the happy path, a bad option (400),
auth (401 without a key, open with DOCIST_API_ANONYMOUS, a valid key works),
metering (one call costs one operation) and the fixed 500. The password cases
check that a password reaches neither the response nor the log.
"""
import io
import logging
import os
import zipfile

import pytest
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

import app as flask_app_module
from models import ApiKey, Usage, User, db
from pdf_ops import ocr as ocr_ops
from pdf_ops.security import protect_pdf
from routes import api

APP = flask_app_module.app
FIXED = "Docist couldn't finish this. Try again, or try another file."
SECRET = "internal-detail-xyz"
PASSWORD = "Zebra-Quartz-7319"

requires_ocr = pytest.mark.skipif(not ocr_ops.is_available(),
                                  reason="OCR requires Tesseract and Ghostscript")


# --------------------------------------------------------------------------
# Inputs and helpers
# --------------------------------------------------------------------------
def reader(response):
    return PdfReader(io.BytesIO(response.data))


def text_of(response):
    return "\n".join(page.extract_text() or "" for page in reader(response).pages)


def usage_rows():
    with APP.app_context():
        return {(u.subject[:2], u.count) for u in db.session.execute(db.select(Usage)).scalars()}


def post(client, path, pdf, fields, headers=None):
    data = {"file": (open(pdf, "rb"), pdf.name)}
    data.update(fields)
    return client.post(path, data=data, content_type="multipart/form-data",
                       headers=headers or {})


def _font(size):
    for root, _dirs, files in os.walk("/usr/share/fonts"):
        for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
            if name in files:
                return ImageFont.truetype(os.path.join(root, name), size)
    return ImageFont.load_default()


@pytest.fixture
def report_pdf(tmp_path, builders):
    return builders.pdf(tmp_path / "report.pdf", pages=5, marker="ReportPage")


@pytest.fixture
def locked_pdf(tmp_path, builders):
    plain = builders.pdf(tmp_path / "plain.pdf", pages=5, marker="ReportPage")
    locked = tmp_path / "report.pdf"
    protect_pdf(str(plain), str(locked), PASSWORD)
    return locked


@pytest.fixture
def scan_pdf(tmp_path):
    """One image-only page (no text layer) with two large words on it."""
    import img2pdf

    img = Image.new("RGB", (1275, 1650), "white")
    draw = ImageDraw.Draw(img)
    font = _font(90)
    draw.text((150, 200), "TESSERACT", fill="black", font=font)
    draw.text((150, 400), "SEARCHABLE", fill="black", font=font)
    png = tmp_path / "scan.png"
    img.save(png, dpi=(150, 150))
    path = tmp_path / "report.pdf"
    path.write_bytes(img2pdf.convert(str(png)))
    return path


@pytest.fixture
def anonymous(client):
    prev = APP.config["API_ANONYMOUS"]
    APP.config["API_ANONYMOUS"] = True
    try:
        yield client
    finally:
        APP.config["API_ANONYMOUS"] = prev


@pytest.fixture
def gated(client):
    prev = APP.config["API_ANONYMOUS"]
    APP.config["API_ANONYMOUS"] = False
    try:
        yield client
    finally:
        APP.config["API_ANONYMOUS"] = prev


@pytest.fixture
def api_key(client, make_user):
    user = make_user()
    with APP.app_context():
        raw, _ = ApiKey.issue(db.session.get(User, user.id), "test")
        db.session.commit()
    return raw


# --------------------------------------------------------------------------
# Checks on each endpoint's output
# --------------------------------------------------------------------------
def searchable(response):
    assert response.mimetype == "application/pdf"
    text = text_of(response).upper()
    assert "TESSERACT" in text and "SEARCHABLE" in text


def five_page_images(response):
    assert response.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
        names = sorted(zf.namelist())
        assert names == [f"page_{i:03d}.png" for i in range(1, 6)]
        with Image.open(io.BytesIO(zf.read(names[0]))) as image:
            assert image.format == "PNG"
            assert image.size == (425, 550)        # letter at 50 dpi


def five_page_text(response):
    assert response.mimetype == "text/plain"
    text = response.data.decode("utf-8")
    assert text.count("--- Page ") == 5
    assert "ReportPage page 1" in text and "ReportPage page 5" in text


def protected(response):
    assert response.mimetype == "application/pdf"
    out = reader(response)
    assert out.is_encrypted
    assert out.decrypt(PASSWORD)
    assert len(out.pages) == 5


def unlocked(response):
    assert response.mimetype == "application/pdf"
    out = reader(response)
    assert not out.is_encrypted
    assert "ReportPage page 5" in text_of(response)


# (path, input fixture, happy fields, check, download name, bad fields)
CASES = [
    pytest.param("/api/v1/pages/ocr", "scan_pdf", {"language": "eng"},
                 searchable, "report_searchable.pdf", {"language": "zzz"},
                 id="ocr", marks=requires_ocr),
    pytest.param("/api/v1/export", "report_pdf", {"operation": "images", "dpi": "50"},
                 five_page_images, "report_images.zip", {"dpi": "5"},
                 id="export-images"),
    pytest.param("/api/v1/export", "report_pdf", {"operation": "text"},
                 five_page_text, "report.txt", {"operation": "pdf_to_text"},
                 id="export-text"),
    pytest.param("/api/v1/protect", "report_pdf", {"password": PASSWORD},
                 protected, "report-protected.pdf", {"password": ""},
                 id="protect"),
    pytest.param("/api/v1/unlock", "locked_pdf", {"password": PASSWORD},
                 unlocked, "report-unlocked.pdf", {"password": "not-it"},
                 id="unlock"),
]
ARGS = "path,source,fields,check,name,bad"


# --------------------------------------------------------------------------
# Per endpoint
# --------------------------------------------------------------------------
@pytest.mark.parametrize(ARGS, CASES)
def test_happy_path_answers_the_file(anonymous, request, path, source, fields, check, name, bad):
    response = post(anonymous, path, request.getfixturevalue(source), fields)
    assert response.status_code == 200, response.data[:300]
    assert name in response.headers["Content-Disposition"]
    check(response)
    assert list(anonymous.output_dir.iterdir()) == []


@pytest.mark.parametrize(ARGS, CASES)
def test_bad_option_is_400(anonymous, request, path, source, fields, check, name, bad):
    response = post(anonymous, path, request.getfixturevalue(source), {**fields, **bad})
    assert response.status_code == 400, response.data[:300]
    assert response.get_json()["error"]
    assert list(anonymous.output_dir.iterdir()) == []


@pytest.mark.parametrize(ARGS, CASES)
def test_no_key_is_401(gated, request, path, source, fields, check, name, bad):
    response = post(gated, path, request.getfixturevalue(source), fields)
    assert response.status_code == 401
    assert response.get_json()["error"] == "API key required. Create one at /account."


@pytest.mark.parametrize(ARGS, CASES)
def test_a_valid_key_works(gated, api_key, request, path, source, fields, check, name, bad):
    response = post(gated, path, request.getfixturevalue(source), fields,
                    headers={"Authorization": f"Bearer {api_key}"})
    assert response.status_code == 200
    check(response)


@pytest.mark.parametrize(ARGS, CASES)
def test_each_call_costs_one_operation(anonymous, metered, request,
                                       path, source, fields, check, name, bad):
    pdf = request.getfixturevalue(source)
    assert post(anonymous, path, pdf, {**fields, **bad}).status_code == 400
    assert usage_rows() == set()                      # a refused call costs nothing
    response = post(anonymous, path, pdf, fields)
    assert response.status_code == 200
    assert usage_rows() == {("ip", 1)}
    assert response.headers["X-Docist-Usage-Remaining"] == "1"


@pytest.mark.parametrize(ARGS, CASES)
def test_a_failure_answers_the_fixed_message(anonymous, request, monkeypatch, caplog,
                                             path, source, fields, check, name, bad):
    def boom(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(api, "run_in_job", boom)
    response = post(anonymous, path, request.getfixturevalue(source), fields)
    assert response.status_code == 500
    assert response.get_json() == {"error": FIXED, "code": "server_error"}
    assert SECRET.encode() not in response.data
    assert SECRET in caplog.text                      # the log keeps the cause


@pytest.mark.parametrize(ARGS, CASES)
def test_a_renamed_non_pdf_is_400(anonymous, tmp_path, path, source, fields, check, name, bad):
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"not a pdf at all")
    response = post(anonymous, path, fake, fields)
    assert response.status_code == 400
    assert str(tmp_path).encode() not in response.data


@pytest.mark.parametrize("path", ["/api/v1/pages/ocr", "/api/v1/export",
                                  "/api/v1/protect", "/api/v1/unlock"])
def test_missing_file_is_400(anonymous, path):
    response = anonymous.post(path, data={"operation": "text", "password": PASSWORD},
                              content_type="multipart/form-data")
    assert response.status_code == 400
    assert "file" in response.get_json()["error"]


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------
def test_ocr_without_tesseract_is_400_with_the_hint(anonymous, report_pdf, monkeypatch):
    monkeypatch.setattr(api, "ocr_is_available", lambda: False)
    response = post(anonymous, "/api/v1/pages/ocr", report_pdf, {})
    assert response.status_code == 400
    assert response.get_json()["error"] == ocr_ops.UNAVAILABLE_HINT


@requires_ocr
def test_ocr_language_defaults_to_english(anonymous, scan_pdf):
    response = post(anonymous, "/api/v1/pages/ocr", scan_pdf, {})
    assert response.status_code == 200, response.data[:300]
    searchable(response)


@requires_ocr
def test_ocr_runs_in_the_request_job(anonymous, scan_pdf, monkeypatch):
    seen = []

    def spy(work, tmpdir):
        seen.append(tmpdir)
        return work()

    monkeypatch.setattr(api, "run_in_job", spy)
    response = post(anonymous, "/api/v1/pages/ocr", scan_pdf, {"language": "eng"})
    assert response.status_code == 200
    assert len(seen) == 1


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------
def test_export_images_as_jpg(anonymous, report_pdf):
    response = post(anonymous, "/api/v1/export", report_pdf,
                    {"operation": "images", "fmt": "jpg", "dpi": "40"})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
        names = sorted(zf.namelist())
        assert names[0] == "page_001.jpg" and len(names) == 5
        with Image.open(io.BytesIO(zf.read(names[0]))) as image:
            assert image.format == "JPEG"


@pytest.mark.parametrize("fields", [{}, {"operation": "images", "fmt": "gif"},
                                    {"operation": "images", "dpi": "lots"},
                                    {"operation": "images", "dpi": "900"}])
def test_export_bad_fields_are_400(anonymous, report_pdf, fields):
    response = post(anonymous, "/api/v1/export", report_pdf, fields)
    assert response.status_code == 400
    assert response.get_json()["error"]


def test_export_text_ocr_fallback_without_tesseract_is_400(anonymous, report_pdf, monkeypatch):
    monkeypatch.setattr(api, "ocr_is_available", lambda: False)
    response = post(anonymous, "/api/v1/export", report_pdf,
                    {"operation": "text", "ocr_fallback": "on"})
    assert response.status_code == 400
    assert response.get_json()["error"] == ocr_ops.UNAVAILABLE_HINT


@requires_ocr
def test_export_text_ocr_fallback_reads_a_scan(anonymous, scan_pdf):
    response = post(anonymous, "/api/v1/export", scan_pdf,
                    {"operation": "text", "ocr_fallback": "on", "language": "eng"})
    assert response.status_code == 200, response.data[:300]
    text = response.data.decode("utf-8").upper()
    assert "TESSERACT" in text


@requires_ocr
def test_export_text_bad_ocr_language_is_400(anonymous, report_pdf):
    response = post(anonymous, "/api/v1/export", report_pdf,
                    {"operation": "text", "ocr_fallback": "on", "language": "zzz"})
    assert response.status_code == 400


# --------------------------------------------------------------------------
# Passwords: never in the response, never in the log
# --------------------------------------------------------------------------
def assert_password_absent(response, caplog, password=PASSWORD):
    assert password.encode() not in response.data
    assert password not in caplog.text
    for record in caplog.records:
        assert password not in record.getMessage()
        assert password not in str(record.args)


def test_protect_empty_password_is_400(anonymous, report_pdf, caplog):
    caplog.set_level(logging.DEBUG)
    response = post(anonymous, "/api/v1/protect", report_pdf, {"password": ""})
    assert response.status_code == 400
    assert "password" in response.get_json()["error"]


def test_protect_a_library_refusal_quoting_the_password_is_a_fixed_400(
        anonymous, report_pdf, monkeypatch, caplog):
    # A UnicodeEncodeError is a ValueError whose text quotes the password.
    def refuse(input_path, output_path, password):
        raise UnicodeEncodeError("utf-8", password, 0, len(password), f"bad {password}")

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(api, "protect_pdf", refuse)
    response = post(anonymous, "/api/v1/protect", report_pdf, {"password": PASSWORD})
    assert response.status_code == 400
    assert response.get_json()["error"] == (
        "Docist couldn't protect this PDF. Check the password, or try another file.")
    assert_password_absent(response, caplog)


def test_protect_failure_is_500_without_the_password(anonymous, report_pdf, monkeypatch, caplog):
    def boom(input_path, output_path, password):
        raise RuntimeError(SECRET)

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(api, "protect_pdf", boom)
    response = post(anonymous, "/api/v1/protect", report_pdf, {"password": PASSWORD})
    assert response.status_code == 500
    assert response.get_json() == {"error": FIXED, "code": "server_error"}
    assert SECRET in caplog.text                      # logged through logger.exception
    assert_password_absent(response, caplog)


def test_unlock_wrong_password_is_a_fixed_400(anonymous, locked_pdf, caplog):
    caplog.set_level(logging.DEBUG)
    wrong = "Wrong-Walrus-5521"
    response = post(anonymous, "/api/v1/unlock", locked_pdf, {"password": wrong})
    assert response.status_code == 400
    assert response.get_json() == {"error": "Incorrect password"}
    assert_password_absent(response, caplog, wrong)
    assert_password_absent(response, caplog)


def test_unlock_empty_password_is_400(anonymous, locked_pdf):
    response = post(anonymous, "/api/v1/unlock", locked_pdf, {"password": ""})
    assert response.status_code == 400


def test_unlock_a_plain_pdf_is_400(anonymous, report_pdf, caplog):
    caplog.set_level(logging.DEBUG)
    response = post(anonymous, "/api/v1/unlock", report_pdf, {"password": PASSWORD})
    assert response.status_code == 400
    assert response.get_json() == {"error": "PDF is not password-protected"}
    assert_password_absent(response, caplog)


def test_unlock_failure_is_500_without_the_password(anonymous, locked_pdf, monkeypatch, caplog):
    def boom(input_path, output_path, password):
        raise RuntimeError(SECRET)

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(api, "unlock_pdf", boom)
    response = post(anonymous, "/api/v1/unlock", locked_pdf, {"password": PASSWORD})
    assert response.status_code == 500
    assert response.get_json() == {"error": FIXED, "code": "server_error"}
    assert SECRET in caplog.text
    assert_password_absent(response, caplog)


@pytest.mark.parametrize("path,source", [("/api/v1/protect", "report_pdf"),
                                         ("/api/v1/unlock", "locked_pdf")])
def test_pypdf_warnings_do_not_quote_the_password(anonymous, request, monkeypatch, caplog,
                                                  path, source):
    # pypdf warns, naming the code point, when SASLprep rejects a password
    # character. Run the job in this process so caplog sees what it logs.
    monkeypatch.setattr(api, "run_in_job", lambda work, tmpdir: work())
    caplog.set_level(logging.DEBUG)
    response = post(anonymous, path, request.getfixturevalue(source),
                    {"password": "Zebra\x07Quartz"})
    assert response.status_code in (200, 400)
    assert "U+0007" not in caplog.text
    assert "SASLprep" not in caplog.text
    assert logging.getLogger("pypdf").level == logging.NOTSET   # restored


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
def test_formats_lists_the_four(client):
    operations = client.get("/api/v1/formats").get_json()["operations"]
    for path in ("/api/v1/pages/ocr", "/api/v1/export", "/api/v1/protect", "/api/v1/unlock"):
        assert path in operations
