"""The packet staging store: staged PDFs, their holds, caps, lanes and janitor.

Design packet-builder v0.16 §4.2. No Flask here: the routes
(``routes/packet_stage.py``, ``routes/packet_build.py``) build a
:class:`Store` from the app config and call it.

The store obeys four rules.

* Staging work runs in the request's job. :func:`stage_work` and
  :func:`render_work` are what the routes run through ``run_in_job``; they
  write only into the request's temp directory, never into the store.
* Only checked results leave the request. After the job returns, the route
  hands the finished entry directory to :meth:`Store.add`, which re-checks the
  caps and renames it into ``<root>/<stage_id>/`` in one step. The upload
  itself is never kept.
* Every staged byte counts against caps the client can't reset: a global byte
  cap and a per-subject byte and entry cap, keyed on metering's subject.
* All staging state is on disk. An entry appears only by a rename of a whole
  directory, its ``meta.json`` is replaced atomically, and the ledger is a
  cache of the entries that the janitor rebuilds on every sweep.

Layout under ``root``::

    <stage_id>/doc.pdf            the staged PDF (a job opened it)
    <stage_id>/thumb-<w>-<n>.png  cached thumbnails, counted in the entry's bytes
    <stage_id>/meta.json          owner, subject, key, name, type, pages, bytes,
                                  created, holders {holder: last renewal},
                                  sizes (from packet [B7]; see page_sizes)
    <stage_id>/.lock              the use lock: shared while a build or render
                                  reads the entry, exclusive to remove it
    <stage_id>/.meta.lock         held while meta.json is read and replaced
    .ledger.json                  bytes and entries per subject and in total
    .store.lock                   held while the ledger changes
    .lanes/<subject>.work|render  per-subject lanes (flock)
    .tmp/                         the stage and render requests' temp
                                  directories, so a finished entry or
                                  thumbnail moves into place with one rename
    .trash/                       entries being deleted

Locks are always taken in this order: an entry's use lock, its meta lock,
then the store lock. Nobody waits for a use lock while holding a meta or
store lock: removal tries the use lock without blocking and skips the entry
when it is taken.
"""
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import tempfile
import time

from pdf_ops import limits
from pdf_ops.limits import LimitError

MIB = 1024 * 1024

# The ruled numbers (round note [C5]; design Q5 as ruled): per subject 200 MB
# and 100 files, a global cap of 4 GB, a 2-hour TTL from the last renewal and
# a 24-hour maximum age. The app config can override each one.
SUBJECT_CAP_BYTES = 200 * MIB
SUBJECT_CAP_FILES = 100
GLOBAL_CAP_BYTES = 4 * 1024 * MIB
DEFAULT_TTL_MINUTES = 120
DEFAULT_MAX_HOURS = 24

MAX_HOLDERS = 8            # holders per entry; a ninth drops the stalest
HOLD_MAX_IDS = 500         # keep and release together, per hold request
STAGE_THUMBS = 24          # the stage job renders the first 24 pages at 160 px
STAGE_THUMB_WIDTH = 160
THUMB_WIDTHS = (160, 320)  # cached in the entry
LANE_MAX_AGE = 24 * 3600   # lane files untouched for a day are removed
TMP_MAX_AGE = 3600         # a request's temp directory older than this was left by a crash

ID_RE = re.compile(r'[A-Za-z0-9_-]{22}')
HOLDER_RE = re.compile(r'[A-Za-z0-9_-]{22,32}')

PDF_NAME = 'doc.pdf'
META_NAME = 'meta.json'
USE_LOCK = '.lock'
META_LOCK = '.meta.lock'
LEDGER_NAME = '.ledger.json'
STORE_LOCK = '.store.lock'
LANES_DIR = '.lanes'
TMP_DIR = '.tmp'
TRASH_DIR = '.trash'


def _now():
    """Wall-clock seconds; one clock for every worker (tests patch this)."""
    return time.time()


