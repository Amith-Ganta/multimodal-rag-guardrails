"""Generate a SYNTHETIC sample PDF for the demo, with text and one image.

The document is entirely made up for demonstration; it is not a real report and
its numbers are illustrative. It exists so the multimodal pipeline has something
to ingest out of the box (text chunks + at least one embedded image), and so the
golden set in goldens/ has a document to be answered from.

The one image is a small bar chart drawn with PIL (no matplotlib dependency),
comparing retrieval accuracy across three configurations. Its content matches
golden g3.

Run:
    python scripts/make_sample_pdf.py
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Make the package importable when run as a plain script.
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import DATA_DIR  # noqa: E402

PAGE_TEXT_1 = """Multimodal Retrieval Evaluation (Synthetic Sample)

This document is a synthetic sample used to demonstrate a dual-encoder
multimodal retrieval system. All figures below are illustrative and do not
describe any real deployment.

System design. The system uses two separate embedding towers. A text tower (a
sentence embedding model) encodes text passages, and a CLIP image tower encodes
images. The two towers produce vectors in different spaces, so they are stored
in two separate indexes. A text query is embedded by both towers, which lets a
short query retrieve both relevant passages and relevant images.

Text preparation. Document text is split into chunks of 500 characters with an
overlap of 100 characters before it is embedded. The overlap keeps a claim that
straddles a chunk boundary retrievable from either side.

Headline result. On the held-out set, the dual-encoder retrieval model reports
an overall accuracy of 0.86. This is the combined configuration that uses both
the text tower and the image tower together.
"""

PAGE_TEXT_2 = """Results by configuration.

The chart on this page compares retrieval accuracy across three configurations:
text-only, image-only, and the combined multimodal configuration. The combined
configuration is the highest of the three, which is the motivation for using
both towers rather than either one alone.

Reading the chart. Each bar is one configuration. Taller is better. The
combined bar is tallest, the text-only bar is in the middle, and the image-only
bar is the shortest for this synthetic data.
"""


def _make_bar_chart() -> bytes:
    """Draw a simple three-bar accuracy chart with PIL. Returns PNG bytes."""
    W, H = 520, 360
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 16)
        small = ImageFont.truetype("arial.ttf", 13)
    except OSError:
        font = ImageFont.load_default()
        small = font

    d.text((20, 12), "Retrieval accuracy by configuration", fill="black", font=font)

    # axes
    left, bottom, top = 70, 300, 60
    d.line([(left, top), (left, bottom)], fill="black", width=2)
    d.line([(left, bottom), (W - 30, bottom)], fill="black", width=2)

    labels = ["text-only", "image-only", "combined"]
    values = [0.71, 0.63, 0.86]  # illustrative, matches the headline 0.86
    colors = [(70, 110, 200), (120, 160, 90), (200, 120, 60)]

    bar_w = 90
    gap = 50
    x = left + gap
    scale = (bottom - top) / 1.0  # values are in [0, 1]
    for label, val, color in zip(labels, values, colors):
        bar_h = int(val * scale)
        y0 = bottom - bar_h
        d.rectangle([x, y0, x + bar_w, bottom], fill=color)
        d.text((x + 8, y0 - 20), f"{val:.2f}", fill="black", font=small)
        d.text((x + 4, bottom + 8), label, fill="black", font=small)
        x += bar_w + gap

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> Path:
    import pymupdf  # modern name for fitz

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(DATA_DIR) / "sample_report.pdf"

    doc = pymupdf.open()

    # page 1: text only
    p1 = doc.new_page()
    p1.insert_textbox(
        pymupdf.Rect(50, 50, 545, 780), PAGE_TEXT_1, fontsize=11, fontname="helv"
    )

    # page 2: text + embedded chart image
    p2 = doc.new_page()
    p2.insert_textbox(
        pymupdf.Rect(50, 50, 545, 300), PAGE_TEXT_2, fontsize=11, fontname="helv"
    )
    png = _make_bar_chart()
    p2.insert_image(pymupdf.Rect(60, 320, 580, 680), stream=png)

    doc.save(str(out))
    doc.close()
    print(f"wrote synthetic sample PDF -> {out}")
    return out


if __name__ == "__main__":
    main()
