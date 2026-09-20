---
type: repo-doc
project: docist
description: "Manual hand-test checklist 04 — nine checks across Print Prep (2-up, 4-up, booklet imposition, non-PDF rejection) and Export (PDF to zipped images, DPI bounds, PDF to text with and without OCR fallback)."
tags: [checklist, testing, ui]
updated: 2026-08-02
---

# 04 — Print Prep (`/print`) + Export (`/export`)

Server: default mode.

## Print Prep

### PR1 ⚡ 2-up
`report-12p.pdf`, N-up, 2 per sheet.
**Expect:** `report-12p_2up.pdf` — 6 landscape letter sheets, two
aspect-preserved pages side by side, in order (1+2, 3+4, …).
- [ ] Pass

### PR2 4-up
`report-12p.pdf`, N-up, 4 per sheet.
**Expect:** 3 portrait sheets, 2×2 grid, reading order 1-2/3-4.
- [ ] Pass

### PR3 Booklet imposition
`odd-3p.pdf` (3 pages, padded to 4), Booklet.
**Expect:** `odd-3p_booklet.pdf` with 2 sheets: sheet 1 = [blank | Page 1],
sheet 2 = [Page 2 | Page 3]. Success message tells you to print duplex,
flip on short edge. Optional print test: fold it — pages read 1, 2, 3.
- [ ] Pass

### PR4 Non-PDF rejected
Upload `photo.png` here.
**Expect:** blocked — the JS says `Please choose a .pdf file.` (or the
server answers `Only .pdf files are supported`).
- [ ] Pass

## Export

### E1 ⚡ PDF → images (zip)
`report-12p.pdf`, Images, PNG, 150 DPI.
**Expect:** `report-12p_images.zip` with `page_001.png` … `page_012.png`,
each a clean render.
- [ ] Pass

### E2 DPI bounds and effect
a) `single-1p.pdf`, JPG at 600 DPI → one page, visibly larger/sharper
file than the same run at 150 DPI.
b) Try typing 20 or 700 into the DPI field → the input clamps/blocks it
(min 30, max 600).
- [ ] Pass

### E3 ⚡ PDF → text
`report-12p.pdf`, Text.
**Expect:** `report-12p.txt` in UTF-8 with `--- Page N ---` separators
and each page's body line ("REPORT body text on page N.").
- [ ] Pass

### E4 Scanned PDF → text without OCR fallback
`scanned-notext.pdf`, Text, OCR fallback OFF.
**Expect:** the page separators are there but the text is empty — the
pages are pictures.
- [ ] Pass

### E5 ⚡ Scanned PDF → text with OCR fallback
Same file, OCR fallback ON, language `eng`.
**Expect:** `SCANMARK 1` through `SCANMARK 3` all appear. (Skip if the OCR
checkbox is disabled.)
- [ ] Pass
