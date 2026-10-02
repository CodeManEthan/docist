"""The Word engine: LibreOffice Writer, headless, one process group per call.

Paid users' ``.docx`` files are rendered by LibreOffice, which keeps the
document's own layout and page size (design prelaunch-fixes v0.4 §5). Free
users keep the mammoth reflow in ``converters/docx_converter.py``, which is
also the fallback when this module raises :class:`OfficeError`.

Rendering an upload makes no network request (round [Q6], ruled 2026-10-02,
round note [C1]: remote content in Word files is dropped). Two guards each
hold that on their own, and the tests check each with the other turned off:

  * the strip: every relationship whose target lies outside the package is
    removed unless every reference to it is the ``r:id`` of a ``w:hyperlink``
    (:func:`strip_external`);
  * the profile: each call runs on a fresh LibreOffice profile seeded from
    ``office_profile/registrymodifications.xcu``, with macros off and
    untrusted links blocked.

Time and memory: all LibreOffice work in one request ends by the request's
``deadline`` (a ``time.monotonic()`` value, or None for no deadline). One call
runs at most ``DOCIST_OFFICE_TIMEOUT`` seconds (default 60) and keeps
``RESERVE_SECONDS`` of the deadline for the reflow fallback; with less than
``MIN_CALL_SECONDS`` left it doesn't start LibreOffice at all. The process
group is killed on every exit this process sees. For the exits it can't see
(the worker itself SIGKILLed: gunicorn after a failed graceful abort, the
kernel's OOM killer, or ``docker stop`` past its grace period), every process
in the group carries ``RLIMIT_CPU`` at three times the call's wall timeout and
``RLIMIT_AS`` at ``DOCIST_OFFICE_MEM_MB`` (default 1536). An orphan that
spins hits the CPU limit; one that sits idle lives until the container
restarts (design §9).

No Flask here: the web layer passes the deadline in.
"""
import logging
import math
import os
import re
import resource
import shutil
import signal
import subprocess
import tempfile
import time
import zipfile
import zlib
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)

WORD_MAIN_TYPE = (
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
)
INFILTER = 'MS Word 2007 XML'

DEFAULT_TIMEOUT = 60       # seconds, DOCIST_OFFICE_TIMEOUT
DEFAULT_MEM_MB = 1536      # DOCIST_OFFICE_MEM_MB
RESERVE_SECONDS = 15       # kept from the deadline for the reflow fallback
MIN_CALL_SECONDS = 10      # under this, LibreOffice isn't started

# Bounds on the work the strip does before LibreOffice sees anything. A real
# image-heavy .docx near the paid upload limit unpacks to about its own size.
MAX_ENTRIES = 10_000
MAX_XML_PART_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
_STDERR_TAIL = 2000

_PROFILE_SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'office_profile', 'registrymodifications.xcu')

_PKG_REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
_CT_NS = 'http://schemas.openxmlformats.org/package/2006/content-types'
_DOC_REL_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_HYPERLINK = f'{{{_W_NS}}}hyperlink'
_RID = f'{{{_DOC_REL_NS}}}id'

# A target with a URI scheme (http:, file:, smb:, ...) or a network path.
_OUTSIDE_TARGET = re.compile(r'^\s*([A-Za-z][A-Za-z0-9+.\-]*:|//|\\\\)')


class OfficeError(Exception):
    """LibreOffice couldn't render this file; the caller falls back to the reflow."""


def soffice_path():
    """Path of the ``soffice`` binary, or None."""
    return shutil.which('soffice')


def available():
    """True when LibreOffice is installed."""
    return soffice_path() is not None


def _env_number(name, default):
    try:
        value = float(os.environ[name])
    except (KeyError, ValueError):
        return default
    return value if value > 0 else default


def office_timeout():
    return _env_number('DOCIST_OFFICE_TIMEOUT', DEFAULT_TIMEOUT)


def office_mem_mb():
    return int(_env_number('DOCIST_OFFICE_MEM_MB', DEFAULT_MEM_MB))


def call_timeout(deadline, now=None):
    """Seconds this call may run, or raise ``OfficeError('out of time')``.

    ``min(DOCIST_OFFICE_TIMEOUT, deadline - now - RESERVE_SECONDS)``; with no
    deadline, the per-call timeout alone.
    """
    limit = office_timeout()
    if deadline is None:
        return limit
    if now is None:
        now = time.monotonic()
    limit = min(limit, deadline - now - RESERVE_SECONDS)
    if limit < MIN_CALL_SECONDS:
        raise OfficeError('out of time')
    return limit


