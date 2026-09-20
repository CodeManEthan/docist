---
type: repo-doc
project: docist
description: "Manual hand-test checklist 03 — eleven checks on Page Tools: the clickable thumbnail grid and its 24-page cap, extract/remove/rotate/split, compression, OCR of scanned and already-text PDFs, range validation, and corrupt input."
tags: [checklist, testing, ui]
updated: 2026-08-02
---

# 03 — Page Tools (`/pages`)

Server: default mode. All operations are PDF-only.

## P1 ⚡ Thumbnail grid fills the range box
Upload `report-12p.pdf`.
**Expect:** a clickable grid of all 12 pages. Select the Extract
operation and click thumbnails 3, 5, 7 — the ranges box reads `3,5,7`.
- [ ] Pass

## P2 Grid cap at 24 pages
Upload `big-30p.pdf`.
**Expect:** the grid renders only the first 24 thumbnails (cap), without
errors; operations still address all 30 pages.
- [ ] Pass

## P3 ⚡ Extract pages
`report-12p.pdf`, Extract, ranges `1-3,5`.
**Expect:** `report-12p_extracted.pdf` with 4 pages labeled
Page 1, 2, 3, 5 of 12.
- [ ] Pass

## P4 Remove pages + the remove-everything guard
a) `report-12p.pdf`, Remove, ranges `2-11` → `report-12p_trimmed.pdf`
with exactly Page 1 and Page 12.
b) Remove `1-12` → 400: `Cannot remove every page — at least one page
must remain.`
- [ ] Pass

## P5 Rotate a range
`report-12p.pdf`, Rotate 90°, ranges `1-2`.
**Expect:** pages 1–2 turned (the "bottom-left corner" caption moves),
pages 3–12 untouched.
- [ ] Pass

## P6 ⚡ Split
a) `report-12p.pdf`, Split every 5 pages →
`report-12p_split.zip` containing 3 parts of 5, 5 and 2 pages.
b) Split by ranges `1-3;4-12` → zip with a 3-page and a 9-page part.
- [ ] Pass

## P7 ⚡ Compress
a) `photos-heavy.pdf` (~8 MB), defaults (quality 60, max 150 DPI).
**Expect:** noticeably smaller `_compressed.pdf`; the message reports
before → after sizes and % saved; pages still legible.
b) Compress `report-12p.pdf` (tiny, text-only).
**Expect:** message `Already well compressed — kept the original` (or a
sub-percent saving) — output never larger than input.
- [ ] Pass

## P8 ⚡ OCR: make a scan searchable
`scanned-notext.pdf`, OCR, language `eng`.
**Expect:** `scanned-notext_searchable.pdf`; pages look identical, but
you can now select/copy the text, and searching for `SCANMARK 2` in
your PDF viewer finds page 2. (Skip if the OCR radio is greyed out with the
"Requires Tesseract + Ghostscript" hint.)
- [ ] Pass

## P9 OCR a PDF that already has text
`report-12p.pdf`, OCR, Force OFF.
**Expect:** no corruption — pages with text pass through untouched (or a
clear message that a text layer already exists suggesting Force re-OCR).
Note which of the two you get.
- [ ] Pass

## P10 Range validation messages
On Extract with `report-12p.pdf`, try each: `5-2` (backwards), `0`
(pages are 1-based), `99` (out of range), `abc` (not a number).
**Expect:** each rejected with a distinct, specific error — not a
generic failure.
- [ ] Pass

## P11 Corrupt PDF
Upload `corrupt.pdf`, any operation.
**Expect:** 400: `Could not read the PDF file.` (this page trusts the
parser rather than magic-byte sniffing).
- [ ] Pass
