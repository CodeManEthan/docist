"""Configurable merge pipeline.

Merges a list of source PDFs into one document, optionally:
  * inserting a blank page after any odd-page source (duplex friendliness),
  * stamping sequential page numbers (position + start value configurable),
  * adding one top-level outline (bookmark) entry per source file.

The whole document is assembled in a *single* ``PdfWriter`` so that an outline
survives page-number stamping.  The previous implementation merged with a
separate merger object and then rewrote every page through a fresh
``PdfWriter`` to add numbers -- any outline created before that rewrite was
discarded.  Here numbers are stamped in place on the writer's own pages and the
outline is added last, pointing at page indices tracked while the pages were
appended.

Two merge modes are supported:
  * ``standard`` (default) -- concatenate sources in order (the behavior above).
  * ``interleave`` -- combine exactly two sources (fronts ``A`` and backs
    ``B``, typically separately-scanned front/back page stacks) into
    ``A1, B1, A2, B2, ...``.  Back-side stacks off a flatbed/ADF usually come
    out in reverse order, so ``reverse_second`` (default true) flips ``B``
    before pairing.  Both interleave sources are still routed through a
    concatenating ``PdfWriter`` first (the same font-normalization phase 1);
    the pages are only reordered afterwards on the final ``PdfWriter`` in
    phase 2, preserving the corruption-avoiding two-phase structure.
"""
import io

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter

BLANK_PDF_PATH = 'Blank PDF Document.pdf'

VALID_POSITIONS = ('bottom-right', 'bottom-center', 'bottom-left')
VALID_MODES = ('standard', 'interleave')

DEFAULT_OPTIONS = {
    'page_numbers': True,
    'number_position': 'bottom-right',
    'start_number': 1,
    'blank_pages': True,
    'bookmarks': True,
    # Merge mode: 'standard' concatenates; 'interleave' zips two front/back
    # scans into A1,B1,A2,B2,...  reverse_second flips the back stack first
    # (default true: flatbed/ADF back-side scans usually come out reversed).
    'mode': 'standard',
    'reverse_second': True,
}


class OptionsError(ValueError):
    """Raised for invalid user-supplied merge options (maps to HTTP 400)."""


def _truthy(value, default):
    """Interpret a form value ("true"/"false"/...) as a bool."""
    if value is None:
        return default
    return str(value).strip().lower() in ('true', '1', 'yes', 'on')


def parse_options(form):
    """Validate/normalize a mapping of raw form fields into an options dict.

    Raises ``OptionsError`` (a ValueError) on invalid input so routes can map
    it to a 400.  Missing fields fall back to :data:`DEFAULT_OPTIONS`, which
    reproduce the app's original behavior exactly.
    """
    get = form.get if form is not None else (lambda *_a, **_k: None)

    opts = {
        'page_numbers': _truthy(get('page_numbers'), DEFAULT_OPTIONS['page_numbers']),
        'blank_pages': _truthy(get('blank_pages'), DEFAULT_OPTIONS['blank_pages']),
        'bookmarks': _truthy(get('bookmarks'), DEFAULT_OPTIONS['bookmarks']),
    }

    position = get('number_position')
    if position in (None, ''):
        position = DEFAULT_OPTIONS['number_position']
    if position not in VALID_POSITIONS:
        raise OptionsError(
            f"Invalid number_position '{position}'. "
            f"Expected one of: {', '.join(VALID_POSITIONS)}."
        )
    opts['number_position'] = position

    raw_start = get('start_number')
    if raw_start in (None, ''):
        opts['start_number'] = DEFAULT_OPTIONS['start_number']
    else:
        try:
            start = int(str(raw_start).strip())
        except (TypeError, ValueError):
            raise OptionsError(
                f"start_number must be a whole number, got '{raw_start}'."
            )
        if start < 1:
            raise OptionsError("start_number must be an integer >= 1.")
        opts['start_number'] = start

    # Merge mode.  The 'mode'/'reverse_second' keys are only emitted for the
    # interleave case: for standard merges parse_options returns exactly the
    # historical five keys (normalize_options supplies the defaults), so the
    # standard code path -- and its exact-shape tests -- are unaffected.
    mode = get('mode')
    if mode in (None, ''):
        mode = DEFAULT_OPTIONS['mode']
    if mode not in VALID_MODES:
        raise OptionsError(
            f"Invalid mode '{mode}'. Expected one of: {', '.join(VALID_MODES)}."
        )
    if mode == 'interleave':
        opts['mode'] = 'interleave'
        opts['reverse_second'] = _truthy(
            get('reverse_second'), DEFAULT_OPTIONS['reverse_second']
        )

    return opts


