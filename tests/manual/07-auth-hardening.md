---
type: repo-doc
project: docist
description: "Manual hand-test checklist 07 — sixteen checks of accounts and hardening against a server started in accounts mode: anonymous daily limit and nav usage line, signup, email verification via the console sender, API key lifecycle, owner-bound downloads, CSRF, logout, password change and reset, set-plan, throttles, 413, rate limiting, traversal guard and content sniffing."
tags: [checklist, testing, auth, accounts, secrets]
updated: 2026-09-25
---

# 07 — Accounts & hardening

**Restart the server in accounts mode first**, with tiny limits so every
wall is reachable by hand:

```bash
DOCIST_LIMIT_ANON=3 DOCIST_LIMIT_FREE=20 DOCIST_MAX_UPLOAD_MB=5 \
DOCIST_RATE_LIMIT=10 DOCIST_RATE_WINDOW=60 ./run.sh
```

Emails use the default `console` sender: verification and reset links are
printed in the terminal running the server. Accounts are stored in
`instance/docist.db`; delete that file to start from zero (the server must be
stopped first).

⚠️ The 10-POSTs-per-minute limit applies to every POST, including sign-in
and sign-up. Work at an unhurried pace and save A15/A16 (which deliberately
exhaust limits) for last.

```bash
FX=tests/manual/fixtures
BASE=http://localhost:5010
EMAIL=you@example.com            # any address; nothing is really sent
```

## A1 ⚡ Anonymous use and the nav usage line
Open http://localhost:5010/ in a fresh private window.
**Expect:** the merge page loads with no login. The right of the nav shows
`3 of 3 left today` and a **Sign in** link; there is no Sign out button.
`curl -s $BASE/healthz` → `{"status":"ok"}`.
- [ ] Pass

## A2 ⚡ Anonymous daily limit
In the same window, run three small operations (e.g. Export → text on
`single-1p.pdf`), reloading the page between them.
**Expect:** the nav counts down `2 of 3`, `1 of 3`, `0 of 3 left today`. The
fourth run fails with "You've used today's 3 free operations. Create a free
account and verify your email for 20 a day at /signup — or come back after
midnight UTC." In devtools the response is **429** JSON with
`"code": "daily_limit"` and a `Retry-After` header. A failed operation (e.g.
extracting page 99 of a 1-page file) does not use up a count.
- [ ] Pass

## A3 Sign up
From **Sign in**, follow the link to sign up. Create an account with
`$EMAIL` and a password of at least 8 characters.
**Expect:** you land on `/account`, signed in, with a note that email isn't
set up on this instance, so the account runs on guest limits. The account
page shows the email as unverified with a resend button, plan `free`, and
today's usage. The nav now shows **Account** and a **Sign out** button, and
the usage line still reads `0 of 3 left today`: an unverified account shares
the anonymous allowance of its IP. Signing up again with the same email
says an account already exists.
- [ ] Pass

## A4 ⚡ Verify the email
Find the verification email in the server terminal (logged at WARNING) and
open its link.
**Expect:** a page saying the email is verified. Opening the same link
again says it has expired or was already used. Back on `/account`: verified,
usage `0 / 20`; the nav shows `20 of 20 left today`.
Alternative when no terminal is handy:
`flask --app app verify-user $EMAIL`.
- [ ] Pass

## A5 ⚡ API keys: create, use, reject
On `/account`, create a key labelled `manual`.
**Expect:** the raw key (starting `dk_`) is shown once, in a highlighted box
with a copy-it-now note. Reload: the key is gone, and the table lists only
its label and prefix. Then:
```bash
KEY=dk_...                                   # paste it
curl -s -X POST $BASE/api/v1/watermark \
  -F "file=@$FX/single-1p.pdf" -F "text=X" -w '\n%{http_code}\n'
curl -s -D - -X POST $BASE/api/v1/watermark -H "Authorization: Bearer $KEY" \
  -F "file=@$FX/single-1p.pdf" -F "text=X" -o /dev/null
curl -s -X POST $BASE/api/v1/watermark -H "Authorization: Bearer dk_wrong" \
  -F "file=@$FX/single-1p.pdf" -F "text=X" -w '\n%{http_code}\n'
curl -s $BASE/api/v1/formats -o /dev/null -w '%{http_code}\n'
```
**Expect:** no key → 401 `{"error": "API key required. Create one at
/account."}`. Right key → 200 with `X-Docist-Usage-Limit: 20` and
`X-Docist-Usage-Remaining` counting down. Wrong key → 401 `{"error":
"Invalid or revoked API key."}`. `/api/v1/formats` → 200 with no key.
Revoke the key on `/account`, repeat the right-key call → 401.
Create a second key for the later checks.
- [ ] Pass