def valid_id(value):
    return isinstance(value, str) and ID_RE.fullmatch(value) is not None


def valid_holder(value):
    return isinstance(value, str) and HOLDER_RE.fullmatch(value) is not None


def thumb_name(index, width):
    return f'thumb-{width}-{index}.png'


def key_hash(key):
    """The file hash a conversion key ``hash:engine:paper`` carries."""
    return str(key).split(':', 1)[0]


def make_key(digest, ext, engine, paper):
    """``hash:engine:paper``; a PDF is not converted, so its key is ``hash::``."""
    if ext == '.pdf':
        return f'{digest}::'
    return f'{digest}:{engine}:{paper}'


class StageRefusal(ValueError):
    """A stage job's refusal with its packet-api.json ``code`` (``rejected``,
    ``unreadable`` or ``encrypted``). It pickles whole, so the job's parent
    re-raises it with its code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message

    def __reduce__(self):
        return (type(self), (self.code, self.message))


# ---------------------------------------------------------------------------
# Small file helpers
# ---------------------------------------------------------------------------
def _read_json(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    """Replace ``path`` atomically (``os.replace``)."""
    tmp = f'{path}.{secrets.token_hex(4)}.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, separators=(',', ':'))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _lock(path, mode, block=True):
    """An fd holding ``flock(mode)`` on ``path``, or None when the lock is
    taken (non-blocking) or the path's directory is gone. Closing the fd
    releases the lock."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        fcntl.flock(fd, mode | (0 if block else fcntl.LOCK_NB))
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def _locked(path, mode, block=True):
    fd = _lock(path, mode, block)
    try:
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


def dir_bytes(path):
    """An entry's counted bytes: its PDF and thumbnails (not its meta or locks)."""
    total = 0
    with os.scandir(path) as it:
        for entry in it:
            if entry.name == PDF_NAME or (entry.name.startswith('thumb-')
                                          and entry.name.endswith('.png')):
                total += entry.stat(follow_symlinks=False).st_size
    return total


def _subject_file(subject):
    return hashlib.sha256(subject.encode()).hexdigest()[:32]


