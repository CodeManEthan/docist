"""Single-PDF page operations: parse ranges, extract, remove, rotate, split.

Pure PDF-processing functions with no Flask dependency, built on pypdf.
Page indices returned/consumed by these helpers are 0-based; the *spec*
strings accepted by :func:`parse_page_ranges` are 1-based inclusive
(the way a human would describe pages).
"""
import os

from pypdf import PdfReader, PdfWriter


def parse_page_ranges(spec, page_count):
    """Parse a page-range spec into a sorted list of unique 0-based indices.

    ``spec`` is a comma-separated list of single pages and inclusive ranges,
    all 1-based, e.g. ``"1-3,5,8-10"``. Whitespace around tokens is ignored.

    Returns a sorted list of distinct 0-based page indices.

    Raises ``ValueError`` with a clear message when the spec is empty, has
    bad syntax, or references a page outside ``1..page_count``.
    """
    if page_count < 1:
        raise ValueError("The PDF has no pages to select.")

    if spec is None or not str(spec).strip():
        raise ValueError("No page range provided. Try something like '1-3,5,8-10'.")

    indices = set()
    for raw in str(spec).split(","):
        token = raw.strip()
        if not token:
            raise ValueError(
                "Empty range segment (check for a stray or doubled comma)."
            )

        if "-" in token:
            parts = token.split("-")
            if len(parts) != 2 or parts[0].strip() == "" or parts[1].strip() == "":
                raise ValueError(f"Invalid page range: '{token}'.")
            start_s, end_s = parts[0].strip(), parts[1].strip()
            if not start_s.isdigit() or not end_s.isdigit():
                raise ValueError(f"Invalid page range: '{token}'.")
            start, end = int(start_s), int(end_s)
            if start < 1 or end < 1:
                raise ValueError("Page numbers start at 1.")
            if start > end:
                raise ValueError(
                    f"Range '{token}' is backwards; start must not exceed end."
                )
            if end > page_count:
                raise ValueError(
                    f"Page {end} is out of range (the PDF has {page_count} pages)."
                )
            for p in range(start, end + 1):
                indices.add(p - 1)
        else:
            if not token.isdigit():
                raise ValueError(f"Invalid page number: '{token}'.")
            page = int(token)
            if page < 1:
                raise ValueError("Page numbers start at 1.")
            if page > page_count:
                raise ValueError(
                    f"Page {page} is out of range (the PDF has {page_count} pages)."
                )
            indices.add(page - 1)

    if not indices:
        raise ValueError("No pages selected.")

    return sorted(indices)


def extract_pages(input_path, output_path, indices):
    """Write a new PDF at ``output_path`` containing only ``indices`` (0-based).

    Pages are emitted in the order given by ``indices``.
    """
    reader = PdfReader(input_path)
    page_count = len(reader.pages)
    if not indices:
        raise ValueError("No pages selected to extract.")
    writer = PdfWriter()
    for idx in indices:
        if idx < 0 or idx >= page_count:
            raise ValueError(
                f"Page index {idx} is out of range (the PDF has {page_count} pages)."
            )
        writer.add_page(reader.pages[idx])
    with open(output_path, "wb") as fh:
        writer.write(fh)
    return output_path


def remove_pages(input_path, output_path, indices):
    """Write a new PDF at ``output_path`` with ``indices`` (0-based) removed.

    Raises ``ValueError`` if the removal would leave no pages.
    """
    reader = PdfReader(input_path)
    page_count = len(reader.pages)
    remove = set(indices)
    for idx in remove:
        if idx < 0 or idx >= page_count:
            raise ValueError(
                f"Page index {idx} is out of range (the PDF has {page_count} pages)."
            )
    keep = [i for i in range(page_count) if i not in remove]
    if not keep:
        raise ValueError("Cannot remove every page — at least one page must remain.")
    writer = PdfWriter()
    for i in keep:
        writer.add_page(reader.pages[i])
    with open(output_path, "wb") as fh:
        writer.write(fh)
    return output_path


def rotate_pages(input_path, output_path, angle, indices=None):
    """Rotate pages by ``angle`` (90/180/270) and write to ``output_path``.

    ``indices`` (0-based) selects which pages to rotate; ``None`` rotates all
    pages. Rotation is clockwise and cumulative with any existing rotation.
    """
    if angle not in (90, 180, 270):
        raise ValueError("Rotation angle must be one of 90, 180 or 270 degrees.")
    reader = PdfReader(input_path)
    page_count = len(reader.pages)
    if indices is None:
        targets = set(range(page_count))
    else:
        targets = set(indices)
        for idx in targets:
            if idx < 0 or idx >= page_count:
                raise ValueError(
                    f"Page index {idx} is out of range (the PDF has {page_count} pages)."
                )
    writer = PdfWriter()
    for i in range(page_count):
        page = reader.pages[i]
        if i in targets:
            page.rotate(angle)
        writer.add_page(page)
    with open(output_path, "wb") as fh:
        writer.write(fh)
    return output_path


def split_pdf(input_path, output_dir, mode, value):
    """Split a PDF into multiple files, returning the list of created paths.

    ``mode``:
      * ``"every_n"`` — ``value`` is a positive int; emit consecutive chunks
        of that many pages (the final chunk may be smaller).
      * ``"ranges"`` — ``value`` is a list of range specs (each 1-based, like
        ``"1-3"``); emit one output per spec.

    Output files are named ``<base>_part1.pdf``, ``<base>_part2.pdf``, ...
    inside ``output_dir``. Returns the created file paths in order.
    """
    reader = PdfReader(input_path)
    page_count = len(reader.pages)
    base = os.path.splitext(os.path.basename(input_path))[0]

    chunks = []  # each entry is a list of 0-based indices -> one output file
    if mode == "every_n":
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError("'Every N pages' needs a whole number.")
        if n < 1:
            raise ValueError("'Every N pages' must be at least 1.")
        for start in range(0, page_count, n):
            chunks.append(list(range(start, min(start + n, page_count))))
    elif mode == "ranges":
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("Provide at least one range to split by.")
        for spec in value:
            chunks.append(parse_page_ranges(spec, page_count))
    else:
        raise ValueError(f"Unknown split mode: '{mode}'.")

    if not chunks:
        raise ValueError("Nothing to split.")

    created = []
    for part_no, idxs in enumerate(chunks, start=1):
        writer = PdfWriter()
        for idx in idxs:
            writer.add_page(reader.pages[idx])
        out_path = os.path.join(output_dir, f"{base}_part{part_no}.pdf")
        with open(out_path, "wb") as fh:
            writer.write(fh)
        created.append(out_path)

    return created
