"""Jobs: every request's work on an upload runs in a forked child with hard limits.

No Flask here (design launch-hardening v0.4 §3). The web layer passes the
deadline in; :func:`utils.render_opts.run_in_job` is the request-side wrapper.

A job obeys seven invariants, and the code below is arranged so each holds
on every exit path (tests/backend/test_jobs.py tests each by number):

J1, every process is capped. The forked child, before running the work,
    sets ``RLIMIT_DATA`` soft to its own ``VmData`` plus ``JOB_MEM_BYTES``
    (hard unchanged) and ``RLIMIT_FSIZE`` soft and hard to ``RESULT_BYTES``
    with ``SIGXFSZ`` ignored. Every descendant inherits both. LibreOffice is
    the exception: ``office._limits`` lifts ``RLIMIT_DATA`` and applies B2's
    own ``RLIMIT_AS`` and ``RLIMIT_CPU``.
J2, the child decides what happened. It walks the exception's cause chain
    itself and sends a tagged outcome: ``('ok', value)``, ``('limit', kind,
    message)`` or ``('error', pickled exception, text)``. The parent never
    reads ``__cause__`` or ``__context__`` from anything it unpickled.
J3, the outcome can always be sent. The child lifts its own memory cap and
    drops the failed frames' tracebacks before it builds the outcome.
J4, nothing outlives the job. On every way the parent leaves
    :func:`run_job` (an outcome, the deadline, an exception, gunicorn's
    ``SystemExit``) a ``finally`` kills the child's process group and reaps
    the child. The child also asks the kernel to kill it if the worker dies
    (``PR_SET_PDEATHSIG``).
J5, everything the job writes lives in one directory. The child points
    ``tempfile.tempdir`` and ``TMPDIR`` at ``job_dir``; the parent removes
    ``job_dir`` after the child is reaped, on every exit path. Results the
    route keeps are written outside it, in the request's temp directory.
J6, the parent stops reading at the outcome. The outcome is an 8-byte
    length and that many bytes, so a descendant holding the pipe's write end
    can't make a finished job look like a timeout.
J7, a cap that doesn't bite is noticed. :func:`canary` runs one job that
    allocates past a small cap; app.py runs it at start, logs an error when
    the allocation succeeds and ``/healthz`` reports ``"job_limits": false``.
"""
import ctypes
import errno
import logging
import mmap
import os
import pickle
import resource
import select
import shutil
import signal
import struct
import tempfile
import time

from pdf_ops import limits
from pdf_ops.limits import LimitError

log = logging.getLogger(__name__)

_LENGTH = struct.Struct('<Q')
MAX_OUTCOME_BYTES = 64 * limits.MIB   # an outcome is paths, numbers and short text
_POLL_SECONDS = 0.25                  # how often the parent checks a silent child
_PR_SET_PDEATHSIG = 1

# Signals the worker may have handlers for (gunicorn's). The child takes the
# defaults so a stray signal ends it rather than running worker code.
_RESET_SIGNALS = tuple(
    getattr(signal, name) for name in (
        'SIGTERM', 'SIGINT', 'SIGQUIT', 'SIGHUP', 'SIGUSR1', 'SIGUSR2',
        'SIGWINCH', 'SIGTTIN', 'SIGTTOU', 'SIGCHLD')
    if hasattr(signal, name))

try:   # loaded once, in the parent, so no child has to
    _LIBC = ctypes.CDLL(None, use_errno=True)
    _PRCTL = _LIBC.prctl
except (OSError, AttributeError):   # pragma: no cover - not Linux
    _PRCTL = None

# The canary (J7): a job capped at CANARY_CAP that allocates CANARY_ALLOC.
CANARY_CAP = 64 * limits.MIB
CANARY_ALLOC = 256 * limits.MIB

# Set by canary(): True when the memory cap works on this platform.
limits_enforced = None


# ---------------------------------------------------------------------------
# The child's limits (J1, J3)
# ---------------------------------------------------------------------------
def vm_data_bytes():
    """This process's ``VmData`` in bytes, or None without ``/proc``."""
    try:
        with open('/proc/self/status', 'rb') as fh:
            for line in fh:
                if line.startswith(b'VmData:'):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _set_memory_cap(mem_bytes):
    """``RLIMIT_DATA`` soft to ``VmData + mem_bytes``, hard unchanged. Returns
    False (and sets nothing) without ``/proc``."""
    base = vm_data_bytes()
    if base is None:
        return False
    _soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
    soft = base + int(mem_bytes)
    if hard != resource.RLIM_INFINITY:
        soft = min(soft, hard)
    resource.setrlimit(resource.RLIMIT_DATA, (soft, hard))
    return True


def lift_memory_cap():
    """``RLIMIT_DATA`` soft back to its hard value (J3; LibreOffice, §3.4)."""
    _soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
    resource.setrlimit(resource.RLIMIT_DATA, (hard, hard))


