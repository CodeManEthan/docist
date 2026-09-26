---
type: repo-doc
project: docist
description: "Shortest path to a running Docist: venv activation, dependency install, run.sh dev/prod modes, accounts and the anonymous daily limit, and a six-step merge smoke test in the browser."
tags: [setup, ui]
updated: 2026-09-25
---

# Docist — Quick Start Guide

## Installation

1. Navigate to the project:
   ```bash
   cd ~/projects/Docist
   ```

2. Activate virtual environment:
   ```bash
   source .venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Run the Application

**Option 1: Using the run script (easiest)**
```bash
./run.sh          # dev server
./run.sh prod     # gunicorn, production settings
```

**Option 2: Manual**
```bash
source .venv/bin/activate
python app.py
```

Then open your browser to: **http://localhost:5010**

No login is needed to use the tools. Anonymous visitors get 10 operations a
day; the nav shows how many are left. For more, sign up at
http://localhost:5010/signup. Locally, the verification email is printed in the
terminal running Docist: open its link to verify, and a verified account gets
50 a day plus API keys on its **Account** page. Accounts are stored in SQLite
under `instance/`.

See `DEPLOYMENT.md` for the full production setup (secret key, database,
email sender, reverse proxy, systemd, environment variables).

## Quick Test

1. Visit http://localhost:5010
2. Click the upload area
3. Select 2-3 files — PDFs, or a mix of PDFs, Markdown, Word, images and more
4. Each PDF shows a first-page thumbnail; reorder rows with ▲/▼ or by dragging
5. Click "Merge Files"
6. Download your merged, numbered PDF

Beyond merging, the nav bar reaches every tool: Convert Files, Page Tools, Print Prep, Export, and Watermark & Security. The same operations are available over HTTP — see the API reference at http://localhost:5010/api.

The port is configurable: `DOCIST_PORT=8080 ./run.sh`. Full variable list in `DEPLOYMENT.md`.
