"""The page, print and stamp tools on the REST API (/api/v1).

Each endpoint takes one PDF and the form fields its tool page sends, runs the
same pdf_ops function as the browser route, and answers the finished PDF.
Every case runs against the same table of endpoints: the happy path, a bad
option (400), auth (401 without a key, open with DOCIST_API_ANONYMOUS, a
valid key works), metering (one call costs one operation) and the fixed 500.
"""
import io

import pytest
from pypdf import PdfReader

import app as flask_app_module
from models import ApiKey, Usage, User, db
from routes import api

APP = flask_app_module.app
FIXED = "Docist couldn't finish this. Try again, or try another file."
SECRET = "internal-detail-xyz"
LETTER_LANDSCAPE = (792, 612)
A4_LANDSCAPE = (842, 595)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def reader(response):
    return PdfReader(io.BytesIO(response.data))


def text_of(response):
    return "\n".join(page.extract_text() or "" for page in reader(response).pages)


def sheet_size(page):
    return round(float(page.mediabox.width)), round(float(page.mediabox.height))


def usage_rows():
    with APP.app_context():
        return {(u.subject[:2], u.count) for u in db.session.execute(db.select(Usage)).scalars()}


@pytest.fixture
def five_page_pdf(tmp_path, builders):
    return builders.pdf(tmp_path / "report.pdf", pages=5, marker="ReportPage")


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


def post(client, path, pdf, fields, headers=None):
    data = {"file": (open(pdf, "rb"), pdf.name)}
    data.update(fields)
    return client.post(path, data=data, content_type="multipart/form-data",
                       headers=headers or {})


# --------------------------------------------------------------------------
# Checks on the five-page input's output
# --------------------------------------------------------------------------
def rotated_two_and_four(response):
    rotations = [page.rotation for page in reader(response).pages]
    assert rotations == [0, 90, 0, 90, 0]


def two_pages_removed(response):
    out = reader(response)
    assert len(out.pages) == 3
    text = text_of(response)
    assert "ReportPage page 2" not in text and "ReportPage page 4" not in text
    assert "ReportPage page 5" in text


def five_pages(response):
    assert len(reader(response).pages) == 5


def three_letter_sheets(response):
    pages = reader(response).pages
    assert len(pages) == 3            # 5 pages, 2 to a sheet
    assert sheet_size(pages[0]) == LETTER_LANDSCAPE


def two_a4_portrait_sheets(response):
    pages = reader(response).pages
    assert len(pages) == 2            # 5 pages, 4 to a sheet
    assert sheet_size(pages[0]) == tuple(reversed(A4_LANDSCAPE))


def booklet_of_eight(response):
    pages = reader(response).pages
    assert len(pages) == 4            # padded to 8 pages, 2 to a sheet side
    assert sheet_size(pages[0]) == A4_LANDSCAPE


def page_x_of_y(response):
    assert len(reader(response).pages) == 5
    text = text_of(response)
    assert "Page 1 of 5" in text and "Page 5 of 5" in text


def bates_labels(response):
    assert len(reader(response).pages) == 5
    text = text_of(response)
    assert "ACME0007" in text and "ACME0011" in text


# (path, happy fields, check, download name, bad fields)
CASES = [
    pytest.param("/api/v1/pages/rotate", {"angle": "90", "ranges": "2,4"},
                 rotated_two_and_four, "report_rotated.pdf", {"angle": "45"},
                 id="rotate"),
    pytest.param("/api/v1/pages/remove", {"ranges": "2,4"},
                 two_pages_removed, "report_trimmed.pdf", {"ranges": "1-5"},
                 id="remove"),
    pytest.param("/api/v1/pages/compress", {"image_quality": "50", "image_max_dpi": "100"},
                 five_pages, "report_compressed.pdf", {"image_quality": "5"},
                 id="compress"),
    pytest.param("/api/v1/print/nup", {"n": "2"},
                 three_letter_sheets, "report_2up.pdf", {"n": "3"},
                 id="nup"),
    pytest.param("/api/v1/print/booklet", {"paper": "a4"},
                 booklet_of_eight, "report_booklet.pdf", {"paper": "legal"},
                 id="booklet"),
    pytest.param("/api/v1/stamp/header-footer", {"footer_center": "Page {page} of {pages}"},
                 page_x_of_y, "report-stamped.pdf", {"footer_center": "  "},
                 id="header-footer"),
    pytest.param("/api/v1/stamp/bates",
                 {"prefix": "ACME", "start": "7", "digits": "4", "position": "top-left"},
                 bates_labels, "report-bates.pdf", {"digits": "2"},
                 id="bates"),
]


