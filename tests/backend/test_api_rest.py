"""Integration tests for the versioned REST API (/api/v1) and its docs page.

The API differs from the browser routes in two ways these tests pin down:
the response body *is* the finished file (not JSON pointing at /download),
and nothing is ever left behind in OUTPUT_FOLDER.
"""
import io
import zipfile

import pytest
from PIL import Image
from pypdf import PdfReader

import app as flask_app_module
from models import ApiKey, User, db


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def upload(path, name=None):
    """A (file object, filename) tuple for the Flask test client."""
    return (open(path, "rb"), name or path.name)


def pdf_pages(response):
    """Page count of a PDF returned in a response body."""
    return len(PdfReader(io.BytesIO(response.data)).pages)


def pdf_text(response):
    reader = PdfReader(io.BytesIO(response.data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def disposition(response):
    return response.headers.get("Content-Disposition", "")


def assert_no_residue(client):
    """The temp-dir contract: API calls never write into OUTPUT_FOLDER."""
    assert list(client.output_dir.iterdir()) == []


@pytest.fixture(autouse=True)
def api_anonymous(client):
    """Most tests here call the API without a key: open it for them. The auth
    tests below close it again through the ``gated`` fixture."""
    app = flask_app_module.app
    prev = app.config["API_ANONYMOUS"]
    app.config["API_ANONYMOUS"] = True
    try:
        yield
    finally:
        app.config["API_ANONYMOUS"] = prev


@pytest.fixture
def two_pdfs(tmp_path, builders):
    a = builders.pdf(tmp_path / "alpha.pdf", pages=2, marker=builders.PDF_MARKER_A)
    b = builders.pdf(tmp_path / "beta.pdf", pages=2, marker=builders.PDF_MARKER_B)
    return a, b


@pytest.fixture
def five_page_pdf(tmp_path, builders):
    return builders.pdf(tmp_path / "report.pdf", pages=5, marker="ReportPage")


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------
class TestMerge:
    def test_merges_two_pdfs_into_one_file_response(self, client, two_pdfs):
        a, b = two_pdfs
        response = client.post(
            "/api/v1/merge",
            data={"files[]": [upload(a), upload(b)]},
            content_type="multipart/form-data",
        )
        assert response.status_code == 200
        assert response.mimetype == "application/pdf"
        assert "alpha-merged.pdf" in disposition(response)
        assert pdf_pages(response) == 4  # 2 + 2, both even -> no blank padding
        assert_no_residue(client)

    def test_blank_padding_applies_to_odd_sources(self, client, tmp_path, builders):
        a = builders.pdf(tmp_path / "odd.pdf", pages=1)
        b = builders.pdf(tmp_path / "other.pdf", pages=1)
        response = client.post(
            "/api/v1/merge",
            data={"files[]": [upload(a), upload(b)], "blank_pages": "true"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 200
        assert pdf_pages(response) == 4  # each odd source gains a blank page

    def test_merge_options_are_honoured(self, client, two_pdfs):
        a, b = two_pdfs
        response = client.post(
            "/api/v1/merge",
            data={
                "files[]": [upload(a), upload(b)],
                "page_numbers": "true",
                "number_position": "bottom-center",
                "start_number": "7",
                "bookmarks": "true",
                "blank_pages": "false",
            },
            content_type="multipart/form-data",
        )
        assert response.status_code == 200
        text = pdf_text(response)
        assert "7" in text and "10" in text  # numbering started at 7
        assert_no_residue(client)

    def test_bad_option_is_400(self, client, two_pdfs):
        a, b = two_pdfs
        response = client.post(
            "/api/v1/merge",
            data={"files[]": [upload(a), upload(b)], "number_position": "top-left"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
        assert "error" in response.get_json()
        assert_no_residue(client)

    def test_converts_non_pdf_sources(self, client, tmp_path, builders):
        pdf = builders.pdf(tmp_path / "doc.pdf", pages=2)
        png = builders.png(tmp_path / "pic.png")
        response = client.post(
            "/api/v1/merge",
            data={"files[]": [upload(pdf), upload(png)], "blank_pages": "false"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 200
        assert pdf_pages(response) == 3  # 2 PDF pages + 1 image page


class TestConvert:
    def test_png_to_jpg(self, client, tmp_path, builders):
        png = builders.png(tmp_path / "diagram.png")
        response = client.post(
            "/api/v1/convert",
            data={"file": upload(png), "target": ".jpg"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 200
        assert "diagram.jpg" in disposition(response)
        assert Image.open(io.BytesIO(response.data)).format == "JPEG"
        assert_no_residue(client)

    def test_bare_extension_without_dot_is_accepted(self, client, tmp_path, builders):
        png = builders.png(tmp_path / "pic.png")
        response = client.post(
            "/api/v1/convert",
            data={"file": upload(png), "target": "jpg"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 200

    def test_multipage_pdf_to_png_returns_the_zip_actually_written(
        self, client, five_page_pdf
    ):
        """The registry's actual-written-path contract: many pages -> one zip."""
        response = client.post(
            "/api/v1/convert",
            data={"file": upload(five_page_pdf), "target": ".png"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 200
        assert ".zip" in disposition(response)
        assert response.mimetype == "application/zip"
        with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
            assert len(zf.namelist()) == 5
        assert_no_residue(client)


class TestPagesExtract:
    def test_extracts_the_requested_ranges(self, client, five_page_pdf):
        response = client.post(
            "/api/v1/pages/extract",
            data={"file": upload(five_page_pdf), "ranges": "1-3,5"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 200
        assert response.mimetype == "application/pdf"
        assert "report_extracted.pdf" in disposition(response)
        assert pdf_pages(response) == 4
        text = pdf_text(response)
        assert "ReportPage page 4" not in text
        assert "ReportPage page 5" in text
        assert_no_residue(client)


class TestPagesSplit:
    def test_every_n_returns_a_zip_of_parts(self, client, five_page_pdf):
        response = client.post(
            "/api/v1/pages/split",
            data={"file": upload(five_page_pdf), "mode": "every_n", "value": "2"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 200
        assert response.mimetype == "application/zip"
        assert "report_split.zip" in disposition(response)
        with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
            names = zf.namelist()
            assert len(names) == 3  # 2 + 2 + 1
            first = PdfReader(io.BytesIO(zf.read(names[0])))
            assert len(first.pages) == 2
        assert_no_residue(client)

    def test_ranges_mode_emits_one_part_per_spec(self, client, five_page_pdf):
        response = client.post(
            "/api/v1/pages/split",
            data={"file": upload(five_page_pdf), "mode": "ranges", "value": "1-2;3-5"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
            assert len(zf.namelist()) == 2
        assert_no_residue(client)

    def test_unknown_mode_is_400(self, client, five_page_pdf):
        response = client.post(
            "/api/v1/pages/split",
            data={"file": upload(five_page_pdf), "mode": "sideways", "value": "2"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
        assert "error" in response.get_json()


class TestWatermark:
    def test_stamps_text_on_every_page(self, client, five_page_pdf):
        response = client.post(
            "/api/v1/watermark",
            data={
                "file": upload(five_page_pdf),
                "text": "CONFIDENTIALZETA",
                "position": "footer",
                "opacity": "0.4",
                "font_size": "20",
            },
            content_type="multipart/form-data",
        )
        assert response.status_code == 200
        assert response.mimetype == "application/pdf"
        assert "report-watermarked.pdf" in disposition(response)
        assert pdf_pages(response) == 5
        assert pdf_text(response).count("CONFIDENTIALZETA") == 5
        assert_no_residue(client)

    def test_missing_text_is_400(self, client, five_page_pdf):
        response = client.post(
            "/api/v1/watermark",
            data={"file": upload(five_page_pdf), "text": "   "},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
        assert "error" in response.get_json()

    def test_bad_opacity_is_400(self, client, five_page_pdf):
        response = client.post(
            "/api/v1/watermark",
            data={"file": upload(five_page_pdf), "text": "X", "opacity": "9"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400


class TestFormats:
    def test_reports_merge_inputs_and_the_convert_matrix(self, client):
        response = client.get("/api/v1/formats")
        assert response.status_code == 200
        data = response.get_json()
        assert ".pdf" in data["merge_extensions"]
        assert ".png" in data["merge_extensions"]
        assert isinstance(data["convert_matrix"], dict)
        assert ".png" in data["convert_sources"]
        assert ".jpg" in data["convert_matrix"][".png"]
        # The matrix agrees with the page-facing endpoint.
        assert data["convert_matrix"] == client.get("/convert/matrix").get_json()["matrix"]
        assert data["merge_extensions"] == client.get("/formats").get_json()["extensions"]


# --------------------------------------------------------------------------
# Error paths
# --------------------------------------------------------------------------
class TestErrors:
    def test_merge_without_files_is_400(self, client):
        response = client.post("/api/v1/merge", data={}, content_type="multipart/form-data")
        assert response.status_code == 400
        assert "error" in response.get_json()
        assert_no_residue(client)

    @pytest.mark.parametrize(
        "path", ["/api/v1/convert", "/api/v1/pages/extract",
                 "/api/v1/pages/split", "/api/v1/watermark"],
    )
    def test_missing_file_field_is_400(self, client, path):
        response = client.post(path, data={"target": ".png", "ranges": "1",
                                           "mode": "every_n", "value": "1",
                                           "text": "x"},
                               content_type="multipart/form-data")
        assert response.status_code == 400
        assert "error" in response.get_json()

    @pytest.mark.parametrize("ranges", ["", "0", "abc", "9-12", "1-", "3-1"])
    def test_bad_ranges_are_400(self, client, five_page_pdf, ranges):
        response = client.post(
            "/api/v1/pages/extract",
            data={"file": upload(five_page_pdf), "ranges": ranges},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
        assert "error" in response.get_json()
        assert_no_residue(client)

    def test_unsupported_target_is_400(self, client, tmp_path, builders):
        png = builders.png(tmp_path / "pic.png")
        response = client.post(
            "/api/v1/convert",
            data={"file": upload(png), "target": ".exe"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
        assert "Available targets" in response.get_json()["error"]

    def test_missing_target_is_400(self, client, tmp_path, builders):
        png = builders.png(tmp_path / "pic.png")
        response = client.post(
            "/api/v1/convert",
            data={"file": upload(png)},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400

    def test_unsupported_source_is_400(self, client, tmp_path):
        weird = tmp_path / "thing.xyz"
        weird.write_bytes(b"whatever")
        response = client.post(
            "/api/v1/convert",
            data={"file": upload(weird), "target": ".pdf"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
        assert "Supported inputs" in response.get_json()["error"]

    def test_fake_pdf_content_is_rejected(self, client, tmp_path):
        fake = tmp_path / "fake.pdf"
        fake.write_bytes(b"PK\x03\x04 definitely a zip, not a pdf")
        response = client.post(
            "/api/v1/pages/extract",
            data={"file": upload(fake), "ranges": "1"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
        assert "does not look like" in response.get_json()["error"]
        assert_no_residue(client)

    def test_fake_png_content_is_rejected_by_convert(self, client, tmp_path):
        fake = tmp_path / "fake.png"
        fake.write_bytes(b"%PDF-1.4 not really a png")
        response = client.post(
            "/api/v1/convert",
            data={"file": upload(fake), "target": ".jpg"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
        assert_no_residue(client)

    def test_fake_pdf_in_merge_is_rejected(self, client, tmp_path, builders):
        good = builders.pdf(tmp_path / "good.pdf", pages=1)
        fake = tmp_path / "bad.pdf"
        fake.write_bytes(b"nope")
        response = client.post(
            "/api/v1/merge",
            data={"files[]": [upload(good), upload(fake)]},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
        assert "bad.pdf" in response.get_json()["error"]
        assert_no_residue(client)

    def test_non_pdf_on_a_pdf_only_endpoint_is_400(self, client, tmp_path, builders):
        png = builders.png(tmp_path / "pic.png")
        response = client.post(
            "/api/v1/watermark",
            data={"file": upload(png), "text": "X"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
        assert "Only .pdf" in response.get_json()["error"]


# --------------------------------------------------------------------------
# Auth: per-user API keys; the session cookie never authenticates the API
# --------------------------------------------------------------------------
@pytest.fixture
def gated(client):
    """The standard test client with anonymous API access off (the default)."""
    flask_app_module.app.config["API_ANONYMOUS"] = False
    return client


@pytest.fixture
def api_key(client, make_user):
    """A verified user and a live raw key for them: ``(user, raw)``."""
    user = make_user()
    with flask_app_module.app.app_context():
        raw, _ = ApiKey.issue(db.session.get(User, user.id), "test")
        db.session.commit()
    return user, raw


def _key_row(raw):
    with flask_app_module.app.app_context():
        return db.session.execute(
            db.select(ApiKey).where(ApiKey.prefix == raw[:12])
        ).scalar_one()


class TestApiAuth:
    def _extract(self, client, pdf, headers=None):
        return client.post(
            "/api/v1/pages/extract",
            data={"file": upload(pdf), "ranges": "1-2"},
            content_type="multipart/form-data",
            headers=headers or {},
        )

    def test_post_without_key_is_401(self, gated, five_page_pdf):
        response = self._extract(gated, five_page_pdf)
        assert response.status_code == 401
        assert response.get_json()["error"] == "API key required. Create one at /account."

    def test_valid_key_works_and_stamps_last_used(self, gated, api_key, five_page_pdf):
        _, raw = api_key
        assert _key_row(raw).last_used_at is None
        response = self._extract(gated, five_page_pdf, {"Authorization": f"Bearer {raw}"})
        assert response.status_code == 200
        assert pdf_pages(response) == 2
        assert_no_residue(gated)
        assert _key_row(raw).last_used_at is not None

    def test_bearer_scheme_is_case_insensitive(self, gated, api_key, five_page_pdf):
        _, raw = api_key
        response = self._extract(gated, five_page_pdf, {"Authorization": f"bearer {raw}"})
        assert response.status_code == 200

    def test_revoked_key_is_401(self, gated, api_key, five_page_pdf):
        user, raw = api_key
        with flask_app_module.app.app_context():
            ApiKey.revoke_all(db.session.get(User, user.id))
            db.session.commit()
        response = self._extract(gated, five_page_pdf, {"Authorization": f"Bearer {raw}"})
        assert response.status_code == 401
        assert response.get_json()["error"] == "Invalid or revoked API key."

    def test_disabled_users_key_is_401(self, gated, api_key, five_page_pdf):
        user, raw = api_key
        with flask_app_module.app.app_context():
            db.session.get(User, user.id).disabled = True
            db.session.commit()
        response = self._extract(gated, five_page_pdf, {"Authorization": f"Bearer {raw}"})
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "header",
        ["Bearer wrong-key", "Bearer ", "Basic dk_abc", "dk_abc", "Bearer dk_"],
    )
    def test_bad_authorization_headers_are_401(self, gated, five_page_pdf, header):
        response = self._extract(gated, five_page_pdf, {"Authorization": header})
        assert response.status_code == 401

    def test_bad_key_never_falls_back_to_anonymous(self, client, five_page_pdf):
        # Anonymous access is on (autouse fixture), yet a wrong key still fails.
        response = self._extract(client, five_page_pdf, {"Authorization": "Bearer nope"})
        assert response.status_code == 401

    def test_anonymous_flag_opens_the_api(self, client, five_page_pdf):
        assert self._extract(client, five_page_pdf).status_code == 200

    def test_cookie_session_alone_does_not_authenticate(
            self, gated, make_user, login, five_page_pdf):
        login(gated, make_user())
        response = self._extract(gated, five_page_pdf)
        assert response.status_code == 401

    def test_formats_and_docs_stay_open(self, gated):
        response = gated.get("/api/v1/formats")
        assert response.status_code == 200
        assert ".pdf" in response.get_json()["merge_extensions"]
        assert gated.get("/api").status_code == 200


# --------------------------------------------------------------------------
# Docs page
# --------------------------------------------------------------------------
class TestDocsPage:
    def test_renders_with_the_api_nav_entry_active(self, client):
        response = client.get("/api")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert '<a href="/api" class="active">API</a>' in body
        assert '<a href="/convert">Convert Files</a>' in body
        assert '/static/style.css' in body
        assert '/static/favicon.svg' in body

    def test_documents_every_endpoint_and_the_limits(self, client):
        body = client.get("/api").get_data(as_text=True)
        for path in ("/api/v1/merge", "/api/v1/convert", "/api/v1/pages/extract",
                     "/api/v1/pages/split", "/api/v1/watermark", "/api/v1/formats"):
            assert path in body, path
        assert 'Authorization: Bearer' in body
        assert "429" in body and "Retry-After" in body
        assert "DOCIST_MAX_UPLOAD_MB" in body and "50 MB" in body
        assert "DOCIST_RATE_LIMIT" in body
