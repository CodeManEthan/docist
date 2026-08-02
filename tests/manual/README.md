# Docist manual hand-tests

Checklists for hand-testing every user-facing feature. Automated tests
(559 pytest + 208 node) cover the logic; these cover what only a human
can judge — the browser UX, real downloads opened in real viewers, drag
and drop, and the error messages users actually see.

## One-time setup per session

```bash
source .venv/bin/activate
python tests/manual/make_fixtures.py     # writes tests/manual/fixtures/ (~20 MB, gitignored)
```

OCR checks (03/P8–P9, 04/E4–E5, 02/C10) need `tesseract` and `gs` on the
host — the UI greys those controls out if they're missing, and you can
skip those items.

**Password for both encrypted fixture PDFs:** `docist-test`

## Server modes

| Mode | Command | Used by |
|---|---|---|
| Default | `./run.sh` → http://localhost:5010 | checklists 01–06 |
| Hardened | `DOCIST_PASSWORD=testgate DOCIST_MAX_UPLOAD_MB=5 DOCIST_RATE_LIMIT=5 DOCIST_RATE_WINDOW=60 ./run.sh` | checklist 07 |

The hardened overrides exist to make failures reachable by hand: a 5 MB
cap lets `padding-6mb.pdf` trigger the oversized-upload path, and 5
POSTs/minute makes the rate limiter fire without 30 rapid submits.

## Checklists (suggested order)

1. `01-merge.md` — Merge & Convert page (12 checks)
2. `02-convert.md` — Convert Files page (11 checks)
3. `03-pages.md` — Page Tools (11 checks)
4. `04-print-export.md` — Print Prep + Export (9 checks)
5. `05-security.md` — Watermark & Security (10 checks)
6. `06-api.md` — REST API via curl (8 checks)
7. `07-auth-hardening.md` — login gate, limits, spoofing (10 checks)

Roughly 2–2.5 hours for a full pass.

**Smoke subset (~30 min):** items marked ⚡ — one representative check
per feature. Run it after small changes; run everything before a release.

## Tips

- Results land in `output/` and are pruned after 24 h; re-running the
  same operation on the same file yields a `_1`-suffixed second result.
- Where a checklist quotes an error message, expect that exact text —
  they're lifted from the source.
- Check items off with `[x]` in your editor as you go; `git checkout`
  the files afterwards (or commit a dated copy if you want a record).
