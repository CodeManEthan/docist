# Docist — production image (see DEPLOYMENT.md for configuration)
FROM python:3.12-slim

# System deps: tesseract + ghostscript for OCR (ocrmypdf), fonts for PDF rendering
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
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
CMD gunicorn --workers 2 --timeout 120 --forwarded-allow-ips='*' --bind "0.0.0.0:${PORT}" app:app
