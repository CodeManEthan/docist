"""Packet pipeline: one PDF from a packet document and its staged PDFs.

A packet (design.md section 3.1) is sections of items: file runs that name a
staged PDF by the SHA-256 of the dropped file, and generated pages. This module
holds the two pieces the build needs:

  * :func:`page_plan` -- the written page-plan spec (design section 3.3): every
    physical page's position, printed label and whether the label is stamped,
    plus the contents/outline entries. It is pure, and ``static/packet-model.js``
    implements the same spec; both run the vectors in
    ``tests/fixtures/packet-plan.json``.
  * :func:`packet_pipeline` -- builds the PDF that plan describes, in the same
    two phases as :func:`pdf_ops.merge.merge_pipeline`.

The pipeline is a plain function: it writes only under the ``work_dir`` and to
the ``out_path`` its caller gives, creates no temp directory, and reports every
refusal as :class:`PacketError` (a ValueError), so it runs unchanged inside the
build route's job. It imports nothing from ``pdf_ops.jobs`` or
``pdf_ops.limits``. Staged inputs are already PDFs; it converts nothing.

Cover, divider and contents pages are planned here but rendered by renderers
that later stages add to :data:`_RENDERERS`. Until one is registered, a packet
that needs it is refused with ``unsupported``.
"""
import contextlib
import os

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import DictionaryObject, NameObject

from converters.options import DEFAULT_PAPER, PAPER_SIZES
from pdf_ops.merge import VALID_POSITIONS, _add_padding_blank, _number_overlay

# Design section 4.6: the output page count is capped.
MAX_OUTPUT_PAGES = 2000

VALID_ROTATIONS = (0, 90, 180, 270)

# The packet's output options (design section 3.1). The same defaults are
# pinned in tests/fixtures/packet-plan.json as ``option_defaults``.
DEFAULT_OPTIONS = {
    'paper': DEFAULT_PAPER,
    'page_numbers': True,
    'number_position': 'bottom-right',
    'start_number': 1,
    'number_generated': False,
    'bookmarks': True,
    'contents': False,
    'recto_starts': False,
}
_BOOL_OPTIONS = ('page_numbers', 'number_generated', 'bookmarks', 'contents', 'recto_starts')

# Phase 1's document, written under the caller's work_dir.
PHASE1_NAME = 'packet-phase1.pdf'

# Generated pages other than blanks and pads, keyed by plan kind. Each
# renderer is ``render(writer, page, packet, planned, opts) -> None``: it
# appends exactly one page to ``writer`` for the plan page ``page``, reading
# its text from ``packet`` (cover, section titles) and ``planned`` (contents
# entries and labels), at ``opts['paper']``. The divider and cover renderers
# come with stage B3 and the contents renderer with B5 (design, Build stages).
_RENDERERS = {}