# ---------------------------------------------------------------------------
# Step 1: bind the input to Word
# ---------------------------------------------------------------------------
def _read_member(zin, info):
    cap = MAX_XML_PART_BYTES
    if info.file_size > cap:
        raise OfficeError(f'{info.filename} is too large')
    with zin.open(info) as fh:
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise OfficeError(f'{info.filename} is too large')
    return data


def _parse(data, name):
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise OfficeError(f'{name} is not well-formed XML') from exc


def _open_zip(path):
    try:
        zin = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise OfficeError('not a Word document') from exc
    infos = zin.infolist()
    if len(infos) > MAX_ENTRIES:
        zin.close()
        raise OfficeError('too many parts')
    # Part names are case-insensitive (OPC). Two entries with one name would
    # let the strip read one copy of a part while LibreOffice reads the other.
    lowered = [info.filename.lower() for info in infos]
    if len(set(lowered)) != len(lowered):
        zin.close()
        raise OfficeError('duplicate part names')
    return zin, infos


def check_word(zin):
    """Raise ``OfficeError('not a Word document')`` unless the package
    declares the WordprocessingML main part (and holds it)."""
    try:
        info = zin.getinfo('[Content_Types].xml')
    except KeyError:
        raise OfficeError('not a Word document') from None
    root = _parse(_read_member(zin, info), '[Content_Types].xml')
    names = set(zin.namelist())
    for override in root.iter(f'{{{_CT_NS}}}Override'):
        if override.get('ContentType', '').strip() == WORD_MAIN_TYPE:
            part = override.get('PartName', '').lstrip('/')
            if part in names:
                return part
    raise OfficeError('not a Word document')


# ---------------------------------------------------------------------------
# Step 2: strip external targets by use
# ---------------------------------------------------------------------------
def _is_rels(name):
    return name.lower().endswith('.rels')


def _source_part(rels_name):
    """The part a ``.rels`` part belongs to, lower-cased (part names are
    case-insensitive): ``word/_rels/document.xml.rels`` -> ``word/document.xml``;
    the package's ``_rels/.rels`` -> None. A name of any other shape gives a
    part that doesn't exist, so its outside targets all go."""
    head, _, tail = rels_name.lower().rpartition('_rels/')
    base = tail[:-len('.rels')]
    return (head + base) if base else None


def _is_outside(rel):
    if rel.get('TargetMode', '').strip().lower() == 'external':
        return True
    return bool(_OUTSIDE_TARGET.match(rel.get('Target', '')))


def _references(root, rel_ids):
    """``{rel_id: [(element tag, attribute name), ...]}`` over ``root``, for
    every attribute or element text whose value is one of ``rel_ids``.

    Any attribute in any namespace counts, and so does element text, so an
    unexpected way of pointing at a relationship counts as a non-hyperlink
    use. One walk of the tree, however many ids are asked about.
    """
    refs = {rel_id: [] for rel_id in rel_ids}
    for elem in root.iter():
        for name, value in elem.attrib.items():
            if value in refs:
                refs[value].append((elem.tag, name))
        if elem.text is not None:
            text = elem.text.strip()
            if text in refs:
                refs[text].append((elem.tag, None))
    return refs


def _keeps(refs):
    return bool(refs) and all(tag == _HYPERLINK and name == _RID for tag, name in refs)


def strip_rels(rels_xml, load_source):
    """Return ``(new_rels_xml, removed_ids)`` for one ``.rels`` part.

    Rule: a relationship whose target lies outside the package survives only
    if every reference to its id in the part it belongs to is the ``r:id`` of
    a ``w:hyperlink``, whatever type the relationship declares.
    ``load_source()`` returns that part's parsed root, or None when there is
    none (the package's own ``.rels``), and then every such relationship goes.
    It is called only when the ``.rels`` has an outside target.
    """
    root = _parse(rels_xml, '.rels')
    outside = [rel for rel in root
               if rel.tag == f'{{{_PKG_REL_NS}}}Relationship' and _is_outside(rel)]
    if not outside:
        return rels_xml, []
    source_root = load_source()
    ids = {rel.get('Id', '') for rel in outside}
    refs = _references(source_root, ids) if source_root is not None else {}
    removed = []
    for rel in outside:
        rel_id = rel.get('Id', '')
        if not _keeps(refs.get(rel_id, [])):
            root.remove(rel)
            removed.append(rel_id)
    if not removed:
        return rels_xml, removed
    ET.register_namespace('', _PKG_REL_NS)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True), removed