def _owner_ok(meta, owner):
    stored = meta.get('owner')
    return isinstance(stored, str) and hmac.compare_digest(stored.encode(), owner.encode())


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
class Store:
    """The staging store under ``root``. Cheap to build: the routes build one
    per request from the app config (:meth:`from_config`)."""

    def __init__(self, root, *, ttl_seconds, max_age_seconds,
                 subject_cap_bytes=SUBJECT_CAP_BYTES, subject_cap_files=SUBJECT_CAP_FILES,
                 global_cap_bytes=GLOBAL_CAP_BYTES):
        self.root = os.path.abspath(root)
        self.ttl = ttl_seconds
        self.max_age = max_age_seconds
        self.subject_cap_bytes = subject_cap_bytes
        self.subject_cap_files = subject_cap_files
        self.global_cap_bytes = global_cap_bytes
        self.lanes_root = os.path.join(self.root, LANES_DIR)
        self.tmp_root = os.path.join(self.root, TMP_DIR)
        self.trash_root = os.path.join(self.root, TRASH_DIR)
        for path in (self.root, self.lanes_root, self.tmp_root, self.trash_root):
            os.makedirs(path, exist_ok=True)

    @classmethod
    def from_config(cls, config):
        return cls(
            config['STAGING_FOLDER'],
            ttl_seconds=int(config.get('STAGING_TTL_MINUTES', DEFAULT_TTL_MINUTES)) * 60,
            max_age_seconds=int(config.get('STAGING_MAX_HOURS', DEFAULT_MAX_HOURS)) * 3600,
            subject_cap_bytes=config.get('STAGING_SUBJECT_BYTES', SUBJECT_CAP_BYTES),
            subject_cap_files=config.get('STAGING_SUBJECT_FILES', SUBJECT_CAP_FILES),
            global_cap_bytes=config.get('STAGING_GLOBAL_BYTES', GLOBAL_CAP_BYTES),
        )

    # -- paths --------------------------------------------------------------
    def entry_dir(self, stage_id):
        return os.path.join(self.root, stage_id)

    def pdf_path(self, stage_id):
        return os.path.join(self.root, stage_id, PDF_NAME)

    def meta_path(self, stage_id):
        return os.path.join(self.root, stage_id, META_NAME)

    def thumb_path(self, stage_id, index, width):
        return os.path.join(self.root, stage_id, thumb_name(index, width))

    def temp_dir(self):
        """A request's temp directory on the store's file system, removed on
        every exit (launch-hardening Rule E)."""
        return tempfile.TemporaryDirectory(prefix='req-', dir=self.tmp_root)

    def read_meta(self, stage_id):
        return _read_json(self.meta_path(stage_id))

    # -- liveness -----------------------------------------------------------
    def is_live(self, meta, now=None):
        """Live: a holder renewed it within the TTL, and it is younger than
        the maximum age, which renewal doesn't extend. No holder is expired."""
        if now is None:
            now = _now()
        holders = meta.get('holders') or {}
        if not holders:
            return False
        if now - float(meta.get('created', 0)) >= self.max_age:
            return False
        return now - max(float(t) for t in holders.values()) < self.ttl

    def live_meta(self, stage_id, owner):
        """The entry's meta when it exists, is ``owner``'s and is live; else None."""
        if not valid_id(stage_id):
            return None
        meta = self.read_meta(stage_id)
        if meta is None or not _owner_ok(meta, owner) or not self.is_live(meta):
            return None
        return meta

    # -- the ledger (store lock) ------------------------------------------
    @contextlib.contextmanager
    def _ledger(self):
        """Yield the ledger under the store lock; write it back if changed."""
        with _locked(os.path.join(self.root, STORE_LOCK), fcntl.LOCK_EX):
            ledger = _read_json(os.path.join(self.root, LEDGER_NAME))
            if not _ledger_ok(ledger):
                ledger = self._scan_ledger()
                ledger['dirty'] = True
            yield ledger
            if ledger.pop('dirty', False):
                _write_json(os.path.join(self.root, LEDGER_NAME), ledger)

    def _scan_ledger(self):
        """The ledger as the entries on disk give it."""
        ledger = _empty_ledger()
        for name in self._entry_names():
            meta = self.read_meta(name)
            if meta is None:
                continue
            _ledger_add(ledger, meta)
        return ledger

    def rebuild_ledger(self):
        """Rebuild the ledger from the entries (the janitor does this each
        sweep, so a crash between a rename and a ledger write heals)."""
        with self._ledger() as ledger:
            fresh = self._scan_ledger()
            ledger.clear()
            ledger.update(fresh)
            ledger['dirty'] = True

    def _entry_names(self):
        try:
            names = os.listdir(self.root)
        except OSError:
            return []
        return [n for n in names if valid_id(n) and os.path.isdir(os.path.join(self.root, n))]

    def ledger_snapshot(self):
        with self._ledger() as ledger:
            copy = json.loads(json.dumps(ledger))
        copy.pop('dirty', None)
        return copy

    # -- caps -----------------------------------------------------------------
    def _over(self, ledger, subject, nbytes, nfiles):
        """'staging_quota', 'staging_full' or None for adding ``nbytes`` and
        ``nfiles`` entries to ``subject``."""
        row = ledger['subjects'].get(subject) or {'bytes': 0, 'files': 0}
        if (row['bytes'] + nbytes > self.subject_cap_bytes
                or row['files'] + nfiles > self.subject_cap_files):
            return 'staging_quota'
        if ledger['bytes'] + nbytes > self.global_cap_bytes:
            return 'staging_full'
        return None

    def _with_room(self, subject, nbytes, nfiles, action=None):
        """Run ``action(ledger)`` under the store lock if ``subject`` has room
        for ``nbytes`` and ``nfiles``. When it hasn't, the dead entries that
        stand in the way are removed first and the check runs once more.
        Returns None, or the refusal code."""
        over = None
        for attempt in (0, 1):
            with self._ledger() as ledger:
                over = self._over(ledger, subject, nbytes, nfiles)
                if over is None:
                    if action is not None:
                        action(ledger)
                    return None
            if attempt == 0:
                if over == 'staging_quota':
                    self.sweep_subject(subject)
                else:
                    self.sweep_entries()
        return over

    def check_room(self, subject, nbytes):
        """Before saving: room for one more entry of ``nbytes``? None or the code."""
        return self._with_room(subject, nbytes, 1)

    def usage(self, subject, holder):
        """The numbers a ``staging_quota`` refusal carries (design §4.2)."""
        with self._ledger() as ledger:
            row = ledger['subjects'].get(subject) or {'bytes': 0, 'files': 0, 'entries': {}}
            ids = list(row.get('entries', {}))
            body = {
                'cap_bytes': self.subject_cap_bytes,
                'cap_files': self.subject_cap_files,
                'subject_bytes': row['bytes'],
                'subject_files': row['files'],
            }
        holder_bytes = holder_files = 0
        for stage_id in ids:
            meta = self.read_meta(stage_id)
            if meta and holder in (meta.get('holders') or {}):
                holder_bytes += int(meta.get('bytes', 0))
                holder_files += 1
        body['holder_bytes'] = holder_bytes
        body['holder_files'] = holder_files
        return body

    # -- stage ------------------------------------------------------------
    def find(self, owner, subject, key, file_type, holder):
        """Dedup: a live entry of ``owner`` with this conversion key (and the
        same type) gets ``holder`` added and is returned; else None."""
        with self._ledger() as ledger:
            row = ledger['subjects'].get(subject) or {}
            candidates = [sid for sid, e in (row.get('entries') or {}).items()
                          if e.get('key') == key and e.get('type') == file_type
                          and hmac.compare_digest(str(e.get('owner', '')).encode(), owner.encode())]
        for stage_id in candidates:
            meta = self.keep(stage_id, owner, holder)
            if meta is not None:
                return meta
        return None

    def add(self, src_dir, *, owner, subject, holder, key, name, file_type, pages, sizes=None):
        """Keep a finished entry directory (``src_dir``, under :attr:`tmp_root`)
        as a new stage. Re-checks the caps with its real size, then renames it
        into place and records it in the ledger, all under the store lock.

        Returns ``(meta, None)``, or ``(None, code)`` when over a cap; the
        caller's temp directory then removes ``src_dir``.
        """
        nbytes = dir_bytes(src_dir)
        now = _now()
        meta = {
            'id': None, 'owner': owner, 'subject': subject, 'key': key,
            'name': name, 'type': file_type, 'pages': int(pages), 'bytes': nbytes,
            'created': now, 'holders': {holder: now},
        }
        if sizes is not None:
            meta['sizes'] = sizes

        def action(ledger):
            stage_id = secrets.token_urlsafe(16)
            while os.path.exists(self.entry_dir(stage_id)):   # pragma: no cover - 128 bits
                stage_id = secrets.token_urlsafe(16)
            meta['id'] = stage_id
            _write_json(os.path.join(src_dir, META_NAME), meta)
            os.rename(src_dir, self.entry_dir(stage_id))
            _ledger_add(ledger, meta)
            ledger['dirty'] = True

        refusal = self._with_room(subject, nbytes, 1, action)
        if refusal is not None:
            return None, refusal
        return meta, None

    # -- holds --------------------------------------------------------------
    def keep(self, stage_id, owner, holder):
        """Add ``holder`` to a live entry of ``owner``, or renew it. Returns the
        entry's meta, or None when it is gone, expired or another owner's."""
        if not valid_id(stage_id):
            return None
        with _locked(os.path.join(self.entry_dir(stage_id), META_LOCK), fcntl.LOCK_EX) as fd:
            if fd is None:
                return None
            meta = self.read_meta(stage_id)
            if meta is None or not _owner_ok(meta, owner):
                return None
            now = _now()
            if not self.is_live(meta, now):
                return None
            holders = dict(meta.get('holders') or {})
            holders[holder] = now
            while len(holders) > MAX_HOLDERS:
                stalest = min(holders, key=lambda h: (holders[h], h))
                del holders[stalest]
            meta['holders'] = holders
            _write_json(self.meta_path(stage_id), meta)
            return meta

    def release(self, stage_id, owner, holder):
        """Drop ``holder`` from an entry of ``owner``. An entry left with no
        holder is expired, and is removed now unless a build or render is
        reading it (then the janitor removes it)."""
        if not valid_id(stage_id):
            return
        emptied = False
        with _locked(os.path.join(self.entry_dir(stage_id), META_LOCK), fcntl.LOCK_EX) as fd:
            if fd is None:
                return
            meta = self.read_meta(stage_id)
            if meta is None or not _owner_ok(meta, owner):
                return
            holders = dict(meta.get('holders') or {})
            if holder not in holders:
                return
            del holders[holder]
            meta['holders'] = holders
            _write_json(self.meta_path(stage_id), meta)
            emptied = not holders
        if emptied:
            self.remove_if_dead(stage_id)

    # -- reading an entry (builds, renders) -------------------------------
    def use(self, stage_id):
        """Take ``stage_id``'s use lock shared and return its fd, or None when
        the entry is gone. While it is held the janitor can't remove the
        entry. Close the fd to release it."""
        if not valid_id(stage_id):
            return None
        fd = _lock(os.path.join(self.entry_dir(stage_id), USE_LOCK), fcntl.LOCK_SH)
        if fd is None:
            return None
        if not os.path.isfile(self.meta_path(stage_id)):   # removed before we got it
            os.close(fd)
            return None
        return fd

    @contextlib.contextmanager
    def using(self, stage_id):
        fd = self.use(stage_id)
        try:
            yield fd is not None
        finally:
            if fd is not None:
                os.close(fd)

    def cache_thumb(self, stage_id, index, width, src):
        """Move a rendered thumbnail into the entry and count its bytes, unless
        that would put the entry's subject or the store over a cap (then it is
        served but not cached). The caller holds the entry's use lock.
        Returns True when cached."""
        with _locked(os.path.join(self.entry_dir(stage_id), META_LOCK), fcntl.LOCK_EX) as fd:
            if fd is None:
                return False
            meta = self.read_meta(stage_id)
            target = self.thumb_path(stage_id, index, width)
            if meta is None or os.path.exists(target):
                return False
            nbytes = os.path.getsize(src)
            subject = meta['subject']
            with self._ledger() as ledger:
                if self._over(ledger, subject, nbytes, 0) is not None:
                    return False
                os.replace(src, target)
                _ledger_resize(ledger, subject, stage_id, nbytes)
                ledger['dirty'] = True
            meta['bytes'] = int(meta.get('bytes', 0)) + nbytes
            _write_json(self.meta_path(stage_id), meta)
            return True

    # -- removal and the janitor ----------------------------------------
    def remove_if_dead(self, stage_id, now=None):
        """Remove an expired (or holderless, or unreadable) entry unless its
        use lock is held. Returns True when removed."""
        entry = self.entry_dir(stage_id)
        with _locked(os.path.join(entry, USE_LOCK), fcntl.LOCK_EX, block=False) as use_fd:
            if use_fd is None:
                return False   # a build or render is reading it, or it is gone
            with _locked(os.path.join(entry, META_LOCK), fcntl.LOCK_EX) as meta_fd:
                if meta_fd is None:
                    return False
                meta = self.read_meta(stage_id)
                if meta is not None and self.is_live(meta, now):
                    return False
                trash = os.path.join(self.trash_root, f'{stage_id}-{secrets.token_hex(4)}')
                with self._ledger() as ledger:
                    try:
                        os.rename(entry, trash)
                    except FileNotFoundError:
                        return False
                    subject = (meta or {}).get('subject')
                    if subject is not None:
                        _ledger_remove(ledger, subject, stage_id)
                    else:
                        ledger.clear()
                        ledger.update(self._scan_ledger())
                    ledger['dirty'] = True
        shutil.rmtree(trash, ignore_errors=True)
        return True

    def sweep_subject(self, subject):
        """Remove ``subject``'s dead entries (before a cap refusal)."""
        with self._ledger() as ledger:
            ids = list(((ledger['subjects'].get(subject) or {}).get('entries') or {}))
        return sum(1 for stage_id in ids if self.remove_if_dead(stage_id))

    def sweep_entries(self, now=None):
        return sum(1 for name in self._entry_names() if self.remove_if_dead(name, now))

    def sweep(self, now=None):
        """The janitor's pass: dead entries (skipping any under lock), the
        trash, temp directories a crash left, lane files untouched for a day,
        then the ledger rebuilt from what is left."""
        if now is None:
            now = _now()
        removed = self.sweep_entries(now)
        for name in _listdir(self.trash_root):
            shutil.rmtree(os.path.join(self.trash_root, name), ignore_errors=True)
        for name in _listdir(self.tmp_root):
            path = os.path.join(self.tmp_root, name)
            with contextlib.suppress(OSError):
                if now - os.stat(path).st_mtime > TMP_MAX_AGE:
                    shutil.rmtree(path, ignore_errors=True)
        for name in _listdir(self.lanes_root):
            path = os.path.join(self.lanes_root, name)
            with contextlib.suppress(OSError):
                if now - os.stat(path).st_mtime <= LANE_MAX_AGE:
                    continue
                with _locked(path, fcntl.LOCK_EX, block=False) as fd:
                    if fd is not None and now - os.stat(path).st_mtime > LANE_MAX_AGE:
                        os.remove(path)
        self.rebuild_ledger()
        return removed

    # -- lanes ----------------------------------------------------------------
    @contextlib.contextmanager
    def lane(self, subject, kind):
        """Take ``subject``'s ``kind`` lane ('work' or 'render') without
        blocking. Yields True when taken, False when another request holds it.
        A flock, so it holds across workers and frees itself when a worker dies."""
        path = os.path.join(self.lanes_root, f'{_subject_file(subject)}.{kind}')
        fd = _lock(path, fcntl.LOCK_EX, block=False)
        if fd is None:
            yield False
            return
        try:
            os.utime(fd)   # the janitor removes lane files untouched for a day
            yield True
        finally:
            os.close(fd)


