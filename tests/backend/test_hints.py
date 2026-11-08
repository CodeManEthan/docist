"""The tool pages' hints and wording: where files go, the Page Tools
subtitle, Bates and N-up, the OCR paths, the empty-page note on Export, the
booklet count, "1 page", and Word's Title and Subtitle styles.
"""
import io
import re
import zipfile

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

import app as flask_app_module
from pdf_ops import ocr as ocr_ops
from pdf_ops.export import pdf_to_text_report
from pdf_ops.pages import parse_page_ranges
from routes import export as export_routes
from routes.export import empty_pages_note
from routes.print import booklet_message

APP = flask_app_module.app

TOOL_PAGES = ["/", "/convert", "/pages", "/print", "/export", "/security"]
WHERE_RE = re.compile(r'<div class="upload-hint" id="whereFilesGo">(.*?)</div>', re.S)
RETENTION_RE = re.compile(r'<p class="pk-hint" id="pkRetention">(.*?)</p>', re.S)


def _text(html):
    return " ".join(re.sub(r"<[^>]+>", "", html).split())


def _pdf(pages_text):
    """A PDF whose pages carry the given text; '' makes a page with no text."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for text in pages_text:
        if text:
            c.setFont("Helvetica", 14)
            c.drawString(72, 720, text)
        c.showPage()
    c.save()
    return buf.getvalue()


# --------------------------------------------------------------------------
# Where files go
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path", TOOL_PAGES)
def test_each_tool_says_where_files_go_under_its_drop_zone(client, monkeypatch, path):
    monkeypatch.setitem(APP.config, "OUTPUT_MAX_AGE", 24 * 3600)
    body = client.get(path).get_data(as_text=True)
    found = WHERE_RE.findall(body)
    assert len(found) == 1, path
    assert _text(found[0]) == (
        "Uploads are deleted when the request ends. Results are kept 24 hours "
        "for download, and only your browser session can fetch them.")
    # Right under the drop zone.
    assert body.index('id="uploadArea"') < body.index('id="whereFilesGo"') < body.index('class="file-list"')


def test_where_files_go_follows_the_output_age(client, monkeypatch):
    monkeypatch.setitem(APP.config, "OUTPUT_MAX_AGE", 30 * 60)
    body = client.get("/convert").get_data(as_text=True)
    assert "Results are kept 30 minutes for download" in _text(WHERE_RE.search(body).group(1))


def test_the_packet_builder_states_its_staging_lifetimes(client, monkeypatch):
    monkeypatch.setitem(APP.config, "STAGING_TTL_MINUTES", 120)
    monkeypatch.setitem(APP.config, "STAGING_MAX_HOURS", 24)
    body = client.get("/packet").get_data(as_text=True)
    assert "whereFilesGo" not in body
    assert _text(RETENTION_RE.search(body).group(1)) == (
        "Files you add stay on this server while you work, and only your browser "
        "session can fetch them. They are deleted after 2 hours without use, or "
        "24 hours after you add them, whichever comes first.")
    monkeypatch.setitem(APP.config, "STAGING_TTL_MINUTES", 45)
    monkeypatch.setitem(APP.config, "STAGING_MAX_HOURS", 1)
    body = client.get("/packet").get_data(as_text=True)
    assert "deleted after 45 minutes without use, or 1 hour after you add them" in body


# --------------------------------------------------------------------------
# Page hints and wording
# --------------------------------------------------------------------------
def test_page_tools_subtitle_names_compress_ocr_and_the_packet_builder(client):
    body = client.get("/pages").get_data(as_text=True)
    subtitle = re.search(r'<p class="subtitle">(.*?)</p>', body, re.S).group(1)
    assert _text(subtitle) == (
        "Extract, remove, rotate, split, compress or OCR pages in a single PDF. "
        "To reorder pages or build one PDF page by page, use the Packet Builder.")
    assert 'href="/packet"' in subtitle


def test_security_has_the_bates_hint_and_the_watermark_example(client):
    body = client.get("/security").get_data(as_text=True)
    bates = re.search(r'<div class="options-panel" id="batesOptions".*?</div>', body, re.S).group(0)
    assert ("Bates numbers give every page a unique sequential ID, like ACME000001, "
            "the way legal teams label documents.") in _text(bates)
    assert 'id="text" placeholder="e.g. CONFIDENTIAL"' in body


def test_print_says_what_n_up_does(client):
    body = client.get("/print").get_data(as_text=True)
    hint = re.search(r'<p class="upload-hint" id="nupHint">(.*?)</p>', body, re.S).group(1)
    assert _text(hint).startswith("2-up puts two pages side by side on one sheet; 4-up puts four.")


def test_each_ocr_hint_names_the_other_two_tools(client, monkeypatch):
    monkeypatch.setattr(ocr_ops, "is_available", lambda: True)
    monkeypatch.setattr(export_routes, "ocr_is_available", lambda: True)
    hint_re = re.compile(r'id="ocrPathsHint">(.*?)</(?:p|div)>', re.S)
    others = {"/convert": ("/pages", "/export"), "/pages": ("/export", "/convert"),
              "/export": ("/pages", "/convert")}
    for path, links in others.items():
        hint = hint_re.search(client.get(path).get_data(as_text=True)).group(1)
        for link in links:
            assert f'href="{link}"' in hint, (path, link)


# --------------------------------------------------------------------------
# Export: pages with no text layer
# --------------------------------------------------------------------------
def test_text_report_lists_the_empty_pages(tmp_path):
    src = tmp_path / "src.pdf"
    src.write_bytes(_pdf(["", "words here", "", "more words"]))
    report = pdf_to_text_report(str(src), str(tmp_path / "out.txt"))
    assert report["pages"] == 4
    assert report["empty_pages"] == [1, 3]
    assert report["ocr_pages"] == []


def test_text_report_has_no_empty_pages_when_all_have_text(tmp_path):
    src = tmp_path / "src.pdf"
    src.write_bytes(_pdf(["a", "b"]))
    assert pdf_to_text_report(str(src), str(tmp_path / "out.txt"))["empty_pages"] == []


@pytest.mark.parametrize("pages, ocr_ready, note", [
    ([], True, ""),
    ([2], True, "Page 2 has no text layer. Turn on Use OCR for scanned pages to read them."),
    ([1, 3], True, "Pages 1 and 3 have no text layer. Turn on Use OCR for scanned pages to read them."),
    ([1, 2, 3, 7], True, "Pages 1-3 and 7 have no text layer. Turn on Use OCR for scanned pages to read them."),
    ([1, 4, 6, 7], True, "Pages 1, 4 and 6-7 have no text layer. Turn on Use OCR for scanned pages to read them."),
    ([1, 3], False, "Pages 1 and 3 have no text layer."),
])
def test_empty_pages_note(pages, ocr_ready, note):
    assert empty_pages_note(pages, ocr_ready) == note


def _export_text(client, data, **form):
    return client.post("/export/run", data={
        "operation": "text", "file": (io.BytesIO(data), "scan.pdf"), **form,
    }, content_type="multipart/form-data")


def test_export_says_which_pages_have_no_text_layer(client, monkeypatch):
    monkeypatch.setattr(export_routes, "ocr_is_available", lambda: True)
    resp = _export_text(client, _pdf(["", "words", ""]))
    assert resp.status_code == 200
    assert resp.get_json()["message"] == (
        "Extracted text from 3 page(s). Pages 1 and 3 have no text layer. "
        "Turn on Use OCR for scanned pages to read them.")


def test_export_without_ocr_on_the_server_leaves_out_the_ocr_advice(client, monkeypatch):
    monkeypatch.setattr(export_routes, "ocr_is_available", lambda: False)
    resp = _export_text(client, _pdf(["words", ""]))
    assert resp.get_json()["message"] == (
        "Extracted text from 2 page(s). Page 2 has no text layer.")


def test_export_with_text_on_every_page_adds_no_note(client):
    resp = _export_text(client, _pdf(["one", "two"]))
    assert resp.get_json()["message"] == "Extracted text from 2 page(s)."


# --------------------------------------------------------------------------
# Print: booklet count
# --------------------------------------------------------------------------
@pytest.mark.parametrize("pages, message", [
    (1, "Built a 4-page booklet: 2 sides on 1 sheet of paper."),
    (5, "Built an 8-page booklet: 4 sides on 2 sheets of paper."),
    (8, "Built an 8-page booklet: 4 sides on 2 sheets of paper."),
    (9, "Built a 12-page booklet: 6 sides on 3 sheets of paper."),
    (77, "Built an 80-page booklet: 40 sides on 20 sheets of paper."),
    (88, "Built an 88-page booklet: 44 sides on 22 sheets of paper."),
    (97, "Built a 100-page booklet: 50 sides on 25 sheets of paper."),
    (800, "Built an 800-page booklet: 400 sides on 200 sheets of paper."),
    (1100, "Built a 1100-page booklet: 550 sides on 275 sheets of paper."),
    (11000, "Built an 11000-page booklet: 5500 sides on 2750 sheets of paper."),
    (18000, "Built an 18000-page booklet: 9000 sides on 4500 sheets of paper."),
])
def test_booklet_message(pages, message):
    assert booklet_message(pages) == message


def test_print_booklet_route_reports_the_count(client):
    resp = client.post("/print/run", data={
        "operation": "booklet", "file": (io.BytesIO(_pdf(["a"] * 5)), "zine.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["message"].startswith(
        "Built an 8-page booklet: 4 sides on 2 sheets of paper.")


# --------------------------------------------------------------------------
# Page Tools: "1 page"
# --------------------------------------------------------------------------
def test_out_of_range_on_a_one_page_pdf_says_1_page():
    with pytest.raises(ValueError) as caught:
        parse_page_ranges("2", 1)
    assert "(the PDF has 1 page)." in str(caught.value)
    with pytest.raises(ValueError) as caught:
        parse_page_ranges("4", 3)
    assert "(the PDF has 3 pages)." in str(caught.value)


def test_pages_route_says_1_page(client):
    resp = client.post("/pages/run", data={
        "operation": "extract", "ranges": "2",
        "file": (io.BytesIO(_pdf(["only"])), "one.pdf"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "(the PDF has 1 page)." in resp.get_json()["error"]


# --------------------------------------------------------------------------
# Word: Title and Subtitle become h1 and h2
# --------------------------------------------------------------------------
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _styled_docx(path):
    """A .docx with a Title, a Subtitle and a body paragraph, and the
    styles.xml that names the two styles (mammoth maps by style name)."""
    def para(text, style=None):
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W}"><w:body>'
        + para("Quarterly Plan", "Title") + para("Second half", "Subtitle")
        + para("Body text.") + "<w:sectPr/></w:body></w:document>")
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:styles xmlns:w="{_W}">'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/></w:style>'
        "</w:styles>")
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>")
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/styles" Target="styles.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/_rels/document.xml.rels", doc_rels)
        zf.writestr("word/styles.xml", styles)
    return path


def test_docx_to_html_maps_title_and_subtitle(tmp_path):
    from transforms.documents import docx_to_html
    out = tmp_path / "out.html"
    docx_to_html(str(_styled_docx(tmp_path / "plan.docx")), str(out))
    html = out.read_text(encoding="utf-8")
    assert "<h1>Quarterly Plan</h1>" in html
    assert "<h2>Second half</h2>" in html
    assert "<p>Body text.</p>" in html


def test_docx_to_md_maps_title_and_subtitle(tmp_path):
    from transforms.documents import docx_to_md
    out = tmp_path / "out.md"
    docx_to_md(str(_styled_docx(tmp_path / "plan.docx")), str(out))
    text = out.read_text(encoding="utf-8")
    assert "# Quarterly Plan" in text
    assert "## Second half" in text


def test_docx_to_pdf_reflow_maps_title_and_subtitle(tmp_path, monkeypatch):
    import mammoth

    from converters import docx_converter
    seen = []
    real = mammoth.convert_to_html

    def spy(fileobj, **kwargs):
        result = real(fileobj, **kwargs)
        seen.append(result.value)
        return result

    monkeypatch.setattr(docx_converter.mammoth, "convert_to_html", spy)
    out = tmp_path / "out.pdf"
    docx_converter.convert(str(_styled_docx(tmp_path / "plan.docx")), str(out))
    assert out.read_bytes().startswith(b"%PDF-")
    assert "<h1>Quarterly Plan</h1>" in seen[0]
    assert "<h2>Second half</h2>" in seen[0]
