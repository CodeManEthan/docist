"""OCR languages (round prelaunch-fixes, design §4): the shared validator, the
per-request cap, OCRmyPDF's job count, the cached language probe and the
display names. None of these needs Tesseract installed; the real-pack checks
are at the bottom and skip without it.
"""
import io

import pytest

from pdf_ops import ocr as ocr_ops
from pdf_ops.ocr_langs import NAMES, language_choices, language_name


@pytest.fixture
def langs(monkeypatch):
    monkeypatch.setattr(ocr_ops, 'is_available', lambda: True)
    monkeypatch.setattr(ocr_ops, 'installed_languages',
                        lambda: ['eng', 'spa', 'fra', 'deu', 'chi_sim', 'osd'])
    monkeypatch.delenv('DOCIST_OCR_MAX_LANGS', raising=False)


# ------------------------------------------------------------- validator
def test_single_code(langs):
    assert ocr_ops.validate_language('spa') == 'spa'


def test_plus_spacing_and_empty_parts(langs):
    assert ocr_ops.validate_language(' eng + chi_sim ') == 'eng+chi_sim'
    assert ocr_ops.validate_language('eng++spa+') == 'eng+spa'


@pytest.mark.parametrize('spec', ['', '   ', '+', None])
def test_empty_spec_raises(langs, spec):
    with pytest.raises(ValueError, match='Provide a Tesseract language code'):
        ocr_ops.validate_language(spec)


def test_unknown_code_lists_installed(langs):
    with pytest.raises(ValueError) as exc:
        ocr_ops.validate_language('eng+zzz')
    assert "Unknown OCR language 'zzz'" in str(exc.value)
    assert 'spa' in str(exc.value)


def test_default_cap_allows_two_refuses_three(langs):
    assert ocr_ops.max_languages() == 2
    assert ocr_ops.validate_language('eng+spa') == 'eng+spa'
    with pytest.raises(ValueError, match='Choose at most 2 OCR languages.'):
        ocr_ops.validate_language('eng+spa+fra')


def test_cap_of_three_allows_three(langs, monkeypatch):
    monkeypatch.setenv('DOCIST_OCR_MAX_LANGS', '3')
    assert ocr_ops.validate_language('eng+spa+fra') == 'eng+spa+fra'
    with pytest.raises(ValueError, match='Choose at most 3 OCR languages.'):
        ocr_ops.validate_language('eng+spa+fra+deu')


def test_cap_of_one_message_is_singular(langs, monkeypatch):
    monkeypatch.setenv('DOCIST_OCR_MAX_LANGS', '1')
    with pytest.raises(ValueError, match=r'Choose at most 1 OCR language\.'):
        ocr_ops.validate_language('eng+spa')


@pytest.mark.parametrize('raw', ['0', '-1', 'two', ''])
def test_bad_cap_falls_back_to_default(langs, monkeypatch, raw):
    monkeypatch.setenv('DOCIST_OCR_MAX_LANGS', raw)
    assert ocr_ops.max_languages() == 2


def test_cap_checked_before_any_probe(monkeypatch):
    """Too many codes is refused without asking Tesseract anything."""
    monkeypatch.delenv('DOCIST_OCR_MAX_LANGS', raising=False)

    def boom():
        raise AssertionError('probed')
    monkeypatch.setattr(ocr_ops, 'installed_languages', boom)
    with pytest.raises(ValueError, match='at most 2'):
        ocr_ops.validate_language('a+b+c')


# ------------------------------------------------------------- jobs
def test_ocr_jobs_default_and_env(monkeypatch):
    monkeypatch.delenv('DOCIST_OCR_JOBS', raising=False)
    assert ocr_ops.ocr_jobs() == 2
    monkeypatch.setenv('DOCIST_OCR_JOBS', '1')
    assert ocr_ops.ocr_jobs() == 1


