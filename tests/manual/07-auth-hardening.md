---
type: repo-doc
project: docist
description: "Manual hand-test checklist 07 — ten checks against a server started in hardened mode: login gate and lifecycle, Bearer-token API auth, the known 413 UX wart, rate limiting including on /login, download traversal guard, and content sniffing under auth."
tags: [checklist, testing, auth, secrets]
updated: 2026-08-02
---

# 07 — Auth gate & hardening

**Restart the server in hardened mode first:**

```bash
DOCIST_PASSWORD=testgate DOCIST_MAX_UPLOAD_MB=5 \
DOCIST_RATE_LIMIT=5 DOCIST_RATE_WINDOW=60 ./run.sh
```

⚠️ The 5-POSTs-per-minute limit applies to *everything*, including
`/login`. Do H1–H6 at an unhurried pace, and save H7/H10 (which
deliberately exhaust the limit) for last — or wait 60 s after them.

```bash
FX=tests/manual/fixtures
BASE=http://localhost:5010
```

## H1 ⚡ Login gate
Open http://localhost:5010/ in a fresh browser window (or private tab).
**Expect:** redirect to `/login` with a password form; every tool page
does the same. `/healthz` alone answers without auth:
`curl -s $BASE/healthz` → `{"status":"ok"}`.
- [ ] Pass

## H2 Wrong password
Submit `nope` on the login form.
**Expect:** form re-renders with `Wrong password.` (HTTP 401 in
devtools' network tab).
- [ ] Pass

## H3 Login / logout lifecycle
Log in with `testgate`.
**Expect:** redirected to the merge page; a "Sign out" link now shows in
the nav. Merge something small to confirm tools work while authed. Sign
out → back to `/login`; revisiting `/` redirects to login again.
- [ ] Pass

## H4 ⚡ API auth: 401 vs Bearer
```bash
curl -s -X POST $BASE/api/v1/watermark \
  -F "file=@$FX/single-1p.pdf" -F "text=X" -w '\n%{http_code}\n'
curl -sf -X POST $BASE/api/v1/watermark \
  -H "Authorization: Bearer testgate" \
  -F "file=@$FX/single-1p.pdf" -F "text=X" -o /dev/null -w '%{http_code}\n'
```
**Expect:** first: JSON `{"error": "Authentication required. Reload and
sign in."}` + 401. Second: 200.
- [ ] Pass

## H5 Wrong Bearer token
Repeat the second curl with `Bearer wrongtoken`.
**Expect:** 401, same JSON error — not a 500, no stack trace.
- [ ] Pass

## H6 ⚡ Oversized upload (413) — known UX wart
a) Logged in, upload `padding-6mb.pdf` (~6 MB > the 5 MB cap) on the
merge page.
**Expect (current behavior):** the UI shows a misleading generic
failure like "Failed to connect to server" — the server actually sent
Werkzeug's HTML 413 page, which the frontend can't parse as JSON.
b) Confirm the real status:
```bash
curl -s -X POST $BASE/upload -H "Authorization: Bearer testgate" \
  -F "files[]=@$FX/padding-6mb.pdf" -o /dev/null -w '%{http_code}\n'   # → 413
```
This is a real finding to consider fixing: a JSON 413 handler + a
friendly "file too large" message.
- [ ] Pass

## H7 Rate limiting (429) — run near the end
```bash
for i in $(seq 1 7); do
  curl -s -X POST $BASE/api/v1/watermark -H "Authorization: Bearer testgate" \
    -F "file=@$FX/single-1p.pdf" -F "text=RL$i" \
    -o /dev/null -w "$i: %{http_code}\n"
done
```
**Expect:** first requests 200, then 429s with JSON `Too many requests —
please wait a moment and try again.` and a `Retry-After` header
(add `-D -` to see it). While limited, submitting in the browser also
fails; after ~60 s everything recovers.
- [ ] Pass

## H8 ⚡ Download traversal guard
```bash
curl -s -H "Authorization: Bearer testgate" \
  "$BASE/download?filename=../app.py" -w '\n%{http_code}\n'
curl -s -H "Authorization: Bearer testgate" \
  "$BASE/download?filename=nonexistent.pdf" -w '\n%{http_code}\n'
curl -s -H "Authorization: Bearer testgate" \
  "$BASE/download?filename=00000000000000000000000000000000_nonexistent.pdf" -w '\n%{http_code}\n'
```
**Expect:** `Invalid filename` + 400 for the traversal and for the bare
name (no random key, so it is refused even if such a file exists);
`No file available` + 404 for the keyed missing name. No file contents
leak.
- [ ] Pass

## H9 Content sniffing under auth (API)
```bash
curl -s -X POST $BASE/api/v1/convert -H "Authorization: Bearer testgate" \
  -F "file=@$FX/fake.pdf" -F "target=png" -w '\n%{http_code}\n'
```
**Expect:** 400 JSON: the "does not look like a .pdf" message.
- [ ] Pass

## H10 Login brute-force is rate-limited too
Wait 60 s after H7, then submit 6 wrong passwords on `/login` quickly.
**Expect:** after the 5th POST the form/requests start answering 429 —
the gate can't be brute-forced faster than the rate limit.
- [ ] Pass

**When done:** restart the server in default mode if you're continuing
with other checklists.
