# PDF Merger & Converter

A web application that merges PDFs — and converts common document and image formats to PDF on the fly — with configurable page numbering, blank page insertion, bookmarks, page tools, watermarking, and password protection.

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
   cd ~/projects/PDF-Merger
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

1. Activate the virtual environment (if not already activated):
   ```bash
   source .venv/bin/activate
   ```

2. Run the Flask application:
   ```bash
   python app.py
   ```

3. Open your browser and navigate to:
   ```
   http://localhost:5010
   ```

## Usage

1. Click the upload area or drag and drop PDF files
2. Select multiple PDFs (they will be merged in selection order)
3. Review the file list to confirm order
4. Click "Merge PDFs" button
5. Wait for processing to complete
6. Download your merged, numbered PDF

## How It Works

1. **Upload**: You upload multiple PDF files through the web interface
2. **Merge**: The application merges the PDFs in order
3. **Blank Pages**: After each PDF with an odd number of pages, a blank page is inserted
4. **Numbering**: Page numbers are added to the bottom right corner of every page
5. **Download**: The final merged PDF is available for download

## File Structure

```
PDF-Merger/
├── app.py                      # Flask bootstrap (auto-registers blueprints)
├── routes/                    # Flask blueprints (auto-discovered)
│   ├── merge.py               # Merge & convert endpoints
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
│   ├── watermark.py           # Text watermarking
│   ├── stamp.py               # Header/footer, Bates numbering
│   └── security.py            # Protect / unlock
├── converters/                # Format-to-PDF converter plugins (auto-discovered)
├── transforms/                # File-to-file transform plugins (auto-discovered,
│                              #   with automatic pivot-through-PDF routing)
├── templates/                 # Web interface (one page per tool area)
├── static/                    # Shared theme CSS + per-page JS
├── tests/                     # Backend (pytest) and frontend (node:test) suites
├── uploads/                   # Temporary upload storage
├── output/                    # Processed PDF output
├── Blank PDF Document.pdf     # Blank page template
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
- Flask, PyPDF2, reportlab, werkzeug
- Markdown, xhtml2pdf (Markdown/HTML/DOCX rendering)
- Pillow, pillow-heif, svglib (image/vector conversion)
- mammoth (DOCX → HTML), openpyxl (XLSX), striprtf (RTF)
- pypdfium2 (PDF page rendering for export)
- html2text (HTML → Markdown/text), PyYAML (JSON ↔ YAML)

## Notes

- Maximum file size: 50MB per upload
- Accepted formats are listed by the `/formats` endpoint and shown in the UI
- Uploaded files are temporarily stored and cleaned on each new upload
- The blank page ensures proper alignment for duplex printing
- Conversion fidelity notes: DOCX styling is simplified (semantic structure is kept, Word theme fonts/colors are not); HTML rendering ignores external resources and JavaScript; plain text supports Latin-1 glyphs (others render as `?`)
