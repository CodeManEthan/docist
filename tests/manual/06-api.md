---
type: repo-doc
project: docist
description: "Manual hand-test checklist 06 — eight curl-driven checks of the unauthenticated /api/v1 surface: formats discovery, merge with options, convert (including multi-page zip), extract, split, watermark, and the JSON error shape plus an AES-256 verification snippet."
tags: [checklist, testing, api, cli]
updated: 2026-08-02
---

# 06 — REST API (`/api/v1`)

Server: default mode (no auth). Run from the repo root; outputs land in
the current directory. Auth-mode API behavior is in `07-auth-hardening.md`.

```bash
FX=tests/manual/fixtures
BASE=http://localhost:5010
```

## A1 Formats discovery
```bash
curl -s $BASE/api/v1/formats | python -m json.tool | head -30
```
**Expect:** JSON with `merge_extensions` (19 + pdf), `convert_sources`,
and the full `convert_matrix`.
- [ ] Pass

## A2 ⚡ Merge with options
```bash
curl -sf -X POST $BASE/api/v1/merge \
  -F "files[]=@$FX/odd-3p.pdf" -F "files[]=@$FX/sample.md" \
  -F "page_numbers=true" -F "number_position=bottom-center" \
  -o api-merged.pdf && python -c "
from pypdf import PdfReader; r = PdfReader('api-merged.pdf')
print(len(r.pages), 'pages;', len(r.outline), 'bookmarks')"
```
**Expect:** HTTP 200, a PDF of DOC A followed directly by the rendered
markdown (no blank padding — `blank_pages` defaults to false),
2 bookmarks. The file arrives in the response body directly — nothing
appears in `output/`.
- [ ] Pass

## A3 Convert
```bash
curl -sf -X POST $BASE/api/v1/convert \
  -F "file=@$FX/sample.md" -F "target=.pdf" -o api-sample.pdf
```
**Expect:** a valid PDF of the markdown.
- [ ] Pass

## A4 Convert multi-page PDF → PNG returns a zip
```bash
curl -sf -X POST $BASE/api/v1/convert \
  -F "file=@$FX/report-12p.pdf" -F "target=png" \
  -D - -o api-pages.zip | grep -iE 'content-(type|disposition)'
```
**Expect:** `Content-Type: application/zip`, a `.zip` filename in the
disposition; the archive holds 12 PNGs (`unzip -l api-pages.zip`).
- [ ] Pass

## A5 Extract pages
```bash
curl -sf -X POST $BASE/api/v1/pages/extract \
  -F "file=@$FX/report-12p.pdf" -F "ranges=2-4" -o api-extract.pdf
```
**Expect:** 3-page PDF (Pages 2, 3, 4 of 12).
- [ ] Pass

## A6 Split
```bash
curl -sf -X POST $BASE/api/v1/pages/split \
  -F "file=@$FX/report-12p.pdf" -F "mode=every_n" -F "value=5" \
  -o api-split.zip && unzip -l api-split.zip
```
**Expect:** zip with `report-12p_part1.pdf` … `part3.pdf` (5/5/2 pages).
- [ ] Pass

## A7 Watermark
```bash
curl -sf -X POST $BASE/api/v1/watermark \
  -F "file=@$FX/single-1p.pdf" -F "text=DRAFT" -F "opacity=0.4" \
  -o api-wm.pdf
```
**Expect:** the page carries a diagonal DRAFT.
- [ ] Pass

## A8 ⚡ Error shape + AES-256 verification
```bash
# Errors are always JSON {"error": ...} with 400:
curl -s -X POST $BASE/api/v1/convert -F "file=@$FX/sample.md" -w '\n%{http_code}\n'   # missing target
curl -s -X POST $BASE/api/v1/pages/extract \
  -F "file=@$FX/fake.pdf" -F "ranges=1" -w '\n%{http_code}\n'                          # sniffed
# While here, verify S5's encryption level on a UI-protected file:
python - <<'EOF'
from pypdf import PdfReader
r = PdfReader("tests/manual/fixtures/encrypted-aes256.pdf")
print("V =", r._encryption.V, "R =", r._encryption.R, "(want 5 / 6 = AES-256)")
EOF
```
**Expect:** both curls print a JSON `{"error": …}` body and `400`; the
pypdf check prints `V = 5 R = 6`.
- [ ] Pass

Cleanup: `rm -f api-*.pdf api-*.zip`
