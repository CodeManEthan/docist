"""Tests for transforms/documents.py -- document-to-document transforms.

Fixtures are built programmatically into ``tmp_path``. The hand-zipped OOXML
``.docx`` builder from conftest is reused via the ``builders`` fixture.
"""
import zipfile

import pytest

import transforms
from transforms import TransformError
import transforms.documents as docs


# --------------------------------------------------------------------------
# Local fixture builders (formats conftest doesn't already provide)
# --------------------------------------------------------------------------
def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def build_rtf(path, body="RtfMarkerOmega bold word"):
    """A minimal valid RTF document with control words around the text."""
    rtf = (
        r"{\rtf1\ansi\deff0"
        r"{\fonttbl{\f0 Helvetica;}}"
        r"\f0\fs24 " + body + r"\par}"
    )
    return _write(path, rtf)


# --------------------------------------------------------------------------
# Markdown -> HTML
# --------------------------------------------------------------------------
def test_md_to_html_renders_tags(tmp_path, builders):
    src = builders.markdown(tmp_path / "in.md")
    out = tmp_path / "out.html"
    docs.md_to_html(str(src), str(out))
    html = out.read_text(encoding="utf-8")

    assert "<h1" in html
    assert builders.MD_HEADING in html
    assert "<table" in html
    assert builders.MD_TABLE_CELL in html
    assert "<code" in html
    assert builders.MD_CODE in html
    # standalone document with embedded CSS
    assert "<!DOCTYPE html>" in html
    assert "<style>" in html


def test_markdown_ext_alias_to_html(tmp_path, builders):
    src = builders.markdown(tmp_path / "in.markdown")
    out = tmp_path / "out.html"
    docs.md_to_html(str(src), str(out))
    assert "<h1" in out.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Markdown -> Text (strips syntax)
# --------------------------------------------------------------------------
def test_md_to_txt_strips_markdown_syntax(tmp_path, builders):
    src = builders.markdown(tmp_path / "in.md")
    out = tmp_path / "out.txt"
    docs.md_to_txt(str(src), str(out))
    text = out.read_text(encoding="utf-8")

    # content survives
    assert builders.MD_HEADING in text
    assert builders.MD_PARAGRAPH in text
    # markdown/HTML syntax is gone
    assert "#" not in text
    assert "**" not in text
    assert "<h1" not in text
    assert "```" not in text


# --------------------------------------------------------------------------
# HTML -> Markdown
# --------------------------------------------------------------------------
def test_html_to_md_has_markdown_syntax_no_tags(tmp_path, builders):
    src = builders.html(tmp_path / "in.html")
    out = tmp_path / "out.md"
    docs.html_to_md(str(src), str(out))
    md = out.read_text(encoding="utf-8")

    assert builders.HTML_HEADING in md
    assert builders.HTML_BODY in md
    assert "#" in md           # heading became a markdown heading
    assert "**" in md          # <strong> became **bold**
    assert "<h1" not in md
    assert "<strong" not in md
    assert "<p>" not in md


def test_htm_alias_to_md(tmp_path, builders):
    src = builders.html(tmp_path / "in.htm")
    out = tmp_path / "out.md"
    docs.html_to_md(str(src), str(out))
    assert "#" in out.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# HTML -> Text (clean)
# --------------------------------------------------------------------------
def test_html_to_txt_clean(tmp_path, builders):
    src = builders.html(tmp_path / "in.html")
    out = tmp_path / "out.txt"
    docs.html_to_txt(str(src), str(out))
    text = out.read_text(encoding="utf-8")

    assert builders.HTML_HEADING in text
    assert builders.HTML_BODY in text
    # no tags and no markdown emphasis markers
    assert "<" not in text
    assert ">" not in text
    assert "**" not in text
    assert "#" not in text


# --------------------------------------------------------------------------
# DOCX -> HTML / Markdown / Text
# --------------------------------------------------------------------------
def test_docx_to_html(tmp_path, builders):
    src = builders.docx(tmp_path / "in.docx")
    out = tmp_path / "out.html"
    docs.docx_to_html(str(src), str(out))
    html = out.read_text(encoding="utf-8")

    assert "<!DOCTYPE html>" in html
    assert builders.DOCX_HEADING in html
    assert builders.DOCX_BODY in html
    assert "<strong>" in html   # bold run -> <strong>