## A6 Content sniffing (API)
```bash
curl -s -X POST $BASE/api/v1/convert -H "Authorization: Bearer $KEY" \
  -F "file=@$FX/fake.pdf" -F "target=png" -w '\n%{http_code}\n'
```
**Expect:** 400 JSON with the "does not look like a .pdf" message. The usage
remaining count does not drop.
- [ ] Pass

## A7 ⚡ Downloads are bound to their owner
Signed in, run Export → text and let it download. In devtools, copy the
`/download?filename=…` URL. Open that URL in a different private window
(signed out), and in a second browser signed in as the same account.
**Expect:** the other anonymous window gets 404 `{"error": "No file
available"}`. The same account in another browser downloads the file.
Repeat from an anonymous window: its result is refused in every other window.
- [ ] Pass

## A8 CSRF
```bash
curl -s -X POST $BASE/pages/run -F "operation=extract" -F "ranges=1" \
  -F "file=@$FX/single-1p.pdf" -w '\n%{http_code}\n'
```
**Expect:** 400 JSON `{"error": "Session expired — reload the page and try
again."}`. The same operation from the browser works: the page's script adds
the token. `/api/v1` is exempt (A5 worked with only a key).
- [ ] Pass

## A9 ⚡ Download traversal guard
```bash
curl -s "$BASE/download?filename=../app.py" -w '\n%{http_code}\n'
curl -s "$BASE/download?filename=nonexistent.pdf" -w '\n%{http_code}\n'
curl -s "$BASE/download?filename=$(printf '0%.0s' {1..48})_nonexistent.pdf" -w '\n%{http_code}\n'
```
**Expect:** `Invalid filename` + 400 for the traversal and for the bare
name (no key, so it is refused even if such a file exists); `No file
available` + 404 for the keyed name. No file contents leak.
- [ ] Pass

## A10 Sign in and out
Sign out with the nav button, then try `curl -s -o /dev/null -w
'%{http_code}\n' $BASE/logout`.
**Expect:** the button returns you to `/` signed out; a GET to `/logout` is
405. On `/login`, a wrong password and an unknown email both give "Wrong
email or password." The right password signs you in. Opening
`$BASE/login?next=//evil.example` and signing in lands on Docist, not
evil.example.
- [ ] Pass

## A11 Changing the password ends other sessions
Sign in to the same account in two browsers. In browser 1, change the
password on `/account`.
**Expect:** browser 1 stays signed in; reloading browser 2 shows it signed
out. "Sign out everywhere" on `/account` has the same effect. API keys keep
working after a voluntary password change.
- [ ] Pass

## A12 Forgot password and reset
Sign out. On `/forgot`, submit `$EMAIL`, then an address with no account.
**Expect:** both answers are identical ("If an account exists for that
address, we've sent a reset link."); only the real address produces an
email in the terminal. Open its link and set a new password.
**Expect:** you land on `/account` signed in, with a note that existing API
keys were revoked; the key table shows them revoked and `$KEY` now gets 401.
The old password no longer works; the reset link cannot be used twice.
- [ ] Pass

## A13 Plans
```bash
flask --app app set-plan $EMAIL monthly
```
**Expect:** after a reload the nav has no usage line (unlimited) and
`/account` shows plan `monthly`. `flask --app app set-plan $EMAIL free`
puts it back.
- [ ] Pass

## A14 ⚡ Oversized upload (413)
```bash
curl -s -X POST $BASE/api/v1/watermark -H "Authorization: Bearer $KEY" \
  -F "file=@$FX/padding-6mb.pdf" -F "text=X" -o /dev/null -w '%{http_code}\n'
```
(`$KEY` must be a live key: create one after A12.)
**Expect:** 413. In the browser, uploading `padding-6mb.pdf` on the merge
page still shows a generic failure: the server sends Werkzeug's HTML 413
page, which the frontend cannot parse. Known UX wart; a JSON 413 handler
would fix it.
- [ ] Pass

## A15 Sign-in throttle — run near the end
Submit 6 wrong passwords for `$EMAIL` on `/login` within a minute or two.
**Expect:** from the 6th attempt the form answers 429 with "Too many
attempts. Try again in a few minutes.", even with the right password.
Wait 15 minutes (or restart the server) to clear it.
- [ ] Pass

## A16 Rate limiting (429) — run last
```bash
for i in $(seq 1 12); do
  curl -s -X POST $BASE/api/v1/watermark \
    -F "file=@$FX/single-1p.pdf" -F "text=RL$i" \
    -o /dev/null -w "$i: %{http_code}\n"
done
```
**Expect:** 401s (no key) until the per-minute cap, then 429s with JSON
`Too many requests — please wait a moment and try again.` and a
`Retry-After` header (add `-D -` to see it). Refused requests count against
the cap too. After ~60 s everything recovers.
- [ ] Pass

**When done:** restart the server in default mode if you're continuing
with other checklists.
