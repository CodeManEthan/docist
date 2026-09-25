"""Route-level hardening regressions: unguessable outputs + content sniffing."""
import io
import re


def _post_pdf(client, url, pdf_bytes, name="doc.pdf", **form):
    data = {"file": (io.BytesIO(pdf_bytes), name), **form}
    return client.post(url, data=data, content_type="multipart/form-data")


def _run_twice(client, url, pdf_bytes, **form):
    first = _post_pdf(client, url, pdf_bytes, **form)
    second = _post_pdf(client, url, pdf_bytes, **form)
    assert first.status_code == 200, first.data
    assert second.status_code == 200, second.data
    return first.get_json()["filename"], second.get_json()["filename"]


KEY = re.compile(r"^[0-9a-f]{32}_")


def _friendly(name):
    assert KEY.match(name), name
    return name[33:]


class TestUnguessableOutputs:
    """H1: result names carry a random key, so repeat runs never collide and
    nobody can fetch another visitor's result by guessing its name."""

    def test_pages_extract_twice(self, client, tmp_path, builders):
        pdf = builders.pdf(tmp_path / "doc.pdf", pages=3).read_bytes()
        a, b = _run_twice(
            client, "/pages/run", pdf, operation="extract", ranges="1-2"
        )
        assert a != b
        assert _friendly(a) == _friendly(b) == "doc_extracted.pdf"
        assert (client.output_dir / a).exists()
        assert (client.output_dir / b).exists()

    def test_print_nup_twice(self, client, tmp_path, builders):
        pdf = builders.pdf(tmp_path / "doc.pdf", pages=4).read_bytes()
        a, b = _run_twice(client, "/print/run", pdf, operation="nup", n="2")
        assert a != b
        assert _friendly(a) == _friendly(b) == "doc_2up.pdf"

    def test_export_text_twice(self, client, tmp_path, builders):
        pdf = builders.pdf(tmp_path / "doc.pdf", pages=2).read_bytes()
        a, b = _run_twice(client, "/export/run", pdf, operation="text")
        assert a != b
        assert _friendly(a) == _friendly(b) == "doc.txt"

    def test_download_uses_friendly_name(self, client, tmp_path, builders):
        pdf = builders.pdf(tmp_path / "doc.pdf", pages=2).read_bytes()
        name = _post_pdf(client, "/export/run", pdf, operation="text").get_json()["filename"]
        resp = client.get(f"/download?filename={name}")
        assert resp.status_code == 200
        assert "filename=doc.txt" in resp.headers["Content-Disposition"]

    def test_download_refuses_guessable_name(self, client):
        # A file under an old-style, guessable name is never served, even
        # though it exists in OUTPUT_FOLDER.
        (client.output_dir / "report-merged.pdf").write_bytes(b"%PDF-1.4 secret")
        resp = client.get("/download?filename=report-merged.pdf")
        assert resp.status_code == 400

    def test_download_refuses_wrong_key(self, client, tmp_path, builders):
        pdf = builders.pdf(tmp_path / "doc.pdf", pages=2).read_bytes()
        name = _post_pdf(client, "/export/run", pdf, operation="text").get_json()["filename"]
        guess = "0" * 32 + "_" + _friendly(name)
        assert client.get(f"/download?filename={guess}").status_code == 404


class TestContentSniffing:
    def test_convert_rejects_fake_png(self, client):
        resp = _post_pdf(
            client, "/convert/run", b"this is not an image",
            name="fake.png", target=".jpg",
        )
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_security_rejects_fake_pdf(self, client):
        resp = _post_pdf(
            client, "/security/run", b"MZ\x90\x00 not a pdf",
            name="fake.pdf", operation="watermark", text="DRAFT",
        )
        assert resp.status_code == 400
        assert "does not look like" in resp.get_json()["error"]

    def test_convert_accepts_real_png(self, client, tmp_path, builders):
        png = builders.png(tmp_path / "real.png").read_bytes()
        resp = _post_pdf(
            client, "/convert/run", png, name="real.png", target=".jpg"
        )
        assert resp.status_code == 200, resp.data
