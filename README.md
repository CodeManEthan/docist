# Docist

A self-hosted document toolkit. Docist merges PDFs (auto-converting ~19 input formats on the way in), converts files between ~150 format pairs, edits and splits pages, imposes booklets, exports to images or text with OCR, and stamps, watermarks, and password-protects documents — all behind an optional login gate, ready to deploy.

## Features

### Merge & Convert (`/`)
- **Multiple File Upload**: Select and upload multiple files at once
- **Format Conversion**: Non-PDF files are automatically converted to PDF before merging:
  - Markdown (`.md`, `.markdown`) — headings, tables, code blocks, lists
  - Word documents (`.docx`)
  - HTML (`.html`, `.htm`)
  - Plain text (`.txt`) — monospace, line-wrapped, multi-page
  - Rich text (`.rtf`) — plain-text extraction
  - Spreadsheets (`.csv`, `.xlsx`) — rendered as tables, one per sheet, landscape for wide sheets
  - Images (`.png`, `.jpg`, `.jpeg`, `.gif`, `.bmp`, `.webp`, `.tiff`, `.tif`, `.heic`, `.heif`) — centered on letter pages, multi-frame TIFF/GIF becomes multiple pages
  - Vector graphics (`.svg`) — scaled to fit a letter page
- **Reorder & Remove**: Drag rows (or use ▲/▼ buttons) to reorder files before merging; remove files with ✕
- **Merge Options**: Toggle page numbers (position: bottom left/center/right, custom start number), toggle blank-page insertion, toggle bookmarks
- **Bookmarks**: The merged PDF gets one outline entry per source file
- **Front Page Alignment**: Blank pages after odd-page documents keep every file starting on a front page (perfect for duplex printing)
- **Interleave Mode**: Combine two separately-scanned stacks (fronts + backs) by alternating pages, with reverse-order handling for flatbed/ADF back-side scans

### Convert Files (`/convert`)
Universal file-to-file conversion — upload a file, pick a target format. The page shows the full conversion matrix.
- **Images ↔ images**: PNG, JPG, WebP, BMP, TIFF, GIF, HEIC/HEIF — every direction (alpha flattened for JPEG/BMP, adaptive palette for GIF)
- **Documents**: Markdown ↔ HTML, HTML → Markdown/text, DOCX → HTML/Markdown/text, RTF → text, Markdown → text
- **Data**: CSV ↔ XLSX, CSV/XLSX → JSON, JSON → CSV, JSON ↔ YAML, CSV → HTML table
- **PDF bridge**: any supported format → PDF; PDF → PNG/JPG (zip when multi-page) or text
- **Image → text (OCR)**: any raster image → `.txt` via Tesseract
- **Pivot routing**: when no direct path exists, conversions chain automatically through PDF (e.g. Markdown → PNG, SVG → JPG)

### Page Tools (`/pages`)
- **Extract / Remove pages** by range spec (e.g. `1-3,5,8-10`)
- **Rotate pages** (90°/180°/270°, all pages or a range)
- **Split PDF** — every N pages, or by ranges (`;`-separated), delivered as a zip
- **Compress** — lossless content-stream compression plus image downsampling/re-encoding (quality and DPI caps); output never larger than input
- **OCR (make searchable)** — adds an invisible text layer to scanned PDFs via OCRmyPDF/Tesseract; language selection, optional deskew, force re-OCR; pages that already have text pass through untouched

### Print Prep (`/print`)
- **N-up** — 2 or 4 pages per sheet, aspect-preserving, centered
- **Booklet** — saddle-stitch imposition: print duplex (flip on short edge), fold in half, read in order

