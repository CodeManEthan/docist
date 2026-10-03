"""RenderOptions and utils.render_opts.from_form (round prelaunch-fixes, design §2).

from_form validates a field only on a path that consumes it: ``paper`` when
``paper=True`` (the default), ``language`` only with ``ocr=True``. Outside a
request stamped by ``utils.uploads.apply_limit``, ``deadline`` stays ``None``
(B2 sets it inside one; tests/backend/test_office.py covers that).
"""
import pytest
from werkzeug.datastructures import MultiDict

import converters
import transforms
from converters.options import PAPER_SIZES, RenderOptions, paper_css, paper_size
from pdf_ops import ocr as ocr_ops
from utils import render_opts
from utils.render_opts import RenderOptionsError, from_form, notes_header, with_notes
from pdf_ops import office


@pytest.fixture
def langs(monkeypatch):
    """Pretend Tesseract is installed with eng, spa, fra and deu."""
    monkeypatch.setattr(ocr_ops, 'is_available', lambda: True)
    monkeypatch.setattr(ocr_ops, 'installed_languages',
                        lambda: ['eng', 'spa', 'fra', 'deu', 'osd'])
    monkeypatch.delenv('DOCIST_OCR_MAX_LANGS', raising=False)


@pytest.fixture
def no_probe(monkeypatch):
    """Fail the test if anything asks Tesseract for its languages."""
    def boom(*_a, **_k):
        raise AssertionError('Tesseract was probed')
    monkeypatch.setattr(ocr_ops, 'installed_languages', boom)
    monkeypatch.setattr(ocr_ops, 'validate_language', boom)
    monkeypatch.setattr(ocr_ops, '_probe_languages', boom)


# ---------------------------------------------------------------- defaults
def test_render_options_defaults():
    opts = RenderOptions()
    assert opts.paper == 'letter'
    assert opts.ocr_language == 'eng'
    assert opts.word_engine == 'reflow'
    assert opts.deadline is None
    assert opts.notes == []
    assert RenderOptions().notes is not opts.notes  # no shared list


def test_paper_helpers():
    assert paper_size(None) == (612.0, 792.0)
    assert paper_size(RenderOptions(paper='a4')) == PAPER_SIZES['a4']
    assert paper_size(RenderOptions(paper='a4'), landscape=True) == (841.89, 595.276)
    assert paper_css(None) == 'letter'
    assert paper_css(RenderOptions(paper='a4')) == 'A4'


def test_from_form_defaults(no_probe):
    opts = from_form(MultiDict())
    assert opts == RenderOptions()


def test_deadline_is_none_outside_a_request(langs):
    """No request, no g.request_started: from_form sets no deadline."""
    assert from_form(MultiDict({'paper': 'a4'})).deadline is None
    assert from_form(MultiDict({'language': 'spa'}), ocr=True).deadline is None


# ------------------------------------------------------------------- paper
@pytest.mark.parametrize('raw, expected', [
    ('letter', 'letter'), ('a4', 'a4'), ('A4', 'a4'), (' Letter ', 'letter'),
    ('', 'letter'), (None, 'letter'),
])
def test_paper_values(raw, expected):
    form = MultiDict() if raw is None else MultiDict({'paper': raw})
    assert from_form(form).paper == expected


@pytest.mark.parametrize('raw', ['legal', 'a5', 'letter ; a4', '612x792'])
def test_bad_paper_raises(raw):
    with pytest.raises(RenderOptionsError, match='Paper must be letter or a4.'):
        from_form(MultiDict({'paper': raw}))


def test_paper_not_read_when_not_consumed():
    opts = from_form(MultiDict({'paper': 'legal'}), paper=False)
    assert opts.paper == 'letter'


def test_render_options_error_is_value_error():
    # Routes that already map ValueError to a 400 keep working.
    assert issubclass(RenderOptionsError, ValueError)


# ---------------------------------------------------------------- language
def test_language_ignored_without_ocr(no_probe):
    """A Merge-style form never reads `language` and never probes Tesseract."""
    opts = from_form(MultiDict({'paper': 'a4', 'language': 'zzz+qqq+xxx'}))
    assert opts.paper == 'a4'
    assert opts.ocr_language == 'eng'


def test_merge_form_works_with_no_languages(monkeypatch):
    monkeypatch.setattr(ocr_ops, 'installed_languages', lambda: [])
    opts = from_form(MultiDict({'paper': 'letter', 'page_numbers': 'true'}))
    assert opts.paper == 'letter'


def test_missing_language_defaults_without_probe(no_probe):
    assert from_form(MultiDict(), ocr=True).ocr_language == 'eng'
    assert from_form(MultiDict({'language': '  '}), ocr=True).ocr_language == 'eng'


def test_language_validated_with_ocr(langs):
    assert from_form(MultiDict({'language': 'spa'}), ocr=True).ocr_language == 'spa'


def test_language_plus_spacing_normalised(langs):
    opts = from_form(MultiDict({'language': ' eng + spa '}), ocr=True)
    assert opts.ocr_language == 'eng+spa'


def test_unknown_language_raises(langs):
    with pytest.raises(RenderOptionsError, match="Unknown OCR language 'zzz'"):
        from_form(MultiDict({'language': 'zzz'}), ocr=True)


def test_default_cap_refuses_three_allows_two(langs):
    assert from_form(MultiDict({'language': 'eng+spa'}), ocr=True).ocr_language == 'eng+spa'
    with pytest.raises(RenderOptionsError, match='Choose at most 2 OCR languages.'):
        from_form(MultiDict({'language': 'eng+spa+fra'}), ocr=True)