def test_docx_to_md(tmp_path, builders):
    src = builders.docx(tmp_path / "in.docx")
    out = tmp_path / "out.md"
    docs.docx_to_md(str(src), str(out))
    md = out.read_text(encoding="utf-8")

    assert builders.DOCX_HEADING in md
    assert builders.DOCX_BODY in md
    # mammoth marks bold runs with markdown emphasis (** or __)
    assert ("**" in md) or ("__" in md)
    assert "<strong" not in md
    assert "<p>" not in md


def test_docx_to_txt(tmp_path, builders):
    src = builders.docx(tmp_path / "in.docx")
    out = tmp_path / "out.txt"
    docs.docx_to_txt(str(src), str(out))
    text = out.read_text(encoding="utf-8")

    assert builders.DOCX_HEADING in text
    assert builders.DOCX_BODY in text
    assert builders.DOCX_SECOND in text
    assert "<" not in text
    assert "**" not in text


# --------------------------------------------------------------------------
# RTF -> Text
# --------------------------------------------------------------------------
def test_rtf_to_txt_strips_control_words(tmp_path):
    src = build_rtf(tmp_path / "in.rtf")
    out = tmp_path / "out.txt"
    docs.rtf_to_txt(str(src), str(out))
    text = out.read_text(encoding="utf-8")

    assert "RtfMarkerOmega" in text
    # control words / groups removed
    assert "\\rtf" not in text
    assert "\\par" not in text
    assert "fonttbl" not in text
    assert "{" not in text


# --------------------------------------------------------------------------
# Error cases
# --------------------------------------------------------------------------
def test_corrupt_docx_raises(tmp_path):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"this is not a zip / docx at all")
    with pytest.raises(TransformError):
        docs.docx_to_html(str(bad), str(tmp_path / "out.html"))


def test_corrupt_docx_raises_for_md_and_txt(tmp_path):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"PK\x03\x04 not really a docx")
    with pytest.raises(TransformError):
        docs.docx_to_md(str(bad), str(tmp_path / "out.md"))
    with pytest.raises(TransformError):
        docs.docx_to_txt(str(bad), str(tmp_path / "out.txt"))


def test_non_rtf_raises(tmp_path):
    notrtf = tmp_path / "plain.rtf"
    notrtf.write_text("just plain text, no rtf header", encoding="utf-8")
    with pytest.raises(TransformError):
        docs.rtf_to_txt(str(notrtf), str(tmp_path / "out.txt"))


# --------------------------------------------------------------------------
# Encoding: latin-1 fallback on read
# --------------------------------------------------------------------------
def test_latin1_fallback_on_read(tmp_path):
    src = tmp_path / "latin.html"
    # 0xE9 is 'é' in latin-1 but invalid as standalone UTF-8
    src.write_bytes(b"<html><body><p>caf\xe9 text</p></body></html>")
    out = tmp_path / "out.txt"
    docs.html_to_txt(str(src), str(out))
    text = out.read_text(encoding="utf-8")
    assert "caf" in text  # decoded without raising


# --------------------------------------------------------------------------
# Registry integration
# --------------------------------------------------------------------------
def test_get_transform_md_html_not_none():
    assert transforms.get_transform(".md", ".html") is not None
    assert transforms.get_transform(".markdown", ".html") is not None
    assert transforms.get_transform(".html", ".md") is not None
    assert transforms.get_transform(".htm", ".txt") is not None
    assert transforms.get_transform(".rtf", ".txt") is not None


def test_targets_for_docx_includes_document_targets():
    targets = transforms.targets_for(".docx")
    assert ".html" in targets
    assert ".md" in targets
    assert ".txt" in targets


def test_get_transform_runs_end_to_end(tmp_path, builders):
    src = builders.markdown(tmp_path / "in.md")
    out = tmp_path / "out.html"
    func = transforms.get_transform(".md", ".html")
    actual = func(str(src), str(out))
    assert actual == str(out)
    assert "<h1" in out.read_text(encoding="utf-8")
