---
type: repo-doc
project: docist
description: "Production deployment recipe for Docist: the one-command gunicorn start, every DOCIST_* environment variable, accounts and daily limits, email sender setup, Railway with Postgres, nginx and systemd templates, and honest limitations."
tags: [howto, deployment, ops, auth, accounts]
updated: 2026-09-25
---

# Deploying Docist

Docist's dev server (`python app.py`) is for localhost only. For anything
reachable by other people, run gunicorn behind a reverse proxy, set a secret
key, and use a real database. This document is the complete recipe.

## The short version

```bash
DOCIST_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
DOCIST_COOKIE_SECURE=1 \
DOCIST_TRUSTED_PROXIES=1 \
./run.sh prod
```

That starts gunicorn on `127.0.0.1:5010` with `--preload`, debug off, rate
limiting and daily limits active, and accounts stored in SQLite under
`instance/`. Put nginx (or Caddy) in front for TLS. For more than one host or
a disposable disk, point `DATABASE_URL` at Postgres.

## How access works

There is no shared password. Every tool page works without an account:

- **Anonymous visitors** get `DOCIST_LIMIT_ANON` operations a day (default
  10), counted per client IP. The IP is stored only as a keyed hash.
- **Accounts** sign up with email and password at `/signup`. An account
  counts as a guest (same IP allowance, same count) until its email is
  verified. Verified free accounts get `DOCIST_LIMIT_FREE` a day (default 50).
  Any plan other than `free` gets `DOCIST_LIMIT_PAID` (default 0, unlimited).
- **One operation** is one successful tool POST or `/api/v1` call. Failed
  requests and page thumbnails cost nothing. The day is the UTC date. At the
  limit the request gets a JSON 429 with a `Retry-After` until midnight UTC.
- **The REST API** needs a per-user key, created on `/account` by a verified
  user. Keys are stored hashed and shown once. Set `DOCIST_API_ANONYMOUS=1` to
  let keyless calls through, metered as anonymous.
- **Results** are bound to whoever made them: `/download` serves a result only
  to the browser session or signed-in user that produced it.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///docist.db` (in `instance/`) | Where accounts, keys and usage live. On Railway paste the Postgres add-on's `postgres://…` URL; `postgres://` and `postgresql://` are rewritten to the psycopg 3 driver. |
| `DOCIST_SECRET_KEY` | persisted in `instance/secret_key` | Signs sessions, and keys the anonymous IP hashes and result owner tags. **Required in production.** Without it the key is generated once into `instance/secret_key`, which survives restarts on a persistent disk but not a fresh container. |
| `DOCIST_COOKIE_SECURE` | off | Set to `1` whenever you serve over HTTPS, so session cookies are `Secure`. **Required in production**: the cookie now carries real logins. |
| `DOCIST_TRUSTED_PROXIES` | `1` if `RAILWAY_ENVIRONMENT` is set, else `0` | Reverse-proxy hops to trust for `X-Forwarded-For` / `X-Forwarded-Proto`. `1` behind one proxy (nginx, Railway). Leave `0` when nothing is in front. |
| `DOCIST_LIMIT_ANON` | `10` | Daily operations for anonymous visitors and unverified accounts. |
| `DOCIST_LIMIT_FREE` | `50` | Daily operations for verified free accounts. |
| `DOCIST_LIMIT_PAID` | `0` | Daily operations for any plan other than `free`; `0` means unlimited. |
| `DOCIST_API_ANONYMOUS` | `0` | `1` lets `/api/v1` POSTs run without a key (metered as anonymous). |
| `DOCIST_BASE_URL` | *(unset)* | Public origin for links in emails, e.g. `https://docist.example`. Required by the `smtp` and `resend` backends. |
| `DOCIST_EMAIL_BACKEND` | `console` | `console` (log the email), `smtp` or `resend`. |
| `DOCIST_EMAIL_FROM` | `Docist <no-reply@localhost>` | Sender address. |
| `DOCIST_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` | — / `587` / — / — | SMTP backend. |
| `DOCIST_SMTP_TLS` | `starttls` | `starttls`, `ssl` or `none`. |
| `DOCIST_RESEND_API_KEY` | — | Resend backend. |
| `DOCIST_HOST` | `127.0.0.1` | Dev-server bind address (`app.py` only; gunicorn takes `--bind`). |
| `DOCIST_PORT` | `5010` | Port for both `run.sh` modes. |
| `DOCIST_MAX_UPLOAD_MB` | `50` | Request-body cap in MiB for anonymous visitors and free accounts; oversized uploads get a JSON 413 (`code: upload_too_large`). |
| `DOCIST_MAX_UPLOAD_MB_PAID` | `90` | Request-body cap in MiB for paid accounts. Keep it at least 5 MB under any proxy's own body cap: Cloudflare passes at most 100 MB per request on its Free and Pro plans, and a body over that gets Cloudflare's HTML page, not Docist's. |
| `DOCIST_RENDER_BUDGET` | `90` | Seconds from the start of a request by which all Word-engine (LibreOffice) work must end, upload time included. Keep it 30 s inside gunicorn's `--timeout`. |
| `DOCIST_OFFICE_TIMEOUT` | `60` | Seconds one LibreOffice conversion may run. 15 s of the render budget are kept for the reflow fallback, and a call with under 10 s left isn't started. |
| `DOCIST_OFFICE_MEM_MB` | `1536` | Address-space cap (`RLIMIT_AS`) for each LibreOffice process, in MiB. A conversion over it fails and the file is re-flowed instead. |
| `DOCIST_OCR_MAX_LANGS` | `2` | How many OCR languages one request may name (`eng+spa` is two). Each extra language costs about 15 MB per Tesseract process; re-check the memory budget before raising it. |
| `DOCIST_OCR_JOBS` | `2` | Tesseract processes OCRmyPDF runs at once for one Page Tools OCR request. `1` halves OCR's peak memory again on a small box. |
| `DOCIST_RATE_LIMIT` | `30` | POST requests allowed per window, per client IP. |
| `DOCIST_RATE_WINDOW` | `60` | Rate-limit window in seconds. |
| `DOCIST_OUTPUT_MAX_AGE_MINUTES` | `1440` | Results in `output/` older than this are pruned automatically (check runs at most every 5 minutes, piggybacked on requests). |
| `FLASK_DEBUG` | off | Dev server debugger/reloader. **Never set in production**: the Werkzeug debugger is remote code execution for anyone who can reach it. |