def _listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def _empty_ledger():
    return {'version': 1, 'bytes': 0, 'files': 0, 'subjects': {}}


def _ledger_ok(ledger):
    """A ledger this code wrote; anything else is rebuilt from the entries."""
    if not (isinstance(ledger, dict) and ledger.get('version') == 1
            and isinstance(ledger.get('subjects'), dict)
            and isinstance(ledger.get('bytes'), int) and isinstance(ledger.get('files'), int)):
        return False
    for row in ledger['subjects'].values():
        if not (isinstance(row, dict) and isinstance(row.get('bytes'), int)
                and isinstance(row.get('files'), int) and isinstance(row.get('entries'), dict)):
            return False
        for entry in row['entries'].values():
            if not (isinstance(entry, dict) and isinstance(entry.get('bytes'), int)):
                return False
    return True


def _ledger_add(ledger, meta):
    row = ledger['subjects'].setdefault(meta['subject'], {'bytes': 0, 'files': 0, 'entries': {}})
    nbytes = int(meta.get('bytes', 0))
    row['entries'][meta['id']] = {'owner': meta['owner'], 'key': meta['key'],
                                  'type': meta.get('type', ''), 'bytes': nbytes}
    row['bytes'] += nbytes
    row['files'] += 1
    ledger['bytes'] += nbytes
    ledger['files'] += 1


