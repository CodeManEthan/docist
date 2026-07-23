"""Print-prep imposition: N-up (2/4 pages per sheet) and saddle-stitch booklets.

Pure PDF-processing functions with no Flask dependency, built on PyPDF2. Every
source page is scaled *preserving its aspect ratio* and centered inside its
target slot; empty slots are left blank (nothing is merged onto them).

Page indices used internally are 0-based. Geometry uses PDF points (1/72").

Sheet sizes
-----------
* Letter portrait  : 612 x 792
* Letter landscape : 792 x 612

2-up   -> one letter *landscape* sheet, two slots side by side (396 x 612 each).
4-up   -> one letter *portrait*  sheet, a 2x2 grid (306 x 396 each), filled in
          reading order: left-right, top-bottom.
booklet-> letter *landscape* 2-up sheets ordered as a saddle-stitch signature
          so that printing duplex (flip on short edge) and folding in half
          yields correct reading order.
"""
from PyPDF2 import PdfReader, PdfWriter, Transformation, PageObject

# Page geometry (PDF points).
LETTER_PORTRAIT = (612.0, 792.0)
LETTER_LANDSCAPE = (792.0, 612.0)

_PAGE_SIZES = {
    "letter-landscape": LETTER_LANDSCAPE,
    "letter-portrait": LETTER_PORTRAIT,
}


def booklet_page_order(page_count):
    """Return the saddle-stitch sheet sequence for ``page_count`` source pages.

    ``page_count`` is the number of *real* source pages. It is padded up to the
    next multiple of 4 internally (a booklet always has a multiple of 4 pages so
    it folds cleanly). The result is a list of ``(left, right)`` tuples — one per
    2-up landscape sheet — using 0-based source-page indices. A slot that falls
    on a padding page is ``None`` (a blank).

    The order interleaves the outer and inner pairs the way a folded signature
    stacks. For an 8-page doc the 1-based pairs are::

        (8, 1), (2, 7), (6, 3), (4, 5)

    i.e. front(8,1) back(2,7) front(6,3) back(4,5). Printed duplex (flip on the
    short edge) and folded in half, the pages read 1..8 in order.

    The number of sheets returned is ``padded / 2``.
    """
    if page_count is None or page_count < 1:
        raise ValueError("A booklet needs at least one page.")

    padded = page_count + (-page_count % 4)  # round up to a multiple of 4

    def idx(one_based):
        """1-based page number -> 0-based index, or None if it is padding."""
        return one_based - 1 if one_based <= page_count else None

    pairs = []
    lo, hi = 1, padded
    flip = False
    while lo < hi:
        # Alternate which side the outer/inner page lands on so the folded
        # signature reads correctly: (hi, lo), (lo, hi), (hi, lo), ...
        left, right = (lo, hi) if flip else (hi, lo)
        pairs.append((idx(left), idx(right)))
        lo += 1
        hi -= 1
        flip = not flip
    return pairs


def _place(sheet_page, src_page, x, y, w, h):
    """Scale ``src_page`` to fit the ``(x, y, w, h)`` slot and merge it centered.

    Aspect ratio is preserved. ``sheet_page`` is mutated in place. The source
    page's mediabox origin is normalized to (0, 0) first, so pages whose box is
    offset still land correctly.
    """
    box = src_page.mediabox
    llx, lly = float(box.left), float(box.bottom)
    src_w, src_h = float(box.width), float(box.height)
    if src_w <= 0 or src_h <= 0:
        return

    scale = min(w / src_w, h / src_h)
    # Center the scaled page within the slot.
    tx = x + (w - src_w * scale) / 2.0
    ty = y + (h - src_h * scale) / 2.0

    transform = (
        Transformation()
        .translate(-llx, -lly)
        .scale(scale)
        .translate(tx, ty)
    )
    src_page.add_transformation(transform)
    sheet_page.merge_page(src_page)


def nup_pdf(input_path, output_path, n=2, page_size="letter-landscape"):
    """Impose ``n`` source pages per sheet (N-up) and write ``output_path``.

    ``n`` must be 2 or 4.

    * ``n == 2``: letter *landscape* sheet (792 x 612); two source pages side by
      side, left then right.
    * ``n == 4``: letter *portrait* sheet (612 x 792); a 2x2 grid filled
      left-right, top-bottom.

    Each source page is scaled to fit its slot preserving aspect ratio and
    centered. The final sheet's leftover slots stay blank. Returns
    ``output_path``.
    """
    if n not in (2, 4):
        raise ValueError("N-up supports only 2 or 4 pages per sheet.")

    reader = PdfReader(input_path)
    page_count = len(reader.pages)
    if page_count < 1:
        raise ValueError("The PDF has no pages to impose.")

    if n == 2:
        sheet_w, sheet_h = LETTER_LANDSCAPE
        # Two slots side by side.
        slots = [
            (0.0, 0.0, sheet_w / 2.0, sheet_h),
            (sheet_w / 2.0, 0.0, sheet_w / 2.0, sheet_h),
        ]
    else:  # n == 4
        sheet_w, sheet_h = LETTER_PORTRAIT
        cw, ch = sheet_w / 2.0, sheet_h / 2.0
        # Reading order: top-left, top-right, bottom-left, bottom-right.
        # Row 0 is the TOP half (higher y) since PDF y grows upward.
        slots = [
            (0.0, ch, cw, ch),   # top-left
            (cw, ch, cw, ch),    # top-right
            (0.0, 0.0, cw, ch),  # bottom-left
            (cw, 0.0, cw, ch),   # bottom-right
        ]

    writer = PdfWriter()
    for start in range(0, page_count, n):
        sheet = PageObject.create_blank_page(width=sheet_w, height=sheet_h)
        for slot_i in range(n):
            src_i = start + slot_i
            if src_i >= page_count:
                break  # leftover slots stay blank
            src_page = reader.pages[src_i]
            x, y, w, h = slots[slot_i]
            _place(sheet, src_page, x, y, w, h)
        writer.add_page(sheet)

    with open(output_path, "wb") as fh:
        writer.write(fh)
    return output_path


def booklet_pdf(input_path, output_path):
    """Impose ``input_path`` as a saddle-stitch booklet, writing ``output_path``.

    The input is padded to a multiple of 4 with blank pages, then emitted as
    2-up letter *landscape* sheets in signature order (see
    :func:`booklet_page_order`). Print the result duplex, flip on the short
    edge, and fold in half to get a correctly ordered booklet. Returns
    ``output_path``.
    """
    reader = PdfReader(input_path)
    page_count = len(reader.pages)
    if page_count < 1:
        raise ValueError("The PDF has no pages to impose.")

    sheet_w, sheet_h = LETTER_LANDSCAPE
    left_slot = (0.0, 0.0, sheet_w / 2.0, sheet_h)
    right_slot = (sheet_w / 2.0, 0.0, sheet_w / 2.0, sheet_h)

    writer = PdfWriter()
    for left_idx, right_idx in booklet_page_order(page_count):
        sheet = PageObject.create_blank_page(width=sheet_w, height=sheet_h)
        if left_idx is not None:
            _place(sheet, reader.pages[left_idx], *left_slot)
        if right_idx is not None:
            _place(sheet, reader.pages[right_idx], *right_slot)
        writer.add_page(sheet)

    with open(output_path, "wb") as fh:
        writer.write(fh)
    return output_path