### The Word engine

Paid accounts' Word files are rendered by LibreOffice Writer (installed in
the image), which keeps each document's own layout and page size; everyone
else gets the reflow onto Letter or A4. Each conversion runs in its own
process group on a fresh, locked-down profile and makes no network request:
remote images and linked templates in a `.docx` are dropped, images stored
inside the file are kept. If LibreOffice can't convert a file in time, the
file is re-flowed and the user is told. The process group is killed on every
exit the worker sees. If the worker itself is `SIGKILL`ed (gunicorn after a
failed graceful abort, the kernel's OOM killer, or `docker stop` past its
grace period), a spinning LibreOffice is stopped by its CPU limit (three times
the call's timeout), and an idle one lives until the container restarts.

`DOCIST_PASSWORD` and `DOCIST_PUBLIC_DEMO` are gone. If `DOCIST_PASSWORD` is
still set, the app logs a warning at startup and ignores it.

## Accounts and email

### Database

Tables are created at startup (`db.create_all()`); existing tables are never
altered. Every gunicorn launcher (`./run.sh prod`, the Dockerfile,
`units/docist.service`) passes `--preload`, so the app is imported once in
the master: the secret key is resolved and the schema created before the
workers fork.

- **Locally / one box:** leave `DATABASE_URL` unset. SQLite lives in
  `instance/docist.db` next to `instance/secret_key`. Back up `instance/`.
- **Railway:** add the Postgres add-on and set `DATABASE_URL` to its URL
  (`${{Postgres.DATABASE_URL}}`). **SQLite on Railway is ephemeral**: without a
  volume, every deploy starts with an empty database and nobody's account
  survives.

### Railway checklist

| Variable | Value |
|---|---|
| `DATABASE_URL` | the Postgres add-on's URL |
| `DOCIST_SECRET_KEY` | 64 hex chars, generated once and kept (a new key signs everyone out) |
| `DOCIST_COOKIE_SECURE` | `1` |
| `DOCIST_TRUSTED_PROXIES` | `1` (the default when `RAILWAY_ENVIRONMENT` is set) |
| `DOCIST_BASE_URL` | the public `https://…` origin |
| `DOCIST_EMAIL_BACKEND` and its settings | see below |

The app logs a startup warning when `DATABASE_URL` is set but
`DOCIST_SECRET_KEY` is not, and when `DATABASE_URL` is set but
`DOCIST_TRUSTED_PROXIES` is `0` (behind a proxy that would put every
anonymous visitor on the proxy's IP and one shared allowance). After the first
deploy, check one request's `X-Forwarded-For` to confirm the edge appends the
client IP as the last hop.

### Email sender

Verification and password-reset emails go through `DOCIST_EMAIL_BACKEND`:

- `console` (default) logs each email, links included, at WARNING. Fine for
  development; in production it means no one receives mail.
- `smtp` uses `DOCIST_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD` and
  `DOCIST_SMTP_TLS`.
- `resend` posts to the Resend API with `DOCIST_RESEND_API_KEY`.

Links in emails are built only from `DOCIST_BASE_URL`, never from the
request's Host header; `smtp` and `resend` refuse to send without it. A failed
send never fails the signup.

Until a real sender is configured, new accounts stay unverified and run on
guest limits, and the account page says so. Verify a user by hand:

```bash
flask --app app verify-user someone@example.com
```

### Plans

There is no billing yet. Grant a plan from the command line; any value other
than `free` counts as paid (unlimited by default):

```bash
flask --app app set-plan someone@example.com monthly
flask --app app set-plan someone@example.com free
```

## What the hardening gives you

- **Accounts** — email and password (scrypt hashes via Werkzeug), signed
  session cookie (`HttpOnly`, `SameSite=Lax`, `Secure` with
  `DOCIST_COOKIE_SECURE=1`), 30-day sessions. Changing or resetting the
  password, or "sign out everywhere", ends every other session. A password
  reset also revokes every API key. Login, signup, forgot-password and
  resend-verification are throttled per IP (and per email for login and
  forgot).
- **CSRF** — every POST outside `/api/v1` needs the session's CSRF token, as a
  hidden form field or the `X-CSRF-Token` header (`static/csrf.js` adds it to
  the tool pages' `fetch()` calls). `/api/v1` ignores the cookie, so there is
  nothing to forge there.
- **Daily limits** — per IP for anonymous use, per account once verified,
  stored in the database so they are exact across workers.
- **Download safety** — `/download` serves only plain filenames that exist
  inside `output/`; traversal attempts are rejected.
- **Upload validation** — file content is sniffed against the claimed
  extension (magic bytes for binary formats, binary-content rejection for
  text formats) before any converter touches it.
- **Isolated storage** — every request processes uploads in its own
  temporary directory; nothing persists except the result in `output/`,
  which is pruned on a timer. Each result's name starts with an owner tag (an
  HMAC of the signed-in user or the anonymous browser session) and a random
  128-bit key. `/download` answers 404 unless the tag is the asker's, so a
  leaked or guessed result URL is useless to anyone else.
- **No external fetches while rendering** — HTML, Markdown and DOCX are
  rendered with a link callback that only allows inline `data:` URIs, so an
  uploaded document cannot make the server request URLs or read local files.
- **Rate limiting** — sliding-window per-IP limit on POSTs (the endpoints
  that do real work, `/login` included), on top of the daily limits. Note:
  the limiter is per-process, so with N gunicorn workers the effective ceiling
  is N × limit. Keep workers low (2 is plenty) or enforce limits at the proxy
  for exact caps.

## nginx reverse proxy

```nginx
server {
    listen 443 ssl;
    server_name docist.example.com;

    # ssl_certificate ...; ssl_certificate_key ...;   (e.g. via certbot)

    client_max_body_size 100m;         # slightly above DOCIST_MAX_UPLOAD_MB_PAID

    location / {
        proxy_pass http://127.0.0.1:5010;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;       # OCR jobs can be slow
    }
}
```

Note on client IPs: the rate limiter and the anonymous allowance key on the
client IP. Behind one proxy, set `DOCIST_TRUSTED_PROXIES=1` so the app takes
the last `X-Forwarded-For` hop (the one nginx appended) and ignores anything
a client prepended. With two proxies in a chain, set `2`. Without a proxy,
leave it `0`: trusting the header with nothing in front lets any client pick
its own IP.

## systemd unit

`/etc/systemd/system/docist.service`:

```ini
[Unit]
Description=Docist PDF toolkit
After=network.target

[Service]
User=docist
WorkingDirectory=/opt/docist
Environment=DOCIST_SECRET_KEY=change-me-64-hex-chars
Environment=DOCIST_COOKIE_SECURE=1
Environment=DOCIST_TRUSTED_PROXIES=1
ExecStart=/opt/docist/.venv/bin/gunicorn --preload --workers 2 --timeout 120 \
    --bind 127.0.0.1:5010 app:app
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now docist
curl -s http://127.0.0.1:5010/healthz   # {"status": "ok"}
```

The checked-in `units/docist.service` is a user unit for a loopback-only
install; it reads settings from a gitignored `.env` instead.

## System packages (optional, for OCR)

```bash
# Fedora
sudo dnf install tesseract ghostscript
# Debian/Ubuntu
sudo apt install tesseract-ocr ghostscript
```

## Honest limitations to keep in mind

- PDF password protection uses AES-256 (`/V 5 /R 6`), which pypdf provides
  via the `cryptography` package in `requirements.txt` — keep that dependency
  installed or protecting a PDF will fail. Unlocking still accepts legacy
  RC4-40/RC4-128/AES-128 files created elsewhere. The encryption is only ever
  as good as the password the user picks, so it is still not a place to put
  real secrets.
- **The operation count is unweighted.** A 50-page OCR run costs the same one
  operation as a two-page merge.
- **Some work is free.** Failed operations (including a 120 s OCR timeout)
  and page thumbnails cost no operations; only the per-IP POST limiter bounds
  them.
- **Throttles and the rate limiter are in memory, per worker** (so double the
  stated numbers with 2 workers), and a restart clears them. The daily limits
  are exact because they live in the database. Concurrent requests right at
  the limit can overshoot by at most the number of workers.
- **The per-email login throttle can lock a victim out** for 15 minutes if
  someone else keeps failing on their address.
- **No migrations.** `create_all` only adds missing tables; the first column
  added to an existing table needs Flask-Migrate or hand-written SQL.
- **No pruning of usage rows or used email tokens** yet. They are tiny.
- **A lost secret key signs everyone out** and orphans every stored result.
  Without `DOCIST_SECRET_KEY`, a Railway redeploy does exactly that.
- **Results are not carried over** when an anonymous visitor signs in; they
  stay tied to the old session until they age out.
- No billing, admin UI, or OAuth. Plans are set with `flask set-plan`.