# --------------------------------------------------------------------------
# Per endpoint
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path,fields,check,name,bad", CASES)
def test_happy_path_answers_the_pdf(anonymous, five_page_pdf, path, fields, check, name, bad):
    response = post(anonymous, path, five_page_pdf, fields)
    assert response.status_code == 200, response.data[:300]
    assert response.mimetype == "application/pdf"
    assert name in response.headers["Content-Disposition"]
    check(response)
    assert list(anonymous.output_dir.iterdir()) == []


@pytest.mark.parametrize("path,fields,check,name,bad", CASES)
def test_bad_option_is_400(anonymous, five_page_pdf, path, fields, check, name, bad):
    response = post(anonymous, path, five_page_pdf, {**fields, **bad})
    assert response.status_code == 400, response.data[:300]
    assert response.get_json()["error"]
    assert list(anonymous.output_dir.iterdir()) == []


@pytest.mark.parametrize("path,fields,check,name,bad", CASES)
def test_no_key_is_401(gated, five_page_pdf, path, fields, check, name, bad):
    response = post(gated, path, five_page_pdf, fields)
    assert response.status_code == 401
    assert response.get_json()["error"] == "API key required. Create one at /account."


@pytest.mark.parametrize("path,fields,check,name,bad", CASES)
def test_a_valid_key_works(gated, api_key, five_page_pdf, path, fields, check, name, bad):
    response = post(gated, path, five_page_pdf, fields,
                    headers={"Authorization": f"Bearer {api_key}"})
    assert response.status_code == 200
    check(response)


@pytest.mark.parametrize("path,fields,check,name,bad", CASES)
def test_each_call_costs_one_operation(anonymous, metered, five_page_pdf,
                                       path, fields, check, name, bad):
    assert post(anonymous, path, five_page_pdf, {**fields, **bad}).status_code == 400
    assert usage_rows() == set()                      # a refused call costs nothing
    response = post(anonymous, path, five_page_pdf, fields)
    assert response.status_code == 200
    assert usage_rows() == {("ip", 1)}
    assert response.headers["X-Docist-Usage-Remaining"] == "1"


