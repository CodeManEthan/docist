"""LibreOffice and OCR inside a job at the default caps (design launch-hardening
v0.4 §3.4, §15). Skipped where the binaries aren't installed."""
import os
import time

import pytest
from pypdf import PdfReader

import converters
from converters.options import RenderOptions
from pdf_ops import jobs, office
from pdf_ops import ocr as ocr_ops

import word_fixtures as wf


@pytest.mark.skipif(not office.available(), reason='LibreOffice not installed')
@pytest.mark.parametrize('build', [wf.letter_1page, wf.a4_header_footer, wf.calibri_cambria])
def test_the_word_engine_converts_b2_fixtures_in_a_job(tmp_path, build):
    src = build(tmp_path / 'in.docx')
    out = tmp_path / 'out.pdf'
    opts = RenderOptions(word_engine='libreoffice', deadline=time.monotonic() + 90)

    def work():
        converters.get_converter('.docx')(str(src), str(out), opts)
        return list(opts.notes)

    notes = jobs.run_job(work, deadline=opts.deadline, job_dir=str(tmp_path / 'job'))
    assert notes == []          # LibreOffice did it, no fallback to the reflow
    assert len(PdfReader(str(out)).pages) >= 1
    assert not (tmp_path / 'job').exists()


@pytest.mark.skipif(not ocr_ops.is_available(), reason='OCR binaries not installed')
def test_ocr_runs_in_a_job_with_real_tesseract(tmp_path):
    from PIL import Image, ImageDraw
    src = tmp_path / 'scan.pdf'

    def build():
        img = Image.new('L', (1700, 600), 255)
        ImageDraw.Draw(img).text((100, 200), 'HELLO JOBS', fill=0, font_size=120)
        img.save(src, 'PDF', resolution=200.0)

    jobs.run_job(build, job_dir=str(tmp_path / 'build'))
    out = tmp_path / 'out.pdf'
    stats = jobs.run_job(lambda: ocr_ops.make_searchable(str(src), str(out), language='eng'),
                         job_dir=str(tmp_path / 'job'))
    assert stats['pages'] == 1
    text = PdfReader(str(out)).pages[0].extract_text()
    assert 'HELLO' in text.upper()
    assert not (tmp_path / 'job').exists()