def _set_file_cap(nbytes):
    """``RLIMIT_FSIZE`` soft and hard to ``nbytes``; a write past it fails
    with ``EFBIG`` instead of a signal."""
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    _soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    value = int(nbytes)
    if hard != resource.RLIM_INFINITY:
        value = min(value, hard)
    resource.setrlimit(resource.RLIMIT_FSIZE, (value, value))


def _die_with_parent(parent_pid):
    if _PRCTL is not None:
        _PRCTL(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    if os.getppid() != parent_pid:   # the worker died before prctl took
        os._exit(1)


# ---------------------------------------------------------------------------
# The outcome (J2, J3, J6)
# ---------------------------------------------------------------------------
def _chain(exc):
    """``exc`` and every exception in its cause and context chain, once each."""
    seen = []
    stack = [exc]
    while stack:
        item = stack.pop()
        if item is None or any(item is s for s in seen):
            continue
        seen.append(item)
        stack.append(item.__context__)
        stack.append(item.__cause__)
    return seen


def classify(exc):
    """The tagged outcome for an exception the work raised, walking its chain
    in the process that raised it: memory first, then a write past the file
    cap, then a :class:`LimitError`; anything else is an error."""
    chain = _chain(exc)
    if any(isinstance(e, MemoryError) for e in chain):
        return ('limit', 'memory', limits.memory_message())
    if any(isinstance(e, OSError) and e.errno == errno.EFBIG for e in chain):
        return ('limit', 'file', limits.file_message())
    for e in chain:
        if isinstance(e, LimitError):
            return ('limit', e.kind, str(e))
    try:
        text = f'{type(exc).__name__}: {exc}'
    except Exception:   # noqa: BLE001 - a __str__ that raises
        text = type(exc).__name__
    try:
        data = pickle.dumps(exc)
    except Exception:   # noqa: BLE001 - an exception that won't pickle
        data = None
    return ('error', data, text)


def _pack(outcome):
    try:
        data = pickle.dumps(outcome)
    except Exception as exc:   # noqa: BLE001 - a result that won't pickle
        data = pickle.dumps(('error', None,
                             f'the result could not be sent: {type(exc).__name__}: {exc}'))
    return _LENGTH.pack(len(data)) + data


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _child(work, job_dir, write_fd, parent_pid, mem_bytes, file_bytes):
    """Runs in the forked child. Never returns: leaves with ``os._exit`` so no
    atexit handler, finaliser or connection of the parent's runs here."""
    code = 0
    try:
        try:
            os.setpgid(0, 0)
        except OSError:
            pass
        _die_with_parent(parent_pid)
        for signum in _RESET_SIGNALS:
            signal.signal(signum, signal.SIG_DFL)
        tempfile.tempdir = job_dir
        os.environ['TMPDIR'] = job_dir
        _set_file_cap(file_bytes)
        _set_memory_cap(mem_bytes)
        try:
            outcome = ('ok', work())
            lift_memory_cap()
        except BaseException as exc:   # noqa: BLE001 - every outcome is sent
            lift_memory_cap()
            for item in _chain(exc):
                item.__traceback__ = None
            outcome = classify(exc)
        _write_all(write_fd, _pack(outcome))
    except BaseException:   # noqa: BLE001 - the parent reports "couldn't convert"
        code = 70
    finally:
        os._exit(code)


def _read_outcome(fd, pid, deadline):
    """``('ok', bytes)``, ``('timeout', None)`` or ``('eof', status)``.

    Reads the 8-byte length, then exactly that many bytes (J6). A child that
    exits without a whole outcome is ``eof`` even when a descendant still
    holds the pipe open. Returns the child's wait status when it was reaped
    here (else None) as the last element.
    """
    buf = bytearray()
    length = None
    status = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 'timeout', None, status
        ready, _, _ = select.select([fd], [], [], min(remaining, _POLL_SECONDS))
        if not ready:
            if status is None:
                done, wait_status = os.waitpid(pid, os.WNOHANG)
                if done:
                    status = wait_status
            if status is not None:
                # Anything the child wrote is in the pipe by now.
                again, _, _ = select.select([fd], [], [], 0)
                if not again:
                    return 'eof', None, status
            continue
        want = (_LENGTH.size if length is None else _LENGTH.size + length) - len(buf)
        chunk = os.read(fd, max(1, min(want, 1 << 20)))
        if not chunk:
            return 'eof', None, status
        buf += chunk
        if length is None and len(buf) >= _LENGTH.size:
            (length,) = _LENGTH.unpack(bytes(buf[:_LENGTH.size]))
            if length > MAX_OUTCOME_BYTES:
                return 'eof', None, status
        if length is not None and len(buf) >= _LENGTH.size + length:
            return 'ok', bytes(buf[_LENGTH.size:_LENGTH.size + length]), status


def _kill_group(pid):
    for kill in (os.killpg, os.kill):
        try:
            kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _reap(pid):
    while True:
        try:
            os.waitpid(pid, 0)
            return
        except ChildProcessError:
            return
        except InterruptedError:   # pragma: no cover - PEP 475 retries
            continue


def _raise_outcome(outcome):
    tag = outcome[0]
    if tag == 'ok':
        return outcome[1]
    if tag == 'limit':
        _tag, kind, message = outcome
        if kind == 'memory':
            message = limits.memory_message()
        elif kind == 'file':
            message = limits.file_message()
        raise LimitError(message, kind=kind)
    _tag, data, text = outcome
    exc = None
    if data is not None:
        try:
            exc = pickle.loads(data)
        except Exception:   # noqa: BLE001 - a class that pickles but won't unpickle
            exc = None
    if not isinstance(exc, BaseException):
        exc = RuntimeError(text)
    raise exc


def run_job(work, *, deadline=None, job_dir, budget=None, mem_bytes=None):
    """Run ``work()`` in a forked child under the job's limits; return its value.

    ``deadline`` is a ``time.monotonic()`` value; None means
    ``limits.DEFAULT_BUDGET`` seconds from now. ``job_dir`` must not exist
    yet: it is created here and removed on every exit. ``budget`` is the
    number of seconds the time message names. ``mem_bytes`` overrides
    ``JOB_MEM_BYTES`` (the canary).

    Raises :class:`LimitError` for a limit, the deadline, or a child that
    died without an outcome; re-raises the work's own exception otherwise
    (``RuntimeError`` when it can't be unpickled); raises ``OSError`` when
    the fork or the pipe fails.
    """
    if deadline is None:
        deadline = time.monotonic() + limits.DEFAULT_BUDGET
    if budget is None:
        budget = limits.DEFAULT_BUDGET
    if deadline <= time.monotonic():
        raise LimitError(limits.time_message(budget), kind='time')
    mem = limits.JOB_MEM_BYTES if mem_bytes is None else mem_bytes
    file_cap = limits.RESULT_BYTES

    os.makedirs(job_dir, mode=0o700)
    pid = None
    reaped = False
    read_fd = write_fd = None
    try:
        read_fd, write_fd = os.pipe()
        parent_pid = os.getpid()
        pid = os.fork()
        if pid == 0:   # pragma: no cover - the child; never returns
            os.close(read_fd)
            _child(work, job_dir, write_fd, parent_pid, mem, file_cap)
        os.close(write_fd)
        write_fd = None
        try:
            os.setpgid(pid, pid)
        except OSError:
            pass   # the child did it first, or is already gone
        state, data, status = _read_outcome(read_fd, pid, deadline)
        reaped = status is not None
        if state == 'timeout':
            raise LimitError(limits.time_message(budget), kind='time')
        if state == 'eof':
            raise LimitError(limits.died_message(), kind='died')
        outcome = pickle.loads(data)
        return _raise_outcome(outcome)
    finally:
        # J4 and J5, on every exit: SystemExit and KeyboardInterrupt too.
        for fd in (read_fd, write_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if pid:
            # After a normal outcome this is harmless, and it catches a
            # descendant the work left behind.
            _kill_group(pid)
            if not reaped:
                _reap(pid)
        shutil.rmtree(job_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# J7: the canary
# ---------------------------------------------------------------------------
def _canary_work():
    # A private anonymous mapping is what malloc makes for a large block, so
    # RLIMIT_DATA counts it; it is never touched, so a cap that doesn't bite
    # costs no memory either.
    try:
        block = mmap.mmap(-1, CANARY_ALLOC, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    except OSError as exc:
        if exc.errno == errno.ENOMEM:
            raise MemoryError('canary') from exc
        raise
    block.close()
    return CANARY_ALLOC


def canary():
    """Run one job that allocates past a small cap. True when the cap stopped
    it. Logs an error and returns False when it didn't: jobs then run
    without the memory cap."""
    global limits_enforced
    ok = False
    detail = 'the allocation succeeded'
    try:
        with tempfile.TemporaryDirectory(prefix='docist-canary-') as tmp:
            run_job(_canary_work, deadline=time.monotonic() + 30,
                    job_dir=os.path.join(tmp, 'job'), mem_bytes=CANARY_CAP)
    except LimitError as exc:
        ok = exc.kind == 'memory'
        detail = f'the job ended with {exc.kind!r}'
    except Exception as exc:   # noqa: BLE001 - reported, never fatal
        detail = f'the job failed: {type(exc).__name__}: {exc}'
    limits_enforced = ok
    if not ok:
        log.error('!!! Job memory limits are NOT enforced on this platform (%s). '
                  'Uploads run without the per-request memory cap.', detail)
    return ok
