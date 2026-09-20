---
type: repo-doc
project: docist
description: "Manual hand-test checklist 02 — eleven checks on the Convert Files page, one per transform family: target matrix, document/data/image conversions, multi-page PDF-to-zip, pivot routing, OCR, and extension sniffing."
tags: [checklist, testing, ui]
updated: 2026-08-02
---

# 02 — Convert Files (`/convert`)

Server: default mode. One representative check per transform family —
the pytest suite already covers every individual format pair.

## C1 Target dropdown and matrix
Choose `sample.csv`.
**Expect:** the "Convert to" dropdown fills with CSV's real targets
(xlsx, json, html, pdf, …). Expand "What can I convert?" — the full
matrix renders. Swap the file for `photo.png` and the dropdown changes
to image targets.
- [ ] Pass

## C2 ⚡ Markdown → HTML (documents family)
`sample.md` → `.html`. Open the download in a browser.
**Expect:** heading, bold/italic, the two-row table, and the fenced code
block all render.
- [ ] Pass

## C3 DOCX → text
`sample.docx` → `.txt`.
**Expect:** all three paragraphs present, including `DOCX-MARKER`.
- [ ] Pass

## C4 CSV → XLSX (data family)
`sample.csv` → `.xlsx`. Reconvert the downloaded file → `.json`.
**Expect:** round-trip preserves the 3 people rows; JSON is an array of
objects keyed `name`/`role`/`score`.
- [ ] Pass

## C5 JSON → YAML
`sample.json` → `.yaml`.
**Expect:** readable YAML with the `features` list and nested `limits`.
- [ ] Pass

## C6 ⚡ HEIC → JPG (image codec family)
`photo.heic` → `.jpg`.
**Expect:** orange "HEIC PHOTO" image opens fine as JPEG.
- [ ] Pass

## C7 Animated GIF → PNG takes frame 1 only
`animated.gif` → `.png`.
**Expect:** a single still image of FRAME 1 OF 3 (teal) — later frames
are intentionally dropped for image→image conversions.
- [ ] Pass

## C8 ⚡ PDF → PNG: single page vs multi-page zip
a) `single-1p.pdf` → `.png` → one plain PNG image.
b) `report-12p.pdf` → `.png` → a **`.zip`** of 12 PNGs, and the success
message says a zip is coming.
- [ ] Pass

## C9 Pivot routing: DOCX → PNG
`sample.docx` → `.png` (no direct transform exists; it chains
docx → pdf → png automatically).
**Expect:** a PNG of the rendered document page.
- [ ] Pass

## C10 ⚡ Image → text (OCR)
`ocr-sign.png` → `.txt`.
**Expect:** text contains "quick brown fox" and the reference code
`XJ4872` (minor OCR typos elsewhere are acceptable).
- [ ] Pass

## C11 Spoofed extension is sniffed
`fake.pdf` → any target.
**Expect:** 400: `The file does not look like a .pdf file (its content
doesn't match the extension).`
- [ ] Pass