@pytest.mark.parametrize("path,fields,check,name,bad", CASES)
def test_a_failure_answers_the_fixed_message(anonymous, five_page_pdf, monkeypatch, caplog,
                                             path, fields, check, name, bad):
    def boom(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(api, "run_in_job", boom)
    response = post(anonymous, path, five_page_pdf, fields)
    assert response.status_code == 500
    assert response.get_json() == {"error": FIXED, "code": "server_error"}
    assert SECRET.encode() not in response.data
    assert SECRET in caplog.text                      # the log keeps the cause


# --------------------------------------------------------------------------
# Options the table doesn't cover
# --------------------------------------------------------------------------
def test_rotate_without_ranges_turns_every_page(anonymous, five_page_pdf):
    response = post(anonymous, "/api/v1/pages/rotate", five_page_pdf, {"angle": "180"})
    assert response.status_code == 200
    assert [page.rotation for page in reader(response).pages] == [180] * 5


@pytest.mark.parametrize("fields", [{"angle": ""}, {"angle": "ninety"},
                                    {"angle": "90", "ranges": "9"}])
def test_rotate_bad_angle_or_range_is_400(anonymous, five_page_pdf, fields):
    response = post(anonymous, "/api/v1/pages/rotate", five_page_pdf, fields)
    assert response.status_code == 400


@pytest.mark.parametrize("fields", [{}, {"ranges": "0"}, {"ranges": "x-y"}])
def test_remove_needs_valid_ranges(anonymous, five_page_pdf, fields):
    response = post(anonymous, "/api/v1/pages/remove", five_page_pdf, fields)
    assert response.status_code == 400


@pytest.mark.parametrize("fields", [{"image_quality": "high"}, {"image_max_dpi": "50"},
                                    {"image_max_dpi": "400"}])
def test_compress_bounds(anonymous, five_page_pdf, fields):
    response = post(anonymous, "/api/v1/pages/compress", five_page_pdf, fields)
    assert response.status_code == 400
    assert "must be" in response.get_json()["error"]


def test_compress_defaults(anonymous, five_page_pdf):
    response = post(anonymous, "/api/v1/pages/compress", five_page_pdf, {})
    assert response.status_code == 200
    five_pages(response)


def test_nup_four_on_a4(anonymous, five_page_pdf):
    response = post(anonymous, "/api/v1/print/nup", five_page_pdf, {"n": "4", "paper": "a4"})
    assert response.status_code == 200
    assert "report_4up.pdf" in response.headers["Content-Disposition"]
    two_a4_portrait_sheets(response)


@pytest.mark.parametrize("fields", [{}, {"n": "two"}, {"n": "2", "paper": "legal"}])
def test_nup_bad_fields_are_400(anonymous, five_page_pdf, fields):
    response = post(anonymous, "/api/v1/print/nup", five_page_pdf, fields)
    assert response.status_code == 400


def test_booklet_default_paper_is_letter(anonymous, five_page_pdf):
    response = post(anonymous, "/api/v1/print/booklet", five_page_pdf, {})
    assert response.status_code == 200
    assert sheet_size(reader(response).pages[0]) == LETTER_LANDSCAPE


@pytest.mark.parametrize("fields", [{"color": "red"}, {"font_size": "big"},
                                    {"margin": "wide"}])
def test_header_footer_bad_style_is_400(anonymous, five_page_pdf, fields):
    response = post(anonymous, "/api/v1/stamp/header-footer", five_page_pdf,
                    {"header_left": "Draft", **fields})
    assert response.status_code == 400


def test_bates_defaults(anonymous, five_page_pdf):
    response = post(anonymous, "/api/v1/stamp/bates", five_page_pdf, {})
    assert response.status_code == 200
    text = text_of(response)
    assert "000001" in text and "000005" in text


@pytest.mark.parametrize("fields", [{"start": "-1"}, {"start": "one"},
                                    {"position": "middle"}, {"color": "#12"}])
def test_bates_bad_fields_are_400(anonymous, five_page_pdf, fields):
    response = post(anonymous, "/api/v1/stamp/bates", five_page_pdf, fields)
    assert response.status_code == 400


@pytest.mark.parametrize("path", [case.values[0] for case in CASES])
def test_missing_file_is_400(anonymous, path):
    response = anonymous.post(path, data={}, content_type="multipart/form-data")
    assert response.status_code == 400
    assert "file" in response.get_json()["error"]


@pytest.mark.parametrize("path,fields,check,name,bad", CASES)
def test_a_renamed_non_pdf_is_400(anonymous, tmp_path, path, fields, check, name, bad):
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"not a pdf at all")
    response = post(anonymous, path, fake, fields)
    assert response.status_code == 400
    assert str(tmp_path).encode() not in response.data


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
def test_formats_lists_every_operation(client):
    operations = client.get("/api/v1/formats").get_json()["operations"]
    for case in CASES:
        assert case.values[0] in operations
    posts = {rule.rule for rule in APP.url_map.iter_rules()
             if rule.rule.startswith("/api/v1/") and "POST" in rule.methods}
    assert set(operations) == posts