def test_cap_of_three_allows_three(langs, monkeypatch):
    monkeypatch.setenv('DOCIST_OCR_MAX_LANGS', '3')
    opts = from_form(MultiDict({'language': 'eng+spa+fra'}), ocr=True)
    assert opts.ocr_language == 'eng+spa+fra'
    with pytest.raises(RenderOptionsError, match='Choose at most 3 OCR languages.'):
        from_form(MultiDict({'language': 'eng+spa+fra+deu'}), ocr=True)


def test_language_unchecked_when_tesseract_missing(monkeypatch):
    """The renderer's own 'OCR requires Tesseract' message must reach the user."""
    monkeypatch.setattr(ocr_ops, 'is_available', lambda: False)
    assert from_form(MultiDict({'language': 'spa'}), ocr=True).ocr_language == 'spa'


def test_language_is_bounded_when_tesseract_missing(monkeypatch):
    class UnstrippedLanguage(str):
        def strip(self, *_args, **_kwargs):
            raise AssertionError('language was stripped')

    monkeypatch.setattr(ocr_ops, 'is_available', lambda: False)
    raw = UnstrippedLanguage('eng+' * (2 * 1024 * 1024))
    with pytest.raises(RenderOptionsError, match='Choose at most 2 OCR languages.'):
        from_form(MultiDict({'language': raw}), ocr=True)


# ------------------------------------------------------------------- notes
def test_with_notes():
    opts = RenderOptions()
    assert with_notes('Done.', opts) == 'Done.'
    opts.notes.extend(['First note.', 'Second note.'])
    assert with_notes('Done.', opts) == 'Done. First note. Second note.'


def test_notes_header_is_ascii_one_line():
    from flask import Response
    opts = RenderOptions()
    resp = notes_header(Response(), opts)
    assert render_opts.NOTES_HEADER not in resp.headers
    opts.notes.extend(['Converted with the basic\r\nconverter.', 'café'])
    resp = notes_header(Response(), opts)
    value = resp.headers[render_opts.NOTES_HEADER]
    assert value == 'Converted with the basic converter. caf?'
    value.encode('ascii')


# ---------------------------------------------------------------- registry
def test_converter_without_opts_is_wrapped():
    calls = []

    def old_style(i, o):
        calls.append((i, o))

    wrapped = converters.with_opts(old_style)
    wrapped('in', 'out', RenderOptions(paper='a4'))
    wrapped('in2', 'out2')
    assert calls == [('in', 'out'), ('in2', 'out2')]


def test_converter_with_opts_is_unwrapped():
    def new_style(i, o, opts=None):
        return opts
    assert converters.with_opts(new_style) is new_style


def test_every_registered_converter_takes_opts(tmp_path):
    for ext in converters.supported_extensions():
        assert converters.accepts_opts(converters.get_converter(ext)), ext


def test_direct_transform_takes_opts(tmp_path, monkeypatch):
    seen = []

    def direct(i, o, opts=None):
        seen.append(opts)
        open(o, 'w').close()

    monkeypatch.setattr(transforms, '_DIRECT', {('.aaa', '.bbb'): direct})
    opts = RenderOptions(paper='a4')
    transforms.get_transform('.aaa', '.bbb')(str(tmp_path / 'i'), str(tmp_path / 'o.bbb'), opts)
    transforms.get_transform('.aaa', '.bbb')(str(tmp_path / 'i'), str(tmp_path / 'o.bbb'))
    assert seen == [opts, None]


def test_pivot_passes_same_opts_to_both_halves(tmp_path, monkeypatch):
    seen = []

    def to_pdf(i, o, opts=None):
        seen.append(('to', opts))
        open(o, 'w').close()

    def from_pdf(i, o, opts=None):
        seen.append(('from', opts))
        open(o, 'w').close()

    monkeypatch.setattr(transforms, '_DIRECT', {
        ('.aaa', '.pdf'): to_pdf, ('.pdf', '.bbb'): from_pdf,
    })
    opts = RenderOptions(paper='a4')
    transforms.get_transform('.aaa', '.bbb')(str(tmp_path / 'i'), str(tmp_path / 'o.bbb'), opts)
    assert seen == [('to', opts), ('from', opts)]


def test_route_helpers():
    assert transforms.renders_pages('.md', '.pdf')
    assert transforms.renders_pages('.md', '.png')       # pivots through PDF
    assert not transforms.renders_pages('.pdf', '.png')  # direct, no layout
    assert not transforms.renders_pages('.png', '.jpg')  # direct image transform
    assert not transforms.renders_pages('.png', '.txt')  # OCR
    assert transforms.uses_ocr('.png', '.txt')
    assert transforms.uses_ocr('.JPG', '.TXT')
    assert not transforms.uses_ocr('.pdf', '.txt')
    assert not transforms.uses_ocr('.md', '.txt')


def test_keyword_only_opts_is_wrapped():
    seen = []

    def kw_only(i, o, *, opts=None):
        seen.append(opts)

    assert not converters.accepts_opts(kw_only)
    opts = RenderOptions(paper='a4')
    converters.with_opts(kw_only)('in', 'out', opts)
    assert seen == [opts]


class _User:
    def __init__(self, verified, paid):
        self.is_verified = verified
        self.is_paid = paid


@pytest.mark.parametrize('user,installed,engine', [
    (None, True, 'reflow'),
    (_User(False, True), True, 'reflow'),     # unverified, even on a paid plan
    (_User(True, False), True, 'reflow'),
    (_User(True, True), True, 'libreoffice'),
    (_User(True, True), False, 'reflow'),     # paid, but LibreOffice missing
])
def test_word_engine_follows_the_tier(monkeypatch, no_probe, user, installed, engine):
    monkeypatch.setattr(office, 'available', lambda: installed)
    assert from_form(MultiDict(), user).word_engine == engine
