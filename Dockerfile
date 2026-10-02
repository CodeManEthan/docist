# Docist — production image (see DEPLOYMENT.md for configuration)
FROM python:3.12-slim

# System deps: tesseract + ghostscript for OCR (ocrmypdf), fonts for PDF rendering.
# OCR language packs: English (with OSD) comes with tesseract-ocr; the rest are
# the 14-language set (round prelaunch-fixes [Q2], ruled 2026-10-02), about
# 30 MB. Names for each are in pdf_ops/ocr_langs.py.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-spa \
    tesseract-ocr-fra \
    tesseract-ocr-deu \
    tesseract-ocr-por \
    tesseract-ocr-ita \
    tesseract-ocr-chi-sim \
    tesseract-ocr-chi-tra \
    tesseract-ocr-vie \
    tesseract-ocr-ara \
    tesseract-ocr-rus \
    tesseract-ocr-kor \
    tesseract-ocr-jpn \
    tesseract-ocr-fil \
    ghostscript \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home docist && chown -R docist:docist /app
USER docist

ENV PORT=5010
EXPOSE 5010

# $PORT is injected by the platform (Railway) and defaults to 5010 locally.
# --preload imports the app once in the master, so the secret key and the
# schema are set up before the workers fork. Proxy trust is ProxyFix's job
# (DOCIST_TRUSTED_PROXIES), not gunicorn's.
CMD gunicorn --preload --workers 2 --timeout 120 --bind "0.0.0.0:${PORT}" app:app
