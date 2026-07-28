"""Route-level hardening regressions: collision-safe outputs + content sniffing."""
import io


def _post_pdf(client, url, pdf_bytes, name="doc.pdf", **form):
    data = {"file": (io.BytesIO(pdf_bytes), name), **form}
    return client.post(url, data=data, content_type="multipart/form-data")


def _run_twice(client, url, pdf_bytes, **form):
    first = _post_pdf(client, url, pdf_bytes, **form)
    second = _post_pdf(client, url, pdf_bytes, **form)
    assert first.status_code == 200, first.data
    assert second.status_code == 200, second.data
    return first.get_json()["filename"], second.get_json()["filename"]


class TestCollisionSafeOutputs:
    def test_pages_extract_twice(self, client, tmp_path, builders):
        pdf = builders.pdf(tmp_path / "doc.pdf", pages=3).read_bytes()
        a, b = _run_twice(
            client, "/pages/run", pdf, operation="extract", ranges="1-2"
        )
        assert a == "doc_extracted.pdf"
        assert b == "doc_extracted_1.pdf"
        assert (client.output_dir / a).exists()
        assert (client.output_dir / b).exists()

    def test_print_nup_twice(self, client, tmp_path, builders):
        pdf = builders.pdf(tmp_path / "doc.pdf", pages=4).read_bytes()
        a, b = _run_twice(client, "/print/run", pdf, operation="nup", n="2")
        assert a == "doc_2up.pdf"
        assert b == "doc_2up_1.pdf"

    def test_export_text_twice(self, client, tmp_path, builders):
        pdf = builders.pdf(tmp_path / "doc.pdf", pages=2).read_bytes()
        a, b = _run_twice(client, "/export/run", pdf, operation="text")
        assert a == "doc.txt"
        assert b == "doc_1.txt"


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
