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

Cover, divider and contents pages are planned here and drawn by the renderers
in :data:`_RENDERERS`. Stage B3 registers the cover and divider; until B5
registers the contents page, a packet that needs one is refused with
``unsupported``.
"""
import contextlib
import io
import os

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import DictionaryObject, NameObject
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

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
# The finished PDF is written here under work_dir, then moved to out_path.
OUT_PART_NAME = 'packet-out.part.pdf'

# Generated pages other than blanks and pads, keyed by plan kind. Each
# renderer is ``render(writer, page, packet, planned, opts) -> None``: it
# appends exactly one page to ``writer`` for the plan page ``page``, reading
# its text from ``packet`` (cover, section titles) and ``planned`` (contents
# entries and labels), at ``opts['paper']``. The divider and cover renderers
# are stage B3's, registered below them; the contents renderer comes with B5
# (design, Build stages).
_RENDERERS = {}

# What a section with no title is called on its divider and in the outline,
# as the page calls its heading.
UNTITLED_SECTION = 'Untitled section'
# The cover's text fields (design section 3.1); each is a string when given.
COVER_FIELDS = ('title', 'subtitle', 'date', 'note')


class PacketError(ValueError):
    """A build refusal (maps to HTTP 400 with ``code``).

    ``code`` is one of ``invalid`` (malformed packet, options or staged map, or
    two items with one id), ``expired`` (a file item's hash has no staged PDF),
    ``stale`` (refs made on a different page count), ``out_of_range`` (a page
    ref outside its PDF), ``unreadable`` (a staged PDF pypdf can't read),
    ``encrypted`` (a staged PDF that needs a password), ``empty`` (no page to
    build), ``too_large`` (over the output page cap) or ``unsupported`` (a
    generated page whose renderer isn't built yet).
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
    if cover is not None and not all(isinstance(cover.get(f, ''), str) for f in COVER_FIELDS):
        raise PacketError('invalid', "The cover's title, subtitle, date and note must be text.")
    sections = packet.get('sections')
    if not isinstance(sections, list):
        raise PacketError('invalid', 'The packet has no section list.')

    bad = []
    seen = set()
    for section in sections:
        if not (isinstance(section, dict) and isinstance(section.get('id'), str)
                and isinstance(section.get('title', ''), str)
                and isinstance(section.get('divider', False), bool)
                and isinstance(section.get('items'), list)):
            raise PacketError('invalid', 'A section is malformed.')
        for item in section['items']:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                raise PacketError('invalid', 'An item is malformed.')
            if item['id'] in seen:
                # Entries and refusals name items by id, so ids must be unique.
                raise PacketError('invalid', f"Two items share the id '{item['id']}'.",
                                  [{'id': item['id'], 'code': 'invalid'}])
            seen.add(item['id'])
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
    # Design section 4.3: explicit refs carry the page count they were made
    # on, so the fit check below always has one to compare.
    if not isinstance(pages, list) or 'src_pages' not in item:
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
        if item['src_pages'] != count:
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


# Display rotation (clockwise, as /Rotate) -> the matrix that takes a point
# on the page as displayed, measured from its visible box's lower left, into
# user space measured from the same corner. ``w`` and ``h`` are the visible
# box's user-space width and height.
_DISPLAY_TO_USER = {
    0: lambda w, h: (1, 0, 0, 1, 0, 0),
    90: lambda w, h: (0, 1, -1, 0, w, 0),
    180: lambda w, h: (-1, 0, 0, -1, w, h),
    270: lambda w, h: (0, -1, 1, 0, 0, h),
}


def _stamp_upright(page, label, position):
    """Stamp ``label`` with the merge overlay, upright in the page as displayed.

    Design section 4.8: the number goes in the displayed orientation. The page
    keeps its /Rotate and its content; the overlay is drawn at the displayed
    size of the visible (crop) box and merged through the inverse of the
    display rotation, so nothing already on the page moves, links and form
    fields included.
    """
    box = page.cropbox
    width, height = float(box.width), float(box.height)
    rotation = int(page.rotation) % 360
    if rotation not in _DISPLAY_TO_USER:
        rotation = 0   # not a quarter turn: viewers ignore it, so do we
    if rotation in (90, 270):
        shown_w, shown_h = height, width
    else:
        shown_w, shown_h = width, height
    overlay = _number_overlay(label, shown_w, position, shown_h)
    a, b, c, d, e, f = _DISPLAY_TO_USER[rotation](width, height)
    ctm = (a, b, c, d, e + float(box.left), f + float(box.bottom))
    page.merge_transformed_page(overlay, Transformation(ctm))


def _add_outline(writer, plan, packet):
    """Bookmarks from the plan's entries (design section 3.3 rule 6).

    With more than one section in the packet, each planned section is a
    bookmark at its first page (its divider when it has one) and its file
    items nest under it. A packet of one section has no section headings on
    the page, so its file items are top-level bookmarks.
    """
    nested = len(packet['sections']) > 1
    parents = {}
    for entry in plan['entries']:
        if entry['kind'] == 'section':
            if nested:
                parents[entry['id']] = writer.add_outline_item(
                    _clean(entry['title']) or UNTITLED_SECTION, entry['index'])
        else:
            writer.add_outline_item(_clean(entry['title']) or 'Untitled', entry['index'],
                                    parent=parents.get(entry['section']))


# ---------------------------------------------------------------------------
# Generated pages: cover and divider (stage B3)
# ---------------------------------------------------------------------------
_MARGIN = 72
_LEADING = 1.25
_ELLIPSIS = '\u2026'
# A field is cut to this many characters (after cleaning) before it is
# fitted: a crafted megabyte title would otherwise be re-wrapped at each size
# step for nothing. No block can show this many: the narrowest glyph the
# fonts draw (ZapfDingbats U+2758, 0.138 em) at the smallest size (the note,
# 9 pt, 3 lines on Letter's 468 pt) fits about 1131.
_TEXT_CUT = 2000


def _clean(text):
    """``text`` on one line: runs of whitespace and control characters become
    one space. The fields are short single lines (design section 2.1)."""
    return ' '.join(''.join(ch if ch.isprintable() else ' ' for ch in str(text or '')).split())


def _break_word(word, font, size, width):
    """Pieces of one word too wide for a line, each fitting ``width``."""
    pieces, piece = [], ''
    for ch in word:
        if piece and stringWidth(piece + ch, font, size) > width:
            pieces.append(piece)
            piece = ch
        else:
            piece += ch
    return pieces + ([piece] if piece else [])


def _wrap(text, font, size, width):
    """Greedy word wrap; a word wider than a line is broken where it fills one."""
    lines, line = [], ''
    for word in text.split(' '):
        candidate = f'{line} {word}' if line else word
        if stringWidth(candidate, font, size) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
        pieces = _break_word(word, font, size, width)
        lines.extend(pieces[:-1])
        line = pieces[-1] if pieces else ''
    if line:
        lines.append(line)
    return lines


def _fit(text, font, size, min_size, width, max_lines):
    """``(lines, size)`` for ``text``: the largest size from ``size`` down to
    ``min_size`` at which it fits in ``max_lines``; at ``min_size`` the lines
    past ``max_lines`` are cut and the last one ends in an ellipsis."""
    while True:
        lines = _wrap(text, font, size, width)
        if len(lines) <= max_lines or size <= min_size:
            break
        size = max(min_size, size - 2)
    if len(lines) > max_lines:
        last = lines[max_lines - 1]
        while last and stringWidth(last + _ELLIPSIS, font, size) > width:
            last = last[:-1]
        lines = lines[:max_lines - 1] + [last.rstrip() + _ELLIPSIS]
    return lines, size


def _draw_blocks(c, width, height, blocks, centre):
    """Draw text blocks centred across the page, the stack centred on ``centre``
    (a height from the bottom). Each block is ``(text, font, size, min_size,
    max_lines, gap_after)``; empty texts are left out."""
    laid = []
    for text, font, size, min_size, max_lines, gap in blocks:
        text = _clean(text)[:_TEXT_CUT].rstrip()
        if text:
            lines, fitted = _fit(text, font, size, min_size, width - 2 * _MARGIN, max_lines)
            laid.append((lines, font, fitted, gap))
    total = sum(len(lines) * size * _LEADING + gap for lines, _f, size, gap in laid)
    if laid:
        total -= laid[-1][3]
    y = centre + total / 2
    for lines, font, size, gap in laid:
        c.setFont(font, size)
        for line in lines:
            y -= size * _LEADING
            # The baseline sits a fifth of the leading above the line's bottom.
            c.drawCentredString(width / 2, y + size * 0.25, line)
        y -= gap


def _generated_page(opts, blocks, centre_ratio):
    """One page of the packet's paper, portrait, with ``blocks`` drawn on it."""
    width, height = PAPER_SIZES[opts['paper']]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    c.setFillGray(0)
    _draw_blocks(c, width, height, blocks, height * centre_ratio)
    c.showPage()
    c.save()
    return PdfReader(io.BytesIO(buf.getvalue())).pages[0]


def _render_divider(writer, page, packet, planned, opts):
    """Design section 2.1: the section title, large, centred."""
    section = next(s for s in packet['sections'] if s['id'] == page['section'])
    title = _clean(section.get('title', '')) or UNTITLED_SECTION
    writer.add_page(_generated_page(opts, [(title, 'Helvetica-Bold', 36, 16, 4, 0)], 0.5))


def _render_cover(writer, page, packet, planned, opts):
    """Design section 2.1: title, subtitle, date and a short line of text,
    centred, a little above the middle of the page."""
    cover = packet['cover']
    writer.add_page(_generated_page(opts, [
        (cover.get('title', ''), 'Helvetica-Bold', 32, 16, 4, 18),
        (cover.get('subtitle', ''), 'Helvetica', 20, 12, 3, 28),
        (cover.get('date', ''), 'Helvetica', 13, 10, 2, 22),
        (cover.get('note', ''), 'Helvetica-Oblique', 12, 9, 3, 0),
    ], 0.58))


_RENDERERS['divider'] = _render_divider
_RENDERERS['cover'] = _render_cover


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
    None); when given they replace the packet's options as a whole, and keys
    they leave out take :data:`DEFAULT_OPTIONS`, not the packet's values.

    Phase 1's document is written under ``work_dir`` and left there. The
    finished PDF is written under ``work_dir`` too and moved to ``out_path``
    with ``os.replace``, so ``out_path`` appears whole or not at all;
    ``work_dir`` and ``out_path`` must be on one file system. Nothing else is
    written.

    Returns the page plan the PDF was built from (see :func:`page_plan`).
    Raises :class:`PacketError` for every refusal.
    """
    if not isinstance(staged_paths, dict):
        raise PacketError('invalid', 'The staged files must be a map of hash to path.')
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
                _stamp_upright(new_page, info['label'], opts['number_position'])
    if opts['bookmarks']:
        _add_outline(writer, planned, plan)
    part_path = os.path.join(work_dir, OUT_PART_NAME)
    try:
        with _reading([]), open(part_path, 'wb') as fh:
            writer.write(fh)
        os.replace(part_path, out_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(part_path)
        raise
    return planned