def _ledger_remove(ledger, subject, stage_id):
    row = ledger['subjects'].get(subject)
    if not row or stage_id not in row['entries']:
        return
    nbytes = row['entries'].pop(stage_id)['bytes']
    row['bytes'] -= nbytes
    row['files'] -= 1
    ledger['bytes'] -= nbytes
    ledger['files'] -= 1
    if not row['entries']:
        del ledger['subjects'][subject]


def _ledger_resize(ledger, subject, stage_id, added):
    row = ledger['subjects'].get(subject)
    if not row or stage_id not in row['entries']:
        return
    row['entries'][stage_id]['bytes'] += added
    row['bytes'] += added
    ledger['bytes'] += added


# ---------------------------------------------------------------------------
# Job work (runs inside run_job: no Flask, writes only where it is told)
# ---------------------------------------------------------------------------
_PLACEHOLDER_FILL = (226, 228, 232)
_PLACEHOLDER_TEXT = (90, 96, 108)
_LETTER_RATIO = 792.0 / 612.0
# A tEXt chunk the page script looks for, so a page whose image is this tile
# shows no number on it (design packet-builder §2.1 Page numbers on the page
# images; verification v0.20 m4): the tile's shape is not the page's.
PLACEHOLDER_TEXT = ('docist', 'placeholder')