def normalize_options(options):
    """Merge a (possibly partial / None) options dict over the defaults."""
    opts = dict(DEFAULT_OPTIONS)
    if options:
        opts.update({k: v for k, v in options.items() if k in DEFAULT_OPTIONS})
    return opts


def _number_xy(position, page_width):
    """Bottom-margin (x, y) for the page number given a position keyword."""
    y = 30
    if position == 'bottom-left':
        return 50, y
    if position == 'bottom-center':
        return page_width / 2, y
    # bottom-right (default): matches the original x = width - 50 placement.
    return page_width - 50, y


def _number_overlay(number, page_width, position):
    """Build a single-page PDF overlay bearing ``number`` at ``position``."""
    packet = io.BytesIO()
    # pagesize=letter mirrors the original implementation exactly.
    can = canvas.Canvas(packet, pagesize=letter)
    x, y = _number_xy(position, page_width)
    can.setFont("Helvetica", 10)
    can.drawString(x, y, str(number))
    can.save()
    packet.seek(0)
    return PdfReader(packet).pages[0]


def merge_pipeline(sources, options=None):
    """Assemble the merged PDF and return a ready-to-write ``PdfWriter``.

    ``sources`` is an iterable of ``(path, title)`` pairs -- ``title`` is the
    label used for that source's bookmark (typically the original filename
    without extension).  A bare path string is also accepted, in which case the
    title is derived from its basename.

    The writer's pages are stamped in place and the outline is added last, so
    bookmarks point at the correct first page of each source and survive.

    In ``interleave`` mode exactly two sources are required and are zipped into
    ``A1, B1, A2, B2, ...`` (see :func:`_interleave_pipeline`).
    """
    opts = normalize_options(options)

    if opts['mode'] == 'interleave':
        return _interleave_pipeline(sources, opts)

    # --- Phase 1: concatenate sources (+ blank padding) into a writer. -------
    # Routing every source through PdfWriter.append normalizes page/font
    # resources; stamping the number overlay directly onto raw converter output
    # instead can occasionally corrupt merged font dicts (missing /Subtype), so
    # we keep this normalization pass.  We track each source's first-page index
    # here so bookmarks can target it after the final writer is built.
    merger = PdfWriter()
    bookmark_targets = []  # (title, page_index_of_source_start)
    running = 0

    for source in sources:
        if isinstance(source, (tuple, list)):
            path, title = source[0], source[1]
        else:
            path, title = source, _title_from_path(source)

        page_count = len(PdfReader(path).pages)
        bookmark_targets.append((title, running))

        merger.append(path)
        running += page_count

        if opts['blank_pages'] and page_count % 2 == 1:
            merger.append(BLANK_PDF_PATH, pages=(0, 1))
            running += 1

    merged_buffer = io.BytesIO()
    merger.write(merged_buffer)
    merger.close()
    merged_buffer.seek(0)

    # --- Phase 2: stamp page numbers onto a fresh writer, then outline. ------
    reader = PdfReader(merged_buffer)
    writer = PdfWriter()
    start = opts['start_number']

    for offset, page in enumerate(reader.pages):
        # Attach the page to the writer *first*; pypdf only supports merging
        # onto pages that already belong to a writer.
        new_page = writer.add_page(page)
        if opts['page_numbers']:
            page_width = float(new_page.mediabox.width)
            overlay = _number_overlay(start + offset, page_width,
                                      opts['number_position'])
            new_page.merge_page(overlay)

    # Outline is added to the FINAL writer, so it survives page-number stamping.
    if opts['bookmarks']:
        for title, index in bookmark_targets:
            writer.add_outline_item(title, index)

    return writer


def _resolve_source(source):
    """Return ``(path, title)`` for a ``(path, title)`` pair or a bare path."""
    if isinstance(source, (tuple, list)):
        return source[0], source[1]
    return source, _title_from_path(source)


