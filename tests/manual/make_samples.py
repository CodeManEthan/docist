#!/usr/bin/env python
"""Regenerate the sample files behind "Try with sample files" on the Merge page.

Writes three small files into static/samples/, which are committed and served
as static files:

    sample-report.pdf   a two-page PDF report
    sample-note.md      a Markdown note
    sample-chart.png    a PNG bar chart

The content is neutral placeholder text and made-up numbers. Run it after
changing anything here:

    .venv/bin/python tests/manual/make_samples.py

Uses only reportlab and Pillow from requirements.txt. The PDF is written in
reportlab's invariant mode, so the same script gives the same bytes.
"""
from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

SAMPLES = Path(__file__).resolve().parents[2] / "static" / "samples"
PAGE_W, PAGE_H = letter

# Made-up numbers shared by the report table and the chart.
SERIES = [("Q1", 42), ("Q2", 57), ("Q3", 49), ("Q4", 68)]

# Each page: a heading, then (font, text) lines.
BODY, BOLD, MONO = ("Helvetica", 12), ("Helvetica-Bold", 13), ("Courier", 12)
REPORT_PAGES = [
    ("Sample report", [
        (BODY, "This two-page report is a sample file for trying Docist."),
        (BODY, "Merge it with the note and the chart that come with it, or drop"),
        (BODY, "in files of your own. Every name and number here is made up."),
        (BODY, ""),
        (BOLD, "Summary"),
        (BODY, "Output grew in each quarter but the third, ending the year"),
        (BODY, "at its highest level. The chart file shows the same figures."),
    ]),
    ("Figures by quarter", [
        (MONO, "Quarter      Units"),
        *[(MONO, f"{label:<12} {value}") for label, value in SERIES],
        (BODY, ""),
        (BOLD, "Notes"),
        (BODY, "The figures are placeholders, chosen only to give the chart a shape."),
    ]),
]


def make_report(path):
    c = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    c.setTitle("Sample report")
    c.setAuthor("Docist")
    total = len(REPORT_PAGES)
    for number, (heading, lines) in enumerate(REPORT_PAGES, start=1):
        c.setFillColor(HexColor("#2f6f8f"))
        c.rect(0, PAGE_H - 54, PAGE_W, 54, fill=1, stroke=0)
        c.setFillColor(HexColor("#ffffff"))
        c.setFont("Helvetica-Bold", 20)
        c.drawString(54, PAGE_H - 36, heading)
        c.setFillColor(HexColor("#222222"))
        y = PAGE_H - 110
        for font, line in lines:
            c.setFont(*font)
            c.drawString(54, y, line)
            y -= 20
        c.setFont("Helvetica", 9)
        c.setFillColor(HexColor("#666666"))
        c.drawRightString(PAGE_W - 54, 36, f"Page {number} of {total}")
        c.showPage()
    c.save()


NOTE = """# Sample note

A short Markdown note to merge with the sample report and chart.

## Checklist

- Read the report
- Look at the chart
- Merge all three into one PDF

## Figures

| Quarter | Units |
|---------|-------|
""" + "".join(f"| {label} | {value} |\n" for label, value in SERIES) + """
The numbers are made up for this sample.
"""


def make_note(path):
    path.write_text(NOTE, encoding="utf-8", newline="\n")


def make_chart(path):
    width, height = 480, 300
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    left, right, top, bottom = 50, width - 30, 40, height - 40
    draw.text((left, 12), "Units by quarter (sample data)", fill=(34, 34, 34))
    draw.line([(left, top), (left, bottom), (right, bottom)], fill=(120, 120, 120), width=2)
    peak = max(value for _, value in SERIES)
    slot = (right - left) / len(SERIES)
    for i, (label, value) in enumerate(SERIES):
        x0 = left + slot * i + slot * 0.2
        x1 = left + slot * (i + 1) - slot * 0.2
        y0 = bottom - (bottom - top - 10) * value / peak
        draw.rectangle([x0, y0, x1, bottom - 1], fill=(47, 111, 143))
        draw.text((x0 + 4, y0 - 14), str(value), fill=(34, 34, 34))
        draw.text((x0 + 4, bottom + 8), label, fill=(34, 34, 34))
    img.save(path, "PNG", optimize=True)


def main():
    SAMPLES.mkdir(parents=True, exist_ok=True)
    make_report(SAMPLES / "sample-report.pdf")
    make_note(SAMPLES / "sample-note.md")
    make_chart(SAMPLES / "sample-chart.png")
    for path in sorted(SAMPLES.iterdir()):
        print(f"{path.relative_to(SAMPLES.parents[1])}  {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
