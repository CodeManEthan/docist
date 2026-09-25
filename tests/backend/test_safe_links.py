"""H2: uploaded HTML/Markdown/DOCX must not make xhtml2pdf fetch anything.

xhtml2pdf resolves <img>, <link>, @import, @font-face and CSS url() through
three loaders: NetworkFileUri (http/https), LocalProtocolURI (file://) and
LocalFileURI (any other string, treated as a path). The tests replace all
three with recorders, render hostile documents, and require zero calls.
A control test proves the recorders fire when the guard is absent.
"""
import pytest
from xhtml2pdf import files as xfiles
from xhtml2pdf import pisa

from converters import html_converter, markdown_converter
from converters.safe_links import BLOCKED, link_callback

HOSTILE_REFS = [
    "http://127.0.0.1:9/probe.png",
    "https://169.254.169.254/latest/meta-data/",
    "file:///etc/hostname",
    "/etc/hostname",
    "../../app.py",
]


def _hostile_html(ref):
    return f"""<html><head>
<link rel="stylesheet" href="{ref}">
<style>
@import url("{ref}");
@font-face {{ font-family: x; src: url("{ref}"); }}
body {{ background-image: url("{ref}"); }}
</style></head>
<body><p>Hello</p><img src="{ref}"></body></html>"""


@pytest.fixture
def fetches(monkeypatch):
    calls = []
    for cls in (xfiles.NetworkFileUri, xfiles.LocalProtocolURI, xfiles.LocalFileURI):
        def record(self, _cls=cls):
            calls.append((_cls.__name__, self.path))
            return None
        monkeypatch.setattr(cls, "extract_data", record)
    return calls


def test_callback_passes_data_uris():
    uri = "data:image/png;base64,AAAA"
    assert link_callback(uri, None) == uri


@pytest.mark.parametrize("ref", HOSTILE_REFS)
def test_callback_blocks_everything_else(ref):
    assert link_callback(ref, None) == BLOCKED


def test_control_unguarded_pisa_does_fetch(fetches, tmp_path):
    # Without link_callback the recorders fire; otherwise the tests below
    # would pass vacuously.
    with open(tmp_path / "out.pdf", "wb") as out:
        pisa.CreatePDF(src=_hostile_html("/etc/hostname"), dest=out)
    assert fetches


@pytest.mark.parametrize("ref", HOSTILE_REFS)
def test_html_upload_fetches_nothing(fetches, tmp_path, ref):
    src = tmp_path / "in.html"
    src.write_text(_hostile_html(ref))
    out = tmp_path / "out.pdf"
    html_converter.convert(str(src), str(out))
    assert fetches == []
    assert out.stat().st_size > 0


@pytest.mark.parametrize("ref", HOSTILE_REFS)
def test_markdown_raw_html_fetches_nothing(fetches, tmp_path, ref):
    src = tmp_path / "in.md"
    src.write_text(f"# Title\n\n![pic]({ref})\n\n<img src=\"{ref}\">\n\n"
                   f"<link rel=\"stylesheet\" href=\"{ref}\">\n")
    out = tmp_path / "out.pdf"
    markdown_converter.convert(str(src), str(out))
    assert fetches == []
    assert out.stat().st_size > 0