def _interleave_pipeline(sources, opts):
    """Zip exactly two sources (fronts ``A`` / backs ``B``) into one document.

    Output order is ``A1, B1, A2, B2, ...``; ``opts['reverse_second']`` flips
    ``B`` before pairing (default true for reversed back-side scans).  When
    ``A`` has one extra page, that final front lands last with no ``B`` partner;
    a page-count difference greater than one is rejected (``OptionsError``).

    Semantics notes:
      * ``blank_pages`` is intentionally IGNORED here -- padding an odd source
        would shift the pairing and desync fronts from backs.
      * ``page_numbers`` and ``bookmarks`` still apply.  Bookmarks are two
        entries, one per source, each pointing at that source's first page in
        the interleaved result: ``A`` -> index 0, ``B`` -> index 1 (``B``'s
        first output page always immediately follows ``A``'s first page,
        regardless of whether ``B`` was reversed).

    The two sources are still concatenated through a first ``PdfWriter`` (phase
    1 font/resource normalization) and only reordered on the final
    ``PdfWriter`` in phase 2, keeping the two-phase structure that avoids the
    font-corruption bug.
    """
    sources = list(sources)
    if len(sources) != 2:
        raise OptionsError(
            "Interleave mode requires exactly 2 source files "
            f"(got {len(sources)})."
        )

    (a_path, a_title), (b_path, b_title) = (
        _resolve_source(sources[0]),
        _resolve_source(sources[1]),
    )

    len_a = len(PdfReader(a_path).pages)
    len_b = len(PdfReader(b_path).pages)
    if abs(len_a - len_b) > 1:
        raise OptionsError(
            "Interleave requires the two files to have the same number of "
            f"pages (or differ by at most one). Got {len_a} and {len_b}."
        )

    # --- Phase 1: normalize BOTH sources through a writer (A then B). --------
    merger = PdfWriter()
    merger.append(a_path)
    merger.append(b_path)
    merged_buffer = io.BytesIO()
    merger.write(merged_buffer)
    merger.close()
    merged_buffer.seek(0)

    reader = PdfReader(merged_buffer)
    a_pages = [reader.pages[i] for i in range(len_a)]
    b_pages = [reader.pages[len_a + i] for i in range(len_b)]
    if opts['reverse_second']:
        b_pages = list(reversed(b_pages))

    # Zip fronts and backs; a leftover final page (the odd one out) goes last.
    ordered = []
    for i in range(max(len_a, len_b)):
        if i < len_a:
            ordered.append(a_pages[i])
        if i < len_b:
            ordered.append(b_pages[i])

    # --- Phase 2: stamp numbers onto a fresh writer, then outline. -----------
    writer = PdfWriter()
    start = opts['start_number']
    for offset, page in enumerate(ordered):
        new_page = writer.add_page(page)
        if opts['page_numbers']:
            page_width = float(new_page.mediabox.width)
            overlay = _number_overlay(start + offset, page_width,
                                      opts['number_position'])
            new_page.merge_page(overlay)

    if opts['bookmarks']:
        writer.add_outline_item(a_title, 0)
        # B's first output page immediately follows A's first page -> index 1.
        writer.add_outline_item(b_title, 1)

    return writer


def _title_from_path(path):
    import os
    return os.path.splitext(os.path.basename(str(path)))[0]


# ---------------------------------------------------------------------------
# Backward-compatible helpers (retained for import stability; the app now uses
# merge_pipeline).  Nothing else in the repo imports these, but they remain so
# any external caller keeps working.
# ---------------------------------------------------------------------------
def add_page_numbers(pdf_path, output_path):
    """Add page numbers to the bottom right corner of each page (legacy)."""
    reader = PdfReader(pdf_path)
    writer = PdfWriter()

    for page_num in range(len(reader.pages)):
        new_page = writer.add_page(reader.pages[page_num])
        page_width = float(new_page.mediabox.width)
        overlay = _number_overlay(page_num + 1, page_width, 'bottom-right')
        new_page.merge_page(overlay)

    with open(output_path, 'wb') as output_file:
        writer.write(output_file)


def merge_pdfs_with_blanks(pdf_files):
    """Merge PDFs, adding a blank page after any odd-page source (legacy)."""
    merger = PdfWriter()

    for pdf_file in pdf_files:
        reader = PdfReader(pdf_file)
        page_count = len(reader.pages)
        merger.append(pdf_file)
        if page_count % 2 == 1:
            merger.append(BLANK_PDF_PATH, pages=(0, 1))

    return merger
