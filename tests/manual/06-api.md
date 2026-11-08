# 06 — REST API (`/api/v1`)

Server: default mode from `README.md` (`DOCIST_LIMIT_ANON=100000
DOCIST_API_ANONYMOUS=1 ./run.sh`). Every `/api/v1` POST needs an API key
unless the server runs with `DOCIST_API_ANONYMOUS=1`; without it each curl
below answers 401 `{"error": "API key required. Create one at /account."}`.
To run this checklist against a server that requires keys, sign in, verify
your email, create a key on `/account`, and add
`-H "Authorization: Bearer $KEY"` to every POST. `GET /api/v1/formats`
needs no key either way. Auth-mode API behavior is in
`07-auth-hardening.md`.

Run from the repo root; outputs land in the current directory. A12 and
the OCR line of A1 need `tesseract` and `gs` on the host. The server
allows 30 POSTs a minute per client (`DOCIST_RATE_LIMIT`); pasting the
blocks back to back can pass that and answer 429, so wait a minute and
go on.

```bash
FX=tests/manual/fixtures
BASE=http://localhost:5010
```

## A1 Formats discovery
```bash
curl -s $BASE/api/v1/formats | python -c "
import json, sys; d = json.load(sys.stdin)
print(sorted(d))
print(len(d['operations']), 'operations')
print(len(d['merge_extensions']), 'merge extensions:', ' '.join(d['merge_extensions']))
print(len(d['convert_sources']), 'convert sources;', len(d['convert_matrix']), 'matrix rows')
print('paper:', d['paper'], '| ocr:', [l['code'] for l in d['ocr_languages']])"
```
**Expect:** keys `convert_matrix`, `convert_sources`, `merge_extensions`,
`ocr_languages`, `operations`, `paper`. 16 operations (every POST
endpoint). 21 merge extensions: `.pdf` plus 20 more, 16 file types once
aliases such as `.jpeg`/`.jpg` and `.htm`/`.html` fold. 24 convert
sources. Paper `['letter', 'a4']`. OCR lists the host's Tesseract
languages, `eng` first (empty without Tesseract).
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
# A request the caller can fix answers JSON {"error": ...} with 400:
curl -s -X POST $BASE/api/v1/convert -F "file=@$FX/sample.md" -w '\n%{http_code}\n'   # missing target
curl -s -X POST $BASE/api/v1/pages/extract \
  -F "file=@$FX/fake.pdf" -F "ranges=1" -w '\n%{http_code}\n'                          # sniffed
# While here, verify S5's encryption level. The Protect page and
# /api/v1/protect share one helper (pdf_ops/security.py), so protect a file
# here and read its encryption dictionary:
curl -sf -X POST $BASE/api/v1/protect \
  -F "file=@$FX/report-12p.pdf" -F "password=docist-test" -o api-aes256.pdf
python - <<'EOF'
from pypdf import PdfReader
r = PdfReader("api-aes256.pdf")
print("V =", r._encryption.V, "R =", r._encryption.R, "(want 5 / 6 = AES-256)")
EOF
```
**Expect:** both error curls print a JSON `{"error": …}` body and `400`;
the pypdf check prints `V = 5 R = 6`. (The other statuses, 401, 413, 429
and 500, are listed under "Errors" on `/api`; 07 covers 401, 413 and 429.)
- [ ] Pass

## A9 Rotate
```bash
curl -sf -X POST $BASE/api/v1/pages/rotate \
  -F "file=@$FX/odd-3p.pdf" -F "angle=90" -F "ranges=2" \
  -o api-rotated.pdf && python -c "
from pypdf import PdfReader
print([p.rotation for p in PdfReader('api-rotated.pdf').pages])"
```
**Expect:** `[0, 90, 0]`: only page 2 turns, a quarter turn clockwise
(its "bottom-left corner" label ends up top-left). With `angle=45` the
answer is 400 `Rotation angle must be one of 90, 180 or 270 degrees.`
- [ ] Pass

## A10 Remove pages
```bash
curl -sf -X POST $BASE/api/v1/pages/remove \
  -F "file=@$FX/report-12p.pdf" -F "ranges=2,4-5" \
  -o api-trimmed.pdf && python -c "
import re; from pypdf import PdfReader; r = PdfReader('api-trimmed.pdf')
print(len(r.pages), [int(re.search(r'Page (\d+) of', p.extract_text())[1]) for p in r.pages])"
```
**Expect:** `9 [1, 3, 6, 7, 8, 9, 10, 11, 12]`. `ranges=1-12` answers
400 (at least one page must remain).
- [ ] Pass

## A11 Compress
```bash
curl -sf -X POST $BASE/api/v1/pages/compress \
  -F "file=@$FX/photos-heavy.pdf" -F "image_quality=50" -F "image_max_dpi=100" \
  -o api-compressed.pdf && wc -c $FX/photos-heavy.pdf api-compressed.pdf
```
**Expect:** `api-compressed.pdf` is much smaller than the input (the
fixture's ~240 DPI photos come down to 100 DPI), still 4 pages and
readable. `image_quality=5` answers 400 `Image quality must be between 10
and 95.`
- [ ] Pass

## A12 OCR (needs Tesseract)
```bash
curl -sf -X POST $BASE/api/v1/pages/ocr \
  -F "file=@$FX/scanned-notext.pdf" -F "language=eng" \
  -o api-searchable.pdf && python -c "
from pypdf import PdfReader
print([('SCANMARK' in (p.extract_text() or '')) for p in PdfReader('api-searchable.pdf').pages])"
```
**Expect:** `[True, True, True]`: each page of the image-only scan now
has a text layer with its `SCANMARK` line, and looks unchanged in a
viewer. A host without Tesseract answers 400 `OCR requires Tesseract and
Ghostscript to be installed on the system.`
- [ ] Pass

## A13 N-up
```bash
curl -sf -X POST $BASE/api/v1/print/nup \
  -F "file=@$FX/report-12p.pdf" -F "n=4" -F "paper=a4" \
  -D - -o api-4up.pdf | grep -i disposition && python -c "