def strip_external(src_path, dst_path):
    """Copy the ``.docx`` at ``src_path`` to ``dst_path``, Word-checked and
    stripped (steps 1 and 2). Returns the list of removed relationship ids."""
    zin, infos = _open_zip(src_path)
    removed = []
    with zin:
        check_word(zin)
        by_lower = {info.filename.lower(): info for info in infos}
        unpacked = 0  # bytes actually decompressed, not the archive's claims

        def count(n):
            nonlocal unpacked
            unpacked += n
            if unpacked > MAX_UNPACKED_BYTES:
                raise OfficeError('the document unpacks too large')

        with zipfile.ZipFile(dst_path, 'w', zipfile.ZIP_DEFLATED) as zout:
            for info in infos:
                if info.is_dir():
                    continue
                if _is_rels(info.filename):
                    data = _read_member(zin, info)
                    count(len(data))
                    source = _source_part(info.filename)

                    def load_source(source=source):
                        if source is None or source not in by_lower:
                            return None
                        part = _read_member(zin, by_lower[source])
                        return _parse(part, source)

                    data, gone = strip_rels(data, load_source)
                    removed.extend(gone)
                    zout.writestr(info.filename, data)
                    continue
                with zin.open(info) as fh, zout.open(info.filename, 'w') as out:
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        count(len(chunk))
                        out.write(chunk)
    return removed


# ---------------------------------------------------------------------------
# Steps 3 to 7: profile, time, run, memory cap, check
# ---------------------------------------------------------------------------
def _seed_profile(profile_dir):
    user = os.path.join(profile_dir, 'user')
    os.makedirs(user, exist_ok=True)
    shutil.copyfile(_PROFILE_SEED, os.path.join(user, 'registrymodifications.xcu'))


def _limits(timeout, mem_mb):
    cpu = int(math.ceil(timeout * 3))
    mem = int(mem_mb) * 1024 * 1024

    def apply():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    return apply


def _kill_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _tail(path):
    try:
        with open(path, 'rb') as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - _STDERR_TAIL))
            return fh.read().decode('utf-8', 'replace').strip()
    except OSError:
        return ''


def _child_env(tmpdir):
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': tmpdir,
           'LANG': os.environ.get('LANG', 'C.UTF-8')}
    return env


def docx_to_pdf(input_path, output_path, deadline=None, *, strip=True, profile=True):
    """Render the ``.docx`` at ``input_path`` to ``output_path`` with LibreOffice.

    Raises :class:`OfficeError` for a file that isn't Word, a file LibreOffice
    can't render, a timeout, or no time left. ``strip`` and ``profile`` exist
    so the tests can check each network guard with the other turned off;
    nothing else turns them off.
    """
    binary = soffice_path()
    if binary is None:
        raise OfficeError('LibreOffice is not installed')
    with tempfile.TemporaryDirectory(prefix='docist-office-') as tmpdir:
        doc = os.path.join(tmpdir, 'input.docx')
        try:
            if strip:
                removed = strip_external(input_path, doc)
                if removed:
                    log.info('Word engine dropped %d external target(s)', len(removed))
            else:
                zin, _ = _open_zip(input_path)
                with zin:
                    check_word(zin)
                shutil.copyfile(input_path, doc)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError,
                RuntimeError) as exc:
            # A damaged or encrypted member, or a compression zipfile lacks.
            raise OfficeError(f'unreadable Word package: {exc}') from exc

        timeout = call_timeout(deadline)

        profile_dir = os.path.join(tmpdir, 'profile')
        if profile:
            _seed_profile(profile_dir)
        else:
            os.makedirs(profile_dir)
        outdir = os.path.join(tmpdir, 'out')
        os.makedirs(outdir)
        errlog = os.path.join(tmpdir, 'soffice.log')
        cmd = [
            binary, '--headless', '--norestore', '--nolockcheck', '--nodefault',
            f'-env:UserInstallation=file://{profile_dir}',
            f'--infilter={INFILTER}',
            '--convert-to', 'pdf', '--outdir', outdir, doc,
        ]
        with open(errlog, 'wb') as err:
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=err, stderr=err,
                    cwd=tmpdir, env=_child_env(tmpdir), start_new_session=True,
                    preexec_fn=_limits(timeout, office_mem_mb()),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise OfficeError(f'LibreOffice could not start: {exc}') from exc
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                raise OfficeError(f'timed out after {timeout:.0f} s') from None
            finally:
                # The group never outlives the call: success, error, timeout,
                # and the SystemExit gunicorn raises when it aborts a worker.
                _kill_group(proc.pid)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    pass

        result = os.path.join(outdir, 'input.pdf')
        try:
            with open(result, 'rb') as fh:
                head = fh.read(5)
        except OSError:
            head = b''
        if head != b'%PDF-':
            raise OfficeError(
                f'LibreOffice wrote no PDF (exit {proc.returncode}): {_tail(errlog)}')
        shutil.move(result, output_path)
