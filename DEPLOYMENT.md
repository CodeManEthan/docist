---
type: repo-doc
project: docist
description: "Production deployment recipe for Docist: the one-command gunicorn start, every DOCIST_* environment variable, what the hardening actually provides, nginx reverse-proxy and systemd unit templates, and honest limitations."
tags: [howto, deployment, ops, auth]
updated: 2026-08-01
---

# Deploying Docist

Docist's dev server (`python app.py`) is for localhost only. For anything
reachable by other people, run gunicorn behind a reverse proxy and set a
password. This document is the complete recipe.

## The short version

```bash
DOCIST_PASSWORD='choose-a-strong-password' \
DOCIST_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
./run.sh prod
```

That starts gunicorn on `127.0.0.1:5010` with the login gate on, debug off,
and rate limiting active. Put nginx (or Caddy) in front for TLS.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DOCIST_PASSWORD` | *(unset — gate off)* | Shared access password. Setting it turns the login gate on for every page and endpoint (only `/login`, `/healthz` and static assets stay open). |
| `DOCIST_PUBLIC_DEMO` | *(unset)* | Set to `1` to print the password on the login page and prefill the box. For a portfolio demo that anyone may try: the gate then only keeps crawlers and scripts out. |
| `DOCIST_SECRET_KEY` | random per process | Signs session cookies. **Set it in production** — otherwise every restart logs everyone out (and multiple gunicorn workers would each mint their own key, breaking logins entirely). |
| `DOCIST_HOST` | `127.0.0.1` | Dev-server bind address (`app.py` only; gunicorn takes `--bind`). |
| `DOCIST_PORT` | `5010` | Port for both `run.sh` modes. |
| `DOCIST_MAX_UPLOAD_MB` | `50` | Request-size cap; oversized uploads are rejected with 413. |
| `DOCIST_RATE_LIMIT` | `30` | POST requests allowed per window, per client IP. |
| `DOCIST_RATE_WINDOW` | `60` | Rate-limit window in seconds. |
| `DOCIST_OUTPUT_MAX_AGE_MINUTES` | `1440` | Results in `output/` older than this are pruned automatically (check runs at most every 5 minutes, piggybacked on requests). |
| `DOCIST_COOKIE_SECURE` | off | Set to `1` once you serve over HTTPS so session cookies are `Secure`. |
| `FLASK_DEBUG` | off | Dev server debugger/reloader. **Never set in production** — the Werkzeug debugger is remote code execution for anyone who can reach it. |

## What the hardening gives you

- **Login gate** — one shared password, session cookie (`HttpOnly`,
  `SameSite=Lax`), constant-time comparison, page requests redirect to
  `/login`, API requests get JSON 401s.
- **Download safety** — `/download` serves only plain filenames that exist
  inside `output/`; traversal attempts are rejected.
- **Upload validation** — file content is sniffed against the claimed
  extension (magic bytes for binary formats, binary-content rejection for
  text formats) before any converter touches it.
- **Isolated storage** — every request processes uploads in its own
  temporary directory; nothing persists except the result in `output/`,
  which is pruned on a timer. Each result's name starts with a random
  128-bit key that only the requester receives, and `/download` refuses any
  name without one, so one visitor cannot fetch another's result.
- **No external fetches while rendering** — HTML, Markdown and DOCX are
  rendered with a link callback that only allows inline `data:` URIs, so an
  uploaded document cannot make the server request URLs or read local files.
- **Rate limiting** — sliding-window per-IP limit on POSTs (the endpoints
  that do real work, `/login` included). Note: the limiter is per-process,
  so with N gunicorn workers the effective ceiling is N × limit. Keep
  workers low (2 is plenty) or enforce limits at the proxy for exact caps.

## nginx reverse proxy

```nginx
server {
    listen 443 ssl;
    server_name docist.example.com;

    # ssl_certificate ...; ssl_certificate_key ...;   (e.g. via certbot)

    client_max_body_size 60m;          # slightly above DOCIST_MAX_UPLOAD_MB

    location / {
        proxy_pass http://127.0.0.1:5010;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;       # OCR jobs can be slow
    }
}
```

Note on client IPs: the rate limiter keys on the direct peer address. Behind
a proxy that is always the proxy itself unless you trust `X-Forwarded-For`;
for a single-proxy setup you can run gunicorn with
`--forwarded-allow-ips=127.0.0.1` so `remote_addr` is the real client.

## systemd unit

`/etc/systemd/system/docist.service`:

```ini
[Unit]
Description=Docist PDF toolkit
After=network.target

[Service]
User=docist
WorkingDirectory=/opt/docist
Environment=DOCIST_PASSWORD=change-me
Environment=DOCIST_SECRET_KEY=change-me-64-hex-chars
ExecStart=/opt/docist/.venv/bin/gunicorn --workers 2 --timeout 120 \
    --forwarded-allow-ips=127.0.0.1 --bind 127.0.0.1:5010 app:app
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now docist
curl -s http://127.0.0.1:5010/healthz   # {"status": "ok"}
```

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
- One shared password, no user accounts — matches "limited-use portfolio
  hosting", not multi-tenant use.
- The rate limiter and session store are in-memory (per process); a restart
  clears both. For this app's scale that is a feature, not a bug.