### Export (`/export`)
- **PDF → images** — PNG or JPG per page at 30–600 DPI, zipped
- **PDF → text** — UTF-8 text with page separators, with optional OCR fallback for scanned pages (only pages without a text layer are OCR'd)

### Watermark & Security (`/security`)
- **Text watermark** — center (diagonal), header, or footer; opacity, font size, rotation, color
- **Header / Footer** — six slots (left/center/right × top/bottom) with `{page}` and `{pages}` placeholders
- **Bates numbering** — prefix + zero-padded counter (e.g. ACME000001), four corner positions
- **Protect** — password-encrypt a PDF (RC4-128 via PyPDF2)
- **Unlock** — remove password protection (requires the current password)

## Security & Deployment

Docist ships hardened for small self-hosted deployments (see `DEPLOYMENT.md` for the full recipe — gunicorn, nginx, systemd):

- **Login gate** — set `DOCIST_PASSWORD` and every page and endpoint requires sign-in (session cookies are `HttpOnly`/`SameSite=Lax`; password checks are constant-time). Unset, the app runs open for local use.
- **Safe downloads** — the shared `/download` endpoint refuses path traversal; only plain filenames inside `output/` are served.
- **Upload validation** — file content is sniffed (magic bytes) against the claimed extension before any converter runs.
- **Isolated, self-cleaning storage** — every request works in its own temp directory; results get collision-safe names and are pruned from `output/` after 24h (configurable).
- **Rate limiting** — sliding-window per-IP limit on all POSTs, `/login` included.
- **Sane defaults** — dev server binds `127.0.0.1`, debug is off unless `FLASK_DEBUG=1`, request size capped at 50 MB.

## Adding a New Converter

Converters are plugins auto-discovered from the `converters/` package. Drop in a module that defines:

```python
EXTENSIONS = ['.ext']

def convert(input_path, output_path):
    ...  # write a PDF to output_path, raise converters.ConversionError on failure
```

Restart the app and the new format is accepted automatically (the UI reads `/formats`).

## Setup

1. Make sure you're in the project directory:
   ```bash
   cd ~/projects/Docist
   ```

2. Activate the virtual environment:
   ```bash
   source .venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Running the Application

```bash
./run.sh                                  # dev server on http://localhost:5010
DOCIST_PASSWORD=secret ./run.sh prod      # gunicorn with the login gate on
```

Or manually: `source .venv/bin/activate && python app.py`

## File Structure

```
Docist/
├── app.py                      # Flask bootstrap: env config, auto-registered
│                               #   blueprints, rate limit + cleanup hooks
├── routes/                    # Flask blueprints (auto-discovered)
│   ├── auth.py                # Login gate (active when DOCIST_PASSWORD is set)
│   ├── merge.py               # Merge & convert endpoints + shared /download
│   ├── convert.py             # Universal file-to-file conversion
│   ├── pages.py               # Page tools endpoints
│   ├── print.py               # Print prep endpoints
│   ├── export.py              # Export endpoints
│   └── security.py            # Watermark & security endpoints
├── pdf_ops/                   # Pure PDF operations (no Flask)
│   ├── merge.py               # Configurable merge pipeline + bookmarks + interleave
│   ├── pages.py               # Split / extract / remove / rotate
│   ├── optimize.py            # Compression
│   ├── imposition.py          # N-up / booklet
│   ├── export.py              # PDF → images / text
│   ├── ocr.py                 # OCR (make searchable)
│   ├── watermark.py           # Text watermarking
│   ├── stamp.py               # Header/footer, Bates numbering
│   └── security.py            # Protect / unlock
├── converters/                # Format-to-PDF converter plugins (auto-discovered)
├── transforms/                # File-to-file transform plugins (auto-discovered,
│                              #   with automatic pivot-through-PDF routing)
├── utils/                     # Web-layer helpers: collision-safe naming,
│                              #   upload sniffing, output pruning, rate limiter
├── templates/                 # Web interface (one page per tool area + login)
├── static/                    # Shared theme CSS + per-page JS
├── tests/                     # Backend (pytest) and frontend (node:test) suites
├── output/                    # Processed results (auto-pruned)
├── DEPLOYMENT.md              # Production setup: gunicorn, nginx, systemd
├── requirements.txt           # Runtime dependencies
└── requirements-dev.txt       # + pytest
```

## Testing

```bash
.venv/bin/python -m pytest tests        # backend + page-serving tests
node --test tests/frontend/*.test.js    # frontend pure-logic tests
```

## Requirements

### System prerequisites (optional, for OCR features)

OCR features (Make Searchable, image → text, OCR fallback in Export) require
system binaries; without them the app runs normally and OCR options appear
disabled with an explanatory hint:

```bash
# Fedora
sudo dnf install tesseract ghostscript
# Debian/Ubuntu
sudo apt install tesseract-ocr ghostscript
```

Additional OCR languages are Tesseract data packages (e.g. `tesseract-langpack-deu`).

### Python

- Python 3.8+
- Flask, PyPDF2, reportlab, werkzeug, gunicorn
- Markdown, xhtml2pdf (Markdown/HTML/DOCX rendering)
- Pillow, pillow-heif, svglib (image/vector conversion)
- mammoth (DOCX → HTML), openpyxl (XLSX), striprtf (RTF)
- pypdfium2 (PDF page rendering for export)
- html2text (HTML → Markdown/text), PyYAML (JSON ↔ YAML)

## Notes

- Maximum upload size: 50 MB per request (configurable via `DOCIST_MAX_UPLOAD_MB`)
- Accepted formats are listed by the `/formats` endpoint and shown in the UI
- Uploads are processed in per-request temporary directories and never persisted; results live in `output/` until pruned
- Conversion fidelity notes: DOCX styling is simplified (semantic structure is kept, Word theme fonts/colors are not); HTML rendering ignores external resources and JavaScript; plain text supports Latin-1 glyphs (others render as `?`)
