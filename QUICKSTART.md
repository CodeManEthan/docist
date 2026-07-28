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

To require a login (recommended for anything reachable by others):

```bash
DOCIST_PASSWORD=choose-a-password ./run.sh prod
```

See `DEPLOYMENT.md` for the full production setup (reverse proxy, systemd, environment variables).

## Quick Test

1. Visit http://localhost:5010
2. Click the upload area
3. Select 2-3 files — PDFs, or a mix of PDFs, Markdown, Word, images and more
4. Each PDF shows a first-page thumbnail; reorder rows with ▲/▼ or by dragging
5. Click "Merge Files"
6. Download your merged, numbered PDF

Beyond merging, the nav bar reaches every tool: Convert Files, Page Tools, Print Prep, Export, and Watermark & Security. The same operations are available over HTTP — see the API reference at http://localhost:5010/api.

The port is configurable: `DOCIST_PORT=8080 ./run.sh`. Full variable list in `DEPLOYMENT.md`.
