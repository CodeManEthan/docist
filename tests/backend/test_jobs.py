"""pdf_ops.jobs: one test per invariant J1 to J7 (design launch-hardening v0.4
§3.1, §15). Limits are patched low; nothing here allocates more than a few
tens of MiB or runs longer than a few seconds.

After each, the worker's own VmData is unchanged and the next job succeeds.
"""
import errno
import logging
import os
import pickle
import resource
import signal
import subprocess
import sys
import tempfile
import time

import pytest

from converters import ConversionError
from pdf_ops import jobs, limits
from pdf_ops.limits import LimitError
from pdf_ops.merge import OptionsError
from transforms import TransformError

MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def small_caps(monkeypatch):
    monkeypatch.setattr(limits, 'JOB_MEM_BYTES', 64 * MIB)
    monkeypatch.setattr(limits, 'RESULT_BYTES', 1 * MIB)


@pytest.fixture
def worker_unchanged():
    """The worker's VmData doesn't grow and the next job still runs."""
    before = jobs.vm_data_bytes()
    yield
    after = jobs.vm_data_bytes()
    if before is not None and after is not None:
        assert after - before < 16 * MIB
    with tempfile.TemporaryDirectory() as tmp:
        assert jobs.run_job(lambda: 'next', job_dir=os.path.join(tmp, 'job')) == 'next'


def _run(tmp_path, work, name='job', **kw):
    kw.setdefault('deadline', time.monotonic() + 30)
    return jobs.run_job(work, job_dir=str(tmp_path / name), **kw)


def _group_members(pgid):
    members = []
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            with open(f'/proc/{entry}/stat') as fh:
                stat = fh.read()
        except OSError:
            continue
        fields = stat.rsplit(')', 1)[1].split()
        state, pgrp = fields[0], int(fields[2])
        if pgrp == pgid and state != 'Z':
            members.append(int(entry))
    return members


