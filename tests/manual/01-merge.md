# 01 — Merge & Convert (`/`)

Server: default mode. Fixtures: `tests/manual/fixtures/`.

## M1 ⚡ Basic merge, default options
Upload `odd-3p.pdf` + `even-4p.pdf` (in that order) → Merge → Download.
**Expect:** `odd-3p-merged.pdf`, 8 pages: DOC A pages 1–3, one blank page
(front-page alignment after the odd doc), then DOC B pages 1–4. Page
numbers stamped bottom-right starting at 1. PDF outline has two
bookmarks, one per source file.
- [ ] Pass

## M2 ⚡ Row thumbnails
After adding the two PDFs in M1, each row shows a small render of that
file's first page ("Page 1 of 3" / "Page 1 of 4"), plus name, size, ✕
and ▲/▼ controls.
- [ ] Pass

## M3 Reorder and remove
Add `odd-3p.pdf`, `even-4p.pdf`, `single-1p.pdf`. Move `single-1p.pdf`
to the top with ▲, then drag `even-4p.pdf` above `odd-3p.pdf` with the
mouse, then remove `odd-3p.pdf` with ✕. Merge.
**Expect:** result is SINGLE PAGE then DOC B 1–4 — order matches the
final on-screen list; drag-and-drop and buttons agree.
- [ ] Pass

## M4 ⚡ Mixed-format merge
Upload `sample.md` + `sample.docx` + `photo.png` + `sample.csv` +
`odd-3p.pdf` → Merge.
**Expect:** one PDF; markdown rendered with heading/table/code block,
DOCX text present, PNG centered on a letter page, CSV as a table, then
DOC A's 3 pages. One bookmark per source (5 total).
- [ ] Pass

## M5 Multi-frame image → multiple pages
Upload `multi.tiff` alone → Merge.
**Expect:** 3-page PDF, pages reading FRAME 1 OF 3 / 2 / 3.
- [ ] Pass

## M6 Merge options
Upload `odd-3p.pdf` + `even-4p.pdf`. Set: number position
bottom-center, start number 5, blank pages OFF, bookmarks OFF → Merge.
**Expect:** 7 pages (no blank inserted), numbers 5–11 centered at the
bottom, no outline entries.
- [ ] Pass

## M7 ⚡ Interleave two scanned stacks
Select Interleave mode. Upload `fronts.pdf` + `backs-reversed.pdf`
(fronts first), "second stack is reversed" ON → Merge.
**Expect:** 6 pages reading Sheet 1, 2, 3, 4, 5, 6 in order.
- [ ] Pass

## M8 Interleave input validation
a) Interleave with three files → 400-style error naming the actual file
count (needs exactly 2).
b) Interleave `fronts.pdf` (3 p) + `report-12p.pdf` (12 p) → error
naming both page counts (they may differ by at most 1).
- [ ] Pass

## M9 Unsupported extension is dropped silently
Upload only `unsupported.xyz` (drag it onto the drop zone if the file
picker filters it) → Merge.
**Expect:** error `No supported files provided`. Then upload it together
with `single-1p.pdf`: merge succeeds and the result contains only the
PDF — the `.xyz` was silently skipped. (Judgement call: is silent
skipping acceptable UX? Note what you think.)
- [ ] Pass

## M10 Corrupt PDF
Add `corrupt.pdf` to the list.
**Expect:** its row appears but the thumbnail silently degrades to a
plain row (no crash). Then Merge → an error message is shown, not a
hang or empty download. Note the exact wording.
- [ ] Pass

## M11 Spoofed extension is sniffed
Upload `fake.pdf` (really a PNG) → Merge.
**Expect:** 400 error: `The file does not look like a .pdf file (its
content doesn't match the extension).`
- [ ] Pass

## M12 Duplicate output naming
Run M1 twice without deleting anything.
**Expect:** second run's download is `odd-3p-merged_1.pdf`; both
downloads work.
- [ ] Pass