def _placeholder_png(width, out_path):
    """The "too large to preview" tile: a Letter-shaped grey PNG, marked with
    :data:`PLACEHOLDER_TEXT`."""
    from PIL import Image, ImageDraw
    from PIL.PngImagePlugin import PngInfo
    height = max(1, round(width * _LETTER_RATIO))
    image = Image.new('RGB', (width, height), _PLACEHOLDER_FILL)
    draw = ImageDraw.Draw(image)
    lines = ['Too large', 'to preview']
    y = height // 2 - 12
    for line in lines:
        w = draw.textlength(line)
        draw.text(((width - w) / 2, y), line, fill=_PLACEHOLDER_TEXT)
        y += 14
    info = PngInfo()
    info.add_text(*PLACEHOLDER_TEXT)
    image.save(out_path, 'PNG', pnginfo=info)


def _render_page_png(doc, index, width, out_path):
    """Render page ``index`` of an open pdfium document ``width`` px wide to
    ``out_path`` through ``limits.render_page`` (FRAME_PIXELS). A page over
    the frame limit, or one pdfium fails on, gets the placeholder tile, so
    the other pages still render. Memory and every other limit pass through.
    Returns True for a placeholder."""
    from pdf_ops.preview import _scale_for
    page = doc[index]
    try:
        try:
            bitmap = limits.render_page(page, _scale_for(page.get_width(), width),
                                        page_number=index + 1)
        except LimitError as exc:
            if exc.kind != 'frame':
                raise
            _placeholder_png(width, out_path)
            return True
        except MemoryError:
            raise
        except Exception:   # noqa: BLE001 - pdfium's error on one damaged page
            _placeholder_png(width, out_path)
            return True
        image = bitmap.to_pil().convert('RGB')
        del bitmap
        image.save(out_path, 'PNG')
        return False
    finally:
        page.close()


