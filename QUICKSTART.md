# Docist quick start

## Install

Needs Python 3.11+ on Linux (macOS should work; untested).

```bash
git clone https://github.com/CodeManEthan/docist.git
cd docist
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

OCR needs system packages (`tesseract` and `ghostscript`; see the README's
Requirements). Without them the app runs and the OCR options are greyed out.

## Run

```bash
./run.sh          # dev server
./run.sh prod     # gunicorn, production settings
```

Then open **http://localhost:5010**. The port is configurable:
`DOCIST_PORT=8080 ./run.sh`.

Or with Docker (the image listens on 5010):

```bash
docker build -t docist .
docker run -p 5010:5010 docist
```

No login is needed to use the tools. Anonymous visitors get 10 operations a
day; the nav shows how many are left. For more, sign up at
http://localhost:5010/signup. Locally, the verification email is printed in the
terminal running Docist: open its link to verify, and a verified account gets
50 a day plus API keys on its **Account** page. Accounts are stored in SQLite
under `instance/`.

See `DEPLOYMENT.md` for the full production setup (secret key, database,
email sender, reverse proxy, systemd, environment variables).

## Quick test

1. Visit http://localhost:5010 and click **Try with sample files**: a PDF, a
   Markdown note and a PNG chart are queued, the PDF with a first-page
   thumbnail.
2. Reorder rows with ▲/▼ or by dragging, or add your own files: PDFs, Word,
   Markdown, images, spreadsheets and more.
3. Click **Merge Files** and download your merged, numbered PDF.

Then try the **Packet Builder** at http://localhost:5010/packet: add files of
any kind, click **All pages** to see every page, drag pages between files, add
a cover, sections and a contents page, and build one PDF. Undo works on every
change, and a reload offers to restore your draft.

The nav bar reaches every tool: Merge & Convert, Packet Builder, Convert
Files, Page Tools, Print Prep, Export, and Watermark & Security. Every
one-file tool is also on the REST API; see the reference at
http://localhost:5010/api.

The full variable list is in `DEPLOYMENT.md`.