def _wait_gone(pgid, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not _group_members(pgid):
            return True
        time.sleep(0.05)
    return False


def _alive(pid):
    try:
        with open(f'/proc/{pid}/stat') as fh:
            return fh.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except OSError:
        return False


# ---------------------------------------------------------------------------
# J1, every process is capped
# ---------------------------------------------------------------------------
def test_j1_a_grandchild_carries_the_jobs_caps(tmp_path, worker_unchanged):
    probe = ('import resource;'
             'print(resource.getrlimit(resource.RLIMIT_DATA)[0],'
             ' resource.getrlimit(resource.RLIMIT_FSIZE)[0])')

    def work():
        out = subprocess.run([sys.executable, '-c', probe], check=True,
                             capture_output=True, text=True).stdout.split()
        data_soft, _ = resource.getrlimit(resource.RLIMIT_DATA)
        fsize = resource.getrlimit(resource.RLIMIT_FSIZE)
        return int(out[0]), int(out[1]), data_soft, fsize, jobs.vm_data_bytes()

    grand_data, grand_fsize, data_soft, fsize, vm_now = _run(tmp_path, work)
    assert grand_data == data_soft
    assert grand_fsize == limits.RESULT_BYTES
    assert fsize == (limits.RESULT_BYTES, limits.RESULT_BYTES)
    # Soft is the child's VmData when it started plus JOB_MEM_BYTES.
    assert 0 < data_soft - limits.JOB_MEM_BYTES <= vm_now


def test_j1_a_write_past_the_file_cap_is_the_file_refusal(tmp_path, worker_unchanged):
    target = tmp_path / 'big.bin'

    def work():
        with open(target, 'wb') as fh:
            fh.write(b'x' * (2 * MIB))

    with pytest.raises(LimitError) as info:
        _run(tmp_path, work)
    assert info.value.kind == 'file'
    assert str(info.value) == limits.file_message()


def test_j1_memory_past_the_cap_is_the_memory_refusal(tmp_path, worker_unchanged):
    with pytest.raises(LimitError) as info:
        _run(tmp_path, lambda: bytearray(256 * MIB))
    assert info.value.kind == 'memory'
    assert str(info.value) == limits.memory_message()


# ---------------------------------------------------------------------------
# J2, the child decides what happened
# ---------------------------------------------------------------------------
def test_j2_memory_wrapped_in_a_conversion_error_is_the_memory_refusal(tmp_path,
                                                                     worker_unchanged):
    def work():
        try:
            bytearray(256 * MIB)
        except MemoryError as exc:
            raise ConversionError('Failed to render PDF from Markdown: ') from exc

    with pytest.raises(LimitError) as info:
        _run(tmp_path, work)
    assert info.value.kind == 'memory'


def test_j2_efbig_wrapped_twice_is_the_file_refusal(tmp_path, worker_unchanged):
    def work():
        try:
            try:
                raise OSError(errno.EFBIG, 'File too large')
            except OSError as exc:
                raise ConversionError('could not write') from exc
        except ConversionError as exc:
            raise TransformError('Could not convert') from exc

    with pytest.raises(LimitError) as info:
        _run(tmp_path, work)
    assert info.value.kind == 'file'


def test_j2_a_limit_error_inside_a_wrapper_keeps_its_message(tmp_path, worker_unchanged):
    def work():
        try:
            limits.check_frame(10_000, 10_000)
        except LimitError as exc:
            raise ConversionError('Could not process image') from exc

    with pytest.raises(LimitError) as info:
        _run(tmp_path, work)
    assert info.value.kind == 'frame'
    assert str(info.value) == limits.image_message()


@pytest.mark.parametrize('exc', [
    TransformError('bad transform'),
    OptionsError('bad options'),
    ValueError('bad value'),
])
def test_j2_ordinary_errors_reraise_as_themselves(tmp_path, worker_unchanged, exc):
    def work():
        raise exc

    with pytest.raises(type(exc)) as info:
        _run(tmp_path, work)
    assert type(info.value) is type(exc)
    assert str(info.value) == str(exc)
    # The parent never sees a chain: pickling drops it.
    assert info.value.__cause__ is None


class _WontPickle(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.handle = lambda: None   # a lambda can't be pickled


class _WontUnpickle(Exception):
    def __init__(self, first, second):
        super().__init__(f'{first} and {second}')


def test_j2_an_exception_that_wont_pickle_becomes_runtime_error(tmp_path, worker_unchanged):
    def work():
        raise _WontPickle('no pickle')

    with pytest.raises(RuntimeError, match='_WontPickle: no pickle'):
        _run(tmp_path, work)


def test_j2_an_exception_that_wont_unpickle_becomes_runtime_error(tmp_path,
                                                                 worker_unchanged):
    with pytest.raises(TypeError):
        pickle.loads(pickle.dumps(_WontUnpickle('a', 'b')))

    def work():
        raise _WontUnpickle('a', 'b')

    with pytest.raises(RuntimeError, match='_WontUnpickle: a and b'):
        _run(tmp_path, work)


def test_j2_a_child_killed_by_a_signal_is_the_couldnt_convert_refusal(tmp_path,
                                                                     worker_unchanged):
    with pytest.raises(LimitError) as info:
        _run(tmp_path, lambda: os.kill(os.getpid(), signal.SIGKILL))
    assert info.value.kind == 'died'
    assert str(info.value) == limits.died_message()
    assert info.value.args[0].startswith("Docist couldn't convert this file.")


# ---------------------------------------------------------------------------
# J3, the outcome can always be sent
# ---------------------------------------------------------------------------
def test_j3_a_job_full_of_small_allocations_still_sends_the_memory_outcome(
        tmp_path, worker_unchanged):
    def work():
        hoard = []
        while True:
            hoard.append(b'%d' % len(hoard) * 64)

    with pytest.raises(LimitError) as info:
        _run(tmp_path, work)
    assert info.value.kind == 'memory'


def test_j3_a_job_at_its_cap_that_raises_something_else_still_reports_it(
        tmp_path, worker_unchanged):
    def work():
        hoard = []
        try:
            while True:
                hoard.append(b'%d' % len(hoard) * 64)
        except MemoryError:
            pass
        # hoard is still alive in this frame while the error is raised.
        raise TransformError(f'gave up after {len(hoard)} blocks')

    with pytest.raises(TransformError, match='gave up after'):
        _run(tmp_path, work)


# ---------------------------------------------------------------------------
# J4, nothing outlives the job
# ---------------------------------------------------------------------------
def test_j4_a_job_past_its_deadline_leaves_no_process_in_its_group(tmp_path,
                                                                  worker_unchanged):
    pids = tmp_path / 'pids'

    def work():
        sleeper = subprocess.Popen(['sleep', '600'])
        pids.write_text(f'{os.getpid()} {sleeper.pid}')
        time.sleep(600)

    started = time.monotonic()
    with pytest.raises(LimitError) as info:
        _run(tmp_path, work, deadline=time.monotonic() + 1.5, budget=7)
    assert time.monotonic() - started < 10
    assert info.value.kind == 'time'
    assert str(info.value) == limits.time_message(7)
    assert '(7 seconds, counting the upload)' in str(info.value)
    child, sleeper = (int(p) for p in pids.read_text().split())
    assert _wait_gone(child)
    assert not _alive(sleeper)
    assert not (tmp_path / 'job').exists()


def test_j4_a_parent_leaving_by_systemexit_leaves_no_process(tmp_path, worker_unchanged):
    pids = tmp_path / 'pids'

    def work():
        sleeper = subprocess.Popen(['sleep', '600'])
        pids.write_text(f'{os.getpid()} {sleeper.pid}')
        time.sleep(600)

    def abort(_signum, _frame):
        raise SystemExit(1)   # what gunicorn's worker does on abort

    old = signal.signal(signal.SIGALRM, abort)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        with pytest.raises(SystemExit):
            _run(tmp_path, work, deadline=time.monotonic() + 60)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    child, sleeper = (int(p) for p in pids.read_text().split())
    assert _wait_gone(child)
    assert not _alive(sleeper)
    assert not (tmp_path / 'job').exists()


# ---------------------------------------------------------------------------
# J5, everything the job writes lives in one directory
# ---------------------------------------------------------------------------
@pytest.fixture
def system_tmp(tmp_path, monkeypatch):
    """An empty directory standing in for the system TMPDIR."""
    path = tmp_path / 'system-tmp'
    path.mkdir()
    monkeypatch.setenv('TMPDIR', str(path))
    monkeypatch.setattr(tempfile, 'tempdir', str(path))
    return path


def test_j5_temp_files_of_a_killed_job_go_with_it(tmp_path, system_tmp, worker_unchanged):
    seen = tmp_path / 'seen'

    def work():
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.write(b'scratch')
        handle.close()
        folder = tempfile.mkdtemp()
        with open(os.path.join(folder, 'inner'), 'w') as fh:
            fh.write('inner')
        seen.write_text(f'{handle.name}\n{folder}\n{os.environ["TMPDIR"]}')
        time.sleep(600)

    with pytest.raises(LimitError):
        _run(tmp_path, work, deadline=time.monotonic() + 1.0)
    name, folder, tmpdir = seen.read_text().split('\n')
    job_dir = str(tmp_path / 'job')
    assert tmpdir == job_dir
    assert name.startswith(job_dir + os.sep) and folder.startswith(job_dir + os.sep)
    assert not os.path.exists(job_dir)
    assert list(system_tmp.iterdir()) == []


def _scan_pdf(path, pages):
    from PIL import Image, ImageDraw
    images = []
    for n in range(pages):
        img = Image.new('L', (1700, 2200), 255)
        draw = ImageDraw.Draw(img)
        for line in range(40):
            draw.text((100, 100 + line * 50), f'Page {n} line {line} of the scanned text', fill=0)
        images.append(img)
    images[0].save(path, 'PDF', resolution=200.0, save_all=True, append_images=images[1:])
    return path


def test_j5_an_ocr_job_killed_mid_run_leaves_nothing(tmp_path, system_tmp, monkeypatch,
                                                    worker_unchanged):
    from pdf_ops import ocr as ocr_ops
    if not ocr_ops.is_available():
        pytest.skip('OCR binaries not installed')
    # OCRmyPDF needs more than the 64 MiB the other tests allow to get going.
    monkeypatch.setattr(limits, 'JOB_MEM_BYTES', 512 * MIB)
    monkeypatch.setattr(limits, 'RESULT_BYTES', 64 * MIB)
    # Built in a job of its own so the worker's VmData check stays honest.
    src = _run(tmp_path, lambda: _scan_pdf(tmp_path / 'scan.pdf', 24), name='build')
    out = tmp_path / 'out.pdf'
    started = tmp_path / 'started'

    def work():
        started.write_text(str(os.getpid()))
        return ocr_ops.make_searchable(str(src), str(out), language='eng')

    with pytest.raises(LimitError) as info:
        _run(tmp_path, work, deadline=time.monotonic() + 2.0)
    assert info.value.kind == 'time'
    assert started.exists()
    assert _wait_gone(int(started.read_text()))
    assert not (tmp_path / 'job').exists()
    assert list(system_tmp.iterdir()) == []


# ---------------------------------------------------------------------------
# J6, the parent stops reading at the outcome
# ---------------------------------------------------------------------------
def test_j6_a_grandchild_holding_the_pipe_doesnt_delay_the_answer(tmp_path,
                                                                 worker_unchanged):
    pids = tmp_path / 'pids'

    def work():
        grandchild = os.fork()
        if grandchild == 0:   # holds every fd the child had, the pipe included
            time.sleep(600)
            os._exit(0)
        pids.write_text(f'{os.getpid()} {grandchild}')
        return 'done'

    started = time.monotonic()
    assert _run(tmp_path, work, deadline=time.monotonic() + 30) == 'done'
    assert time.monotonic() - started < 3
    child, grandchild = (int(p) for p in pids.read_text().split())
    assert _wait_gone(child)
    assert not _alive(grandchild)


# ---------------------------------------------------------------------------
# J7, a cap that doesn't bite is noticed
# ---------------------------------------------------------------------------
def test_j7_the_canary_passes_where_the_cap_works(monkeypatch):
    monkeypatch.setattr(jobs, 'limits_enforced', None)
    assert jobs.canary() is True
    assert jobs.limits_enforced is True


def test_j7_the_canary_fails_loudly_when_the_cap_has_no_effect(monkeypatch, caplog, client):
    monkeypatch.setattr(jobs, 'limits_enforced', True)
    monkeypatch.setattr(jobs, '_set_memory_cap', lambda _mem: False)
    with caplog.at_level(logging.ERROR, logger='pdf_ops.jobs'):
        assert jobs.canary() is False
    assert any('NOT enforced' in r.getMessage() for r in caplog.records)
    assert client.get('/healthz').get_json() == {'status': 'ok', 'job_limits': False}


def test_j7_healthz_reports_working_limits(monkeypatch, client):
    monkeypatch.setattr(jobs, 'limits_enforced', True)
    assert client.get('/healthz').get_json() == {'status': 'ok', 'job_limits': True}


# ---------------------------------------------------------------------------
# The rest of run_job's contract
# ---------------------------------------------------------------------------
def test_values_come_back_and_the_job_dir_goes(tmp_path, worker_unchanged):
    assert _run(tmp_path, lambda: {'path': '/x', 'n': 3}) == {'path': '/x', 'n': 3}
    assert not (tmp_path / 'job').exists()


def test_a_deadline_already_past_forks_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(os, 'fork', lambda: pytest.fail('forked'))
    with pytest.raises(LimitError) as info:
        _run(tmp_path, lambda: 1, deadline=time.monotonic() - 1)
    assert info.value.kind == 'time'


def test_no_deadline_uses_the_default_budget(tmp_path, monkeypatch, worker_unchanged):
    monkeypatch.setattr(limits, 'DEFAULT_BUDGET', 1)
    with pytest.raises(LimitError) as info:
        jobs.run_job(lambda: time.sleep(600), deadline=None,
                     job_dir=str(tmp_path / 'job'))
    assert info.value.kind == 'time'