def test_make_searchable_passes_jobs(langs, monkeypatch, tmp_path):
    import ocrmypdf
    seen = {}

    def fake_ocr(input_path, output_path, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(ocrmypdf, 'ocr', fake_ocr)
    monkeypatch.setenv('DOCIST_OCR_JOBS', '3')
    ocr_ops.make_searchable(str(tmp_path / 'in.pdf'), str(tmp_path / 'out.pdf'),
                            language='eng+spa')
    assert seen['jobs'] == 3
    assert seen['language'] == 'eng+spa'

    monkeypatch.delenv('DOCIST_OCR_JOBS')
    ocr_ops.make_searchable(str(tmp_path / 'in.pdf'), str(tmp_path / 'out.pdf'))
    assert seen['jobs'] == 2


def test_make_searchable_uses_the_validator(langs, tmp_path):
    with pytest.raises(ValueError, match='at most 2'):
        ocr_ops.make_searchable(str(tmp_path / 'in.pdf'), str(tmp_path / 'out.pdf'),
                                language='eng+spa+fra')


# ------------------------------------------------------------- probe cache
def test_installed_languages_probes_once_per_process(monkeypatch):
    import pytesseract
    calls = []

    def fake_get_languages(*_a, **_k):
        calls.append(1)
        return ['eng', 'spa']

    monkeypatch.setattr(ocr_ops, 'is_available', lambda: True)
    monkeypatch.setattr(pytesseract, 'get_languages', fake_get_languages)
    ocr_ops._probe_languages.cache_clear()
    try:
        for _ in range(3):
            assert ocr_ops.installed_languages() == ['eng', 'spa']
        assert len(calls) == 1
    finally:
        ocr_ops._probe_languages.cache_clear()


def test_installed_languages_empty_without_tesseract(monkeypatch):
    monkeypatch.setattr(ocr_ops, 'is_available', lambda: False)
    assert ocr_ops.installed_languages() == []


# ------------------------------------------------------------- names
def test_every_dockerfile_pack_has_a_name():
    import pathlib
    import re
    dockerfile = (pathlib.Path(__file__).parents[2] / 'Dockerfile').read_text()
    packs = re.findall(r'tesseract-ocr-([a-z-]+)', dockerfile)
    codes = {p.replace('-', '_') for p in packs} | {'eng'}
    assert len(codes) == 14
    assert codes == set(NAMES)


def test_language_name_and_fallback():
    assert language_name('spa') == 'Spanish'
    assert language_name('chi_sim') == 'Chinese (Simplified)'
    assert language_name('xyz') == 'xyz'


def test_language_choices_english_first_and_no_osd():
    choices = language_choices(['spa', 'osd', 'deu', 'eng', 'xyz'])
    assert choices[0] == {'code': 'eng', 'name': 'English'}
    assert [c['code'] for c in choices] == ['eng', 'deu', 'spa', 'xyz']


# ------------------------------------------------------------- routes
def test_pages_and_export_show_language_selects(client, monkeypatch):
    from routes import export as export_routes
    from routes import pages as pages_routes
    monkeypatch.setattr(pages_routes.ocr_ops, 'is_available', lambda: True)
    monkeypatch.setattr(pages_routes.ocr_ops, 'installed_languages', lambda: ['eng', 'spa'])
    monkeypatch.setattr(export_routes, 'ocr_is_available', lambda: True)
    monkeypatch.setattr(export_routes, 'installed_languages', lambda: ['eng', 'spa'])
    for url in ('/pages', '/export'):
        html = client.get(url).get_data(as_text=True)
        assert '<select id="ocrLanguage"' in html, url
        assert '<select id="ocrLanguage2"' in html, url
        assert '<option value="spa">Spanish</option>' in html, url
        assert '/static/ocr-langs.js' in html, url


def test_export_without_ocr_never_reads_language(client, builders, tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError('language was validated')
    monkeypatch.setattr(ocr_ops, 'validate_language', boom)
    src = builders.pdf(tmp_path / 'in.pdf', pages=1)
    resp = client.post('/export/run', data={
        'operation': 'text', 'language': 'zzz+qqq+xxx',
        'file': (io.BytesIO(src.read_bytes()), 'in.pdf'),
    }, content_type='multipart/form-data')
    assert resp.status_code == 200, resp.get_json()


# ------------------------------------------------------------- real packs
requires_spanish = pytest.mark.skipif(
    'spa' not in ocr_ops.installed_languages(),
    reason='needs Tesseract with the Spanish pack')


@requires_spanish
def test_real_validator_accepts_installed_pair(monkeypatch):
    monkeypatch.delenv('DOCIST_OCR_MAX_LANGS', raising=False)
    assert ocr_ops.validate_language('eng+spa') == 'eng+spa'


@requires_spanish
def test_export_refuses_three_languages(client, builders, tmp_path, monkeypatch):
    monkeypatch.delenv('DOCIST_OCR_MAX_LANGS', raising=False)
    src = builders.pdf(tmp_path / 'in.pdf', pages=1)
    resp = client.post('/export/run', data={
        'operation': 'text', 'ocr_fallback': 'true', 'language': 'eng+spa+eng',
        'file': (io.BytesIO(src.read_bytes()), 'in.pdf'),
    }, content_type='multipart/form-data')
    assert resp.status_code == 400
    assert 'at most 2' in resp.get_json()['error']


def test_failed_probe_is_not_cached(monkeypatch):
    """A transient probe failure must not stick for the worker's life."""
    import pytesseract
    calls = []

    def flaky(*_a, **_k):
        calls.append(1)
        if len(calls) == 1:
            raise OSError('fork refused')
        return ['eng', 'spa']

    monkeypatch.setattr(ocr_ops, 'is_available', lambda: True)
    monkeypatch.setattr(pytesseract, 'get_languages', flaky)
    ocr_ops._probe_languages.cache_clear()
    try:
        assert ocr_ops.installed_languages() == []
        assert ocr_ops.installed_languages() == ['eng', 'spa']
        assert ocr_ops.installed_languages() == ['eng', 'spa']
        assert len(calls) == 2
        assert ocr_ops.validate_language('eng') == 'eng'
    finally:
        ocr_ops._probe_languages.cache_clear()