def page_sizes(reader):
    """Each page's displayed size, for placing its number on the page images
    (design packet-builder §4.2 Convert ``sizes``, §3.3 rule 7).

    A list of runs ``[count, crop_w, crop_h, rotate, media_w, media_h]`` in
    page order, whose counts add up to the page count. ``crop`` is the visible
    box, the crop box clipped to the media box (``pdf_ops.packet.visible_box``,
    which the stamp uses too); ``rotate`` is the page's own ``/Rotate``
    reduced modulo 360. Sizes are points rounded to 0.01, and consecutive
    pages with equal values share a run.
    """
    from pdf_ops.packet import visible_box

    runs = []
    for page in reader.pages:
        _left, _bottom, crop_w, crop_h = visible_box(page)
        media = page.mediabox
        values = [round(crop_w, 2), round(crop_h, 2), int(page.rotation) % 360,
                  round(abs(float(media.width)), 2), round(abs(float(media.height)), 2)]
        if runs and runs[-1][1:] == values:
            runs[-1][0] += 1
        else:
            runs.append([1] + values)
    return runs


def stage_work(upload_path, ext, render_opts, entry_dir):
    """One stage request's work, run in its job (design §4.2 Convert).

    Converts a non-PDF with its converter and the request's RenderOptions, or
    copies the uploaded PDF, into ``entry_dir/doc.pdf``; opens that PDF with
    pypdf and counts its pages; renders the first ``STAGE_THUMBS`` pages at
    160 px through ``limits.render_page``. ``entry_dir`` is created here, in
    the request's temp directory, outside the job directory.

    Also reads each page's size (:func:`page_sizes`) from the reader it
    already opened. Returns ``{'pages', 'pdf_bytes', 'sizes'}``. Raises
    :class:`StageRefusal`
    (``rejected``, ``unreadable``, ``encrypted``); a limit reaches the parent
    as ``LimitError`` through the job's cause-chain walk.
    """
    import pypdfium2 as pdfium
    from pypdf import PdfReader

    from converters import get_converter
    from pdf_ops.pdfium_lock import PDFIUM_LOCK

    os.makedirs(entry_dir)
    pdf_path = os.path.join(entry_dir, PDF_NAME)
    if ext == '.pdf':
        shutil.copyfile(upload_path, pdf_path)
    else:
        converter = get_converter(ext)
        if converter is None:
            raise StageRefusal('rejected', f"Docist can't convert {ext} files.")
        try:
            converter(upload_path, pdf_path, render_opts)
        except Exception as exc:
            # MemoryError and LimitError in the chain still win (jobs.classify).
            raise StageRefusal('rejected', str(exc) or type(exc).__name__) from exc

    try:
        reader = PdfReader(pdf_path)
        if reader.is_encrypted and not reader.decrypt(''):
            raise StageRefusal('encrypted',
                               'This PDF needs a password. Remove the password and add it again.')
        pages = len(reader.pages)
        sizes = page_sizes(reader) if pages else []
    except StageRefusal:
        raise
    except Exception as exc:
        raise StageRefusal('unreadable', "This PDF is damaged and can't be read.") from exc
    del reader
    if pages < 1:
        raise StageRefusal('unreadable', 'This PDF has no pages.')

    with PDFIUM_LOCK:
        try:
            doc = pdfium.PdfDocument(pdf_path)
        except Exception as exc:
            raise StageRefusal('unreadable', "This PDF is damaged and can't be read.") from exc
        try:
            for index in range(min(pages, len(doc), STAGE_THUMBS)):
                _render_page_png(doc, index, STAGE_THUMB_WIDTH,
                                 os.path.join(entry_dir, thumb_name(index, STAGE_THUMB_WIDTH)))
        finally:
            doc.close()
    return {'pages': pages, 'pdf_bytes': os.path.getsize(pdf_path), 'sizes': sizes}


def render_work(pdf_path, index, width, out_path):
    """One page render, run in its job: page ``index`` of a staged PDF,
    ``width`` px wide, to ``out_path`` (outside the job directory). Returns
    ``{'placeholder': bool}``."""
    import pypdfium2 as pdfium

    from pdf_ops.pdfium_lock import PDFIUM_LOCK

    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(pdf_path)
        try:
            placeholder = _render_page_png(doc, index, width, out_path)
        finally:
            doc.close()
    return {'placeholder': placeholder}