from pypdf import PdfReader
print([(round(float(p.mediabox.width)), round(float(p.mediabox.height)))
       for p in PdfReader('api-4up.pdf').pages])"
```
**Expect:** filename `report-12p_4up.pdf`; 3 A4 portrait sheets
(`(595, 842)` each), Pages 1–4, 5–8 and 9–12 four to a sheet in reading
order. `n=3` answers 400.
- [ ] Pass

## A14 Booklet
```bash
curl -sf -X POST $BASE/api/v1/print/booklet \
  -F "file=@$FX/odd-3p.pdf" -o api-booklet.pdf && python -c "
from pypdf import PdfReader
print([(round(float(p.mediabox.width)), round(float(p.mediabox.height)))
       for p in PdfReader('api-booklet.pdf').pages])"
```
**Expect:** 2 landscape letter sides (`(792, 612)` each): the 3 pages
are padded to 4, so side one holds a blank and Page 1, side two Pages 2
and 3. Printed duplex, flipped on the short edge and folded, it reads
1, 2, 3.
- [ ] Pass

## A15 Export pages as images
```bash
curl -sf -X POST $BASE/api/v1/export \
  -F "file=@$FX/odd-3p.pdf" -F "operation=images" -F "fmt=jpg" -F "dpi=72" \
  -D - -o api-images.zip | grep -iE 'content-(type|disposition)' && unzip -l api-images.zip
```
**Expect:** `Content-Type: application/zip`, filename `odd-3p_images.zip`,
holding `page_001.jpg`, `page_002.jpg`, `page_003.jpg`. No `operation`
answers 400 `Choose an operation: 'images' or 'text'.`
- [ ] Pass

## A16 ⚡ Export text
```bash
curl -sf -X POST $BASE/api/v1/export \
  -F "file=@$FX/odd-3p.pdf" -F "operation=text" \
  -D - -o api-text.txt | grep -iE 'content-(type|disposition)' && cat api-text.txt
```
**Expect:** `Content-Type: text/plain; charset=utf-8`, filename
`odd-3p.txt`; the text has a `--- Page 1 ---` … `--- Page 3 ---` line
before each page's text, which includes "DOC A (odd) body text on page N."
- [ ] Pass

## A17 Header and footer
```bash
curl -sf -X POST $BASE/api/v1/stamp/header-footer \
  -F "file=@$FX/odd-3p.pdf" -F "header_left=CONFIDENTIAL" \
  -F "footer_center=Sheet {page}/{pages}" -o api-stamped.pdf && python -c "
from pypdf import PdfReader
for p in PdfReader('api-stamped.pdf').pages:
    t = p.extract_text(); print('CONFIDENTIAL' in t, [w for w in t.split() if '/' in w])"
```
**Expect:** each page prints `True` and its `Sheet N/3`: `1/3`, `2/3`,
`3/3`. In a viewer: small grey text top-left and bottom-centre. With
every slot blank the answer is 400.
- [ ] Pass

## A18 Bates numbers
```bash
curl -sf -X POST $BASE/api/v1/stamp/bates \
  -F "file=@$FX/odd-3p.pdf" -F "prefix=ACME-" -F "start=100" \
  -o api-bates.pdf && python -c "
from pypdf import PdfReader
print([w for p in PdfReader('api-bates.pdf').pages for w in p.extract_text().split() if w.startswith('ACME')])"
```
**Expect:** `['ACME-000100', 'ACME-000101', 'ACME-000102']`, bottom-right
in black on each page (6 digits by default).
- [ ] Pass

## A19 Protect
```bash
curl -sf -X POST $BASE/api/v1/protect \
  -F "file=@$FX/odd-3p.pdf" -F "password=docist-test" \
  -D - -o api-protected.pdf | grep -i disposition && python -c "
from pypdf import PdfReader; r = PdfReader('api-protected.pdf')
print('encrypted:', r.is_encrypted, '| decrypt:', r.decrypt('docist-test').name, '|', len(r.pages), 'pages')"
curl -s -X POST $BASE/api/v1/protect -F "file=@$FX/odd-3p.pdf" -w '\n%{http_code}\n'   # no password
```
**Expect:** filename `odd-3p-protected.pdf`; `encrypted: True | decrypt:
OWNER_PASSWORD | 3 pages`; a viewer asks for the password. The second
curl prints `{"error": "A password is required (send a 'password' field)."}`
and `400`. The password appears nowhere in the server log.
- [ ] Pass

## A20 ⚡ Unlock (round trip with A19)
```bash
curl -s -X POST $BASE/api/v1/unlock \
  -F "file=@api-protected.pdf" -F "password=wrong" -w '\n%{http_code}\n'
curl -sf -X POST $BASE/api/v1/unlock \
  -F "file=@api-protected.pdf" -F "password=docist-test" \
  -D - -o api-unlocked.pdf | grep -i disposition && python -c "
from pypdf import PdfReader; r = PdfReader('api-unlocked.pdf')
print('encrypted:', r.is_encrypted, '|', len(r.pages), 'pages')"
```
**Expect:** the wrong password prints `{"error": "Incorrect password"}`
and `400`; the right one gives `api-protected-unlocked.pdf` with
`encrypted: False | 3 pages`, the DOC A pages, and no password prompt in
a viewer. Without A19's file, `$FX/encrypted-aes256.pdf` (same password)
works the same way.
- [ ] Pass

Cleanup: `rm -f api-*.pdf api-*.zip api-*.txt`