class PacketError(ValueError):
    """A build refusal (maps to HTTP 400 with ``code``).

    ``code`` is one of ``invalid`` (malformed packet or options), ``expired``
    (a file item's hash has no staged PDF), ``stale`` (refs made on a different
    page count), ``out_of_range`` (a page ref outside its PDF), ``unreadable``
    (a staged PDF pypdf can't read), ``encrypted`` (a staged PDF that needs a
    password), ``empty`` (no page to build), ``too_large`` (over the output page
    cap) or ``unsupported`` (a generated page whose renderer isn't built yet).
    ``items`` lists ``{'id': item_id, 'code': code}`` for each bad item, so a
    refusal can mark every one; the first item's code is ``code``.
    """

    def __init__(self, code, message, items=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.items = list(items or [])


def normalize_options(options):
    """Merge ``options`` over :data:`DEFAULT_OPTIONS` and check each value.

    Unknown keys are dropped. Raises ``PacketError('invalid')``.
    """
    opts = dict(DEFAULT_OPTIONS)
    if options is not None and not isinstance(options, dict):
        raise PacketError('invalid', 'Packet options must be an object.')
    opts.update({k: v for k, v in (options or {}).items() if k in DEFAULT_OPTIONS})

    for key in _BOOL_OPTIONS:
        if not isinstance(opts[key], bool):
            raise PacketError('invalid', f"Option '{key}' must be true or false.")
    if not isinstance(opts['paper'], str) or opts['paper'] not in PAPER_SIZES:
        raise PacketError('invalid', f"Unknown paper size '{opts['paper']}'.")
    if opts['number_position'] not in VALID_POSITIONS:
        raise PacketError('invalid', f"Unknown number position '{opts['number_position']}'.")
    if not _is_index(opts['start_number']) or opts['start_number'] < 1:
        raise PacketError('invalid', 'start_number must be a whole number of at least 1.')
    return opts


# ---------------------------------------------------------------------------
# Page plan (design section 3.3)
# ---------------------------------------------------------------------------
def _is_index(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _check_shape(packet):
    """Raise ``PacketError('invalid')`` unless ``packet`` has the section 3.1 shape."""
    if not isinstance(packet, dict):
        raise PacketError('invalid', 'The packet must be an object.')
    cover = packet.get('cover')
    if cover is not None and not isinstance(cover, dict):
        raise PacketError('invalid', 'The cover must be an object or null.')
    sections = packet.get('sections')
    if not isinstance(sections, list):
        raise PacketError('invalid', 'The packet has no section list.')

    bad = []
    for section in sections:
        if not (isinstance(section, dict) and isinstance(section.get('id'), str)
                and isinstance(section.get('title', ''), str)
                and isinstance(section.get('divider', False), bool)
                and isinstance(section.get('items'), list)):
            raise PacketError('invalid', 'A section is malformed.')
        for item in section['items']:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                raise PacketError('invalid', 'An item is malformed.')
            if not _item_shape_ok(item):
                bad.append({'id': item['id'], 'code': 'invalid'})
    if bad:
        raise PacketError('invalid', 'Some items are malformed.', bad)


def _item_shape_ok(item):
    kind = item.get('kind')
    if kind == 'blank':
        return True
    if kind != 'file':
        return False
    if not isinstance(item.get('hash'), str) or not item['hash']:
        return False
    if not isinstance(item.get('label', ''), str):
        return False
    if 'src_pages' in item and not _is_index(item['src_pages']):
        return False
    pages = item.get('pages')
    if pages is None:
        return True
    if not isinstance(pages, list):
        return False
    for ref in pages:
        if not isinstance(ref, dict) or not _is_index(ref.get('src')):
            return False
        rot = ref.get('rot', 0)
        if not _is_index(rot) or rot not in VALID_ROTATIONS:
            return False
    return True


def _item_pages(section, item, page_counts, errors):
    """The plan pages one item gives (none for a file that isn't ready)."""
    if item['kind'] == 'blank':
        return [{'kind': 'blank', 'section': section['id'], 'item': item['id']}]

    count = page_counts.get(item['hash'])
    if count is None:
        return []   # not ready: the item keeps its place but gives no page
    refs = item.get('pages')
    if refs is None:
        refs = [{'src': i, 'rot': 0} for i in range(count)]
    else:
        # Fit (design section 3.1): refs are only good on the page count they
        # were made on. The server re-checks what the client reset.
        if 'src_pages' in item and item['src_pages'] != count:
            errors.append({'id': item['id'], 'code': 'stale'})
            return []
        if any(not 0 <= ref['src'] < count for ref in refs):
            errors.append({'id': item['id'], 'code': 'out_of_range'})
            return []
    return [{'kind': 'file', 'section': section['id'], 'item': item['id'],
             'hash': item['hash'], 'src': ref['src'], 'rot': ref.get('rot', 0)}
            for ref in refs]


def page_plan(packet, page_counts, contents_pages=1, options=None):
    """Plan every physical page of ``packet`` (design section 3.3).

    ``page_counts`` maps each ready file hash to its staged PDF's page count; a
    hash missing from it is not ready, and its items give no page.
    ``contents_pages`` is the contents page's length when ``contents`` is on.
    ``options`` replaces ``packet['options']`` when given.

    Returns ``{'pages': [...], 'entries': [...]}``. Each page is a dict with a
    ``kind`` (``cover``, ``contents``, ``pad``, ``divider``, ``file`` or
    ``blank``), its ``label`` (an int, or None for front matter) and ``print``;
    file pages add ``section``, ``item``, ``hash``, ``src`` and ``rot``, blanks
    ``section`` and ``item``, dividers ``section``. Each entry is a planned
    section or a file item that gives a page, with its ``title``, the ``label``
    of its first page and that page's 0-based ``index``.

    Raises ``PacketError``: ``invalid`` for a malformed packet, ``stale`` or
    ``out_of_range`` for refs that don't fit their file.
    """
    _check_shape(packet)
    opts = normalize_options(packet.get('options') if options is None else options)
    recto = opts['recto_starts']

    pages = []
    entries = []
    errors = []

    def pad_to_recto():
        # An odd physical page is an even 0-based index.
        if recto and len(pages) % 2 == 1:
            pages.append({'kind': 'pad'})

    # 1. Order: cover, contents, then each section that gives a page.
    if packet.get('cover') is not None:
        pages.append({'kind': 'cover'})
    if opts['contents']:
        pad_to_recto()
        pages.extend({'kind': 'contents'} for _ in range(contents_pages))

    first_body = None
    for section in packet['sections']:
        runs = [(item, _item_pages(section, item, page_counts, errors))
                for item in section['items']]
        if not any(item_pages for _item, item_pages in runs):
            continue   # a divider alone doesn't put a section in the plan

        # 2. Recto: padding only ever goes before a section start.
        pad_to_recto()
        if first_body is None:
            first_body = len(pages)
        entries.append({'kind': 'section', 'id': section['id'],
                        'title': section.get('title', ''), 'index': len(pages)})
        if section.get('divider'):
            pages.append({'kind': 'divider', 'section': section['id']})
        for item, item_pages in runs:
            if item_pages and item['kind'] == 'file':
                entries.append({'kind': 'item', 'id': item['id'], 'section': section['id'],
                                'title': item.get('label', ''), 'index': len(pages)})
            pages.extend(item_pages)

    if errors:
        raise PacketError(errors[0]['code'],
                          "Some files' page edits don't fit their files.", errors)

    # 3-5. Front matter has no label; from the first section's first page on,
    # every page uses one number. Generated pages print only when asked.
    start = opts['start_number']
    for index, page in enumerate(pages):
        if first_body is None or index < first_body:
            page['label'] = None
        else:
            page['label'] = start + index - first_body
        generated = page['kind'] in ('divider', 'pad')
        page['print'] = (opts['page_numbers'] and page['label'] is not None
                         and (opts['number_generated'] or not generated))

    # 6. Each entry carries its first page's label.
    for entry in entries:
        entry['label'] = pages[entry['index']]['label']

    return {'pages': pages, 'entries': entries}


# ---------------------------------------------------------------------------
# Pipeline (design section 4.8)
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _reading(item_ids):
    """Turn a pypdf failure on staged content into an ``unreadable`` refusal.

    Staged PDFs were opened once by the stage job, but a build still reads
    their pages and content streams, and pypdf raises a mix of exception types
    on a damaged file. None of them may escape as anything but PacketError.
    """
    try:
        yield
    except (PacketError, MemoryError, OSError):
        raise
    except Exception as exc:
        raise PacketError('unreadable', "A file in this packet can't be read as a PDF.",
                          [{'id': i, 'code': 'unreadable'} for i in item_ids]) from exc


def _open_staged(path, item_ids):
    """One ``PdfReader`` and its page count for a staged PDF, or a refusal."""
    if not os.path.isfile(path):
        raise PacketError('expired', 'A file in this packet is no longer staged.',
                          [{'id': i, 'code': 'expired'} for i in item_ids])
    with _reading(item_ids):
        reader = PdfReader(path)
        locked = reader.is_encrypted and not reader.decrypt('')
        count = 0 if locked else len(reader.pages)
    if locked:
        raise PacketError('encrypted', 'A file in this packet is password protected.',
                          [{'id': i, 'code': 'encrypted'} for i in item_ids])
    return reader, count


def _file_items(packet):
    """``{hash: [item ids]}`` for every file item, in packet order."""
    by_hash = {}
    for section in packet['sections']:
        for item in section['items']:
            if item['kind'] == 'file':
                by_hash.setdefault(item['hash'], []).append(item['id'])
    return by_hash


def _detach(page):
    """Give ``page`` its own content stream and resource dicts.

    With one reader per staged PDF, pypdf clones a source page's objects once,
    so two output pages from one source page (a duplicate, or one page in two
    items) share their /Contents and /Resources. pypdf rewrites those in place
    when it rotates content or merges an overlay, which would stamp or turn
    both pages. A page is detached before phase 2 changes it.
    """
    contents = page.get_contents()   # a new object, parsed from the shared one
    if '/Contents' in page:
        del page['/Contents']
    if contents is not None:
        page.replace_contents(contents)
    resources = page.get('/Resources')
    if resources is not None:
        own = DictionaryObject()
        for key, value in resources.get_object().items():
            resolved = value.get_object()
            own[NameObject(key)] = (DictionaryObject(resolved)
                                    if isinstance(resolved, DictionaryObject) else value)
        page[NameObject('/Resources')] = own


def _stamp_upright(page, label, position):
    """Stamp ``label`` with the merge overlay, in the page's visible box.

    The caller has moved any /Rotate into the content first, so the number sits
    the right way up at the bottom of the page as displayed.
    """
    box = page.cropbox
    overlay = _number_overlay(label, float(box.width), position, float(box.height))
    left, bottom = float(box.left), float(box.bottom)
    if left or bottom:
        page.merge_transformed_page(overlay, Transformation().translate(left, bottom))
    else:
        page.merge_page(overlay)


def _add_outline(writer, plan):
    """Flat bookmarks: one per file item, at its first page.

    Stage B3 replaces this with the nested outline (sections, then items).
    """
    for entry in plan['entries']:
        if entry['kind'] == 'item':
            writer.add_outline_item(entry['title'] or 'Untitled', entry['index'])


def _phase1(packet, plan, readers, opts, work_dir):
    """Append every planned page to one writer and write it under ``work_dir``.

    Consecutive pages from the same staged PDF go in one
    ``writer.append(reader, pages=[...])``; pages is a list of ints, since pypdf
    reads a tuple as range arguments. Rotation is added to each page here.
    """
    paper = PAPER_SIZES[opts['paper']]
    writer = PdfWriter()
    last_body = None   # writer index of the latest file or blank page
    pages = plan['pages']

    index = 0
    while index < len(pages):
        page = pages[index]
        if page['kind'] == 'file':
            end = index
            while (end < len(pages) and pages[end]['kind'] == 'file'
                   and pages[end]['hash'] == page['hash']):
                end += 1
            run = pages[index:end]
            base = len(writer.pages)
            with _reading(sorted({p['item'] for p in run})):
                writer.append(readers[page['hash']], pages=[p['src'] for p in run],
                              import_outline=False)
                for offset, ref in enumerate(run):
                    if ref['rot']:
                        writer.pages[base + offset].rotate(ref['rot'])
            last_body = len(writer.pages) - 1
            index = end
            continue

        if page['kind'] == 'blank':
            # Design section 2.1: a blank matches the body page before it,
            # as merge's padding does, or takes the packet's paper size.
            if last_body is None:
                writer.add_blank_page(width=paper[0], height=paper[1])
            else:
                _add_padding_blank(writer, writer.pages[last_body])
            last_body = len(writer.pages) - 1
        elif page['kind'] == 'pad':
            # A pad is never first: it follows the page before a section start.
            _add_padding_blank(writer, writer.pages[-1])
        else:
            _RENDERERS[page['kind']](writer, page, packet, plan, opts)
        index += 1

    phase1_path = os.path.join(work_dir, PHASE1_NAME)
    with _reading([]), open(phase1_path, 'wb') as fh:
        writer.write(fh)
    writer.close()
    return phase1_path


def packet_pipeline(plan, staged_paths, options, work_dir, out_path):
    """Build the packet PDF and write it to ``out_path``.

    ``plan`` is the validated packet document (design section 3.1).
    ``staged_paths`` maps each file item's ``hash`` to its staged PDF's path.
    ``options`` are the packet's output options (``plan['options']`` when
    None). Phase 1's document is written under ``work_dir``; nothing else is
    written but ``out_path``.

    Returns the page plan the PDF was built from (see :func:`page_plan`).
    Raises :class:`PacketError` for every refusal.
    """
    _check_shape(plan)
    opts = normalize_options(plan.get('options') if options is None else options)
    by_hash = _file_items(plan)

    missing = [{'id': i, 'code': 'expired'}
               for h, ids in by_hash.items() if h not in staged_paths for i in ids]
    if missing:
        raise PacketError('expired', 'Some files in this packet are no longer staged.', missing)

    # One reader per staged PDF for the whole build.
    readers, counts = {}, {}
    for h, ids in by_hash.items():
        readers[h], counts[h] = _open_staged(staged_paths[h], ids)

    # The contents renderer (B5) measures its length here before planning.
    planned = page_plan(plan, counts, contents_pages=1, options=opts)
    pages = planned['pages']

    for kind in sorted({p['kind'] for p in pages} - {'file', 'blank', 'pad'}):
        if kind not in _RENDERERS:
            raise PacketError('unsupported', f'{kind.capitalize()} pages are not available yet.')
    if not any(p['kind'] in ('file', 'blank') for p in pages):
        raise PacketError('empty', 'This packet has no pages to build.')
    if len(pages) > MAX_OUTPUT_PAGES:
        raise PacketError('too_large', f'This packet would have {len(pages)} pages. '
                                       f'The limit is {MAX_OUTPUT_PAGES}.')

    # --- Phase 1: assemble every page, then write it to a file. -------------
    # Routing every source through PdfWriter.append normalizes page and font
    # resources; stamping the number overlay directly onto raw converter output
    # can corrupt merged font dicts (missing /Subtype), so, as in
    # merge_pipeline, numbers are stamped only on the re-read phase-1 document.
    phase1_path = _phase1(plan, planned, readers, opts, work_dir)

    # --- Phase 2: stamp numbers onto a fresh writer, then the outline. ------
    reader = PdfReader(phase1_path)
    writer = PdfWriter()
    for info, page in zip(pages, reader.pages):
        with _reading([info['item']] if 'item' in info else []):
            # Attach the page first; pypdf merges only onto a writer's pages.
            new_page = writer.add_page(page)
            if info['print']:
                _detach(new_page)
                # Design section 4.8: the number goes in the displayed
                # orientation, so /Rotate moves into the content first.
                if new_page.rotation:
                    new_page.transfer_rotation_to_content()
                _stamp_upright(new_page, info['label'], opts['number_position'])
    if opts['bookmarks']:
        _add_outline(writer, planned)
    with _reading([]), open(out_path, 'wb') as fh:
        writer.write(fh)
    return planned
