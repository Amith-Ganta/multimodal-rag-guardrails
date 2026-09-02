"""Ingest every PDF in data/ and build + save the multimodal index.

Run:
    python scripts/build_index.py            # ingest all PDFs in data/
    python scripts/build_index.py foo.pdf    # ingest a specific file

If data/ is empty, generate the synthetic sample first:
    python scripts/make_sample_pdf.py

The text side uses the local MiniLM encoder by default (no key needed). The
image side uses CLIP. Both models download on first run.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import DATA_DIR  # noqa: E402
from src.index import MultimodalIndex  # noqa: E402
from src.ingest import ingest_pdf  # noqa: E402


def _pdfs(argv: list[str]) -> list[Path]:
    if argv:
        return [Path(a) if Path(a).is_absolute() else DATA_DIR / a for a in argv]
    return sorted(Path(DATA_DIR).glob("*.pdf"))


def main(argv: list[str]) -> int:
    pdfs = _pdfs(argv)
    if not pdfs:
        print(
            "No PDFs found in data/. Generate the synthetic sample with:\n"
            "    python scripts/make_sample_pdf.py"
        )
        return 1

    ingested = []
    for pdf in pdfs:
        if not pdf.exists():
            print(f"skip (missing): {pdf}")
            continue
        doc_id = pdf.stem
        res = ingest_pdf(str(pdf), doc_id=doc_id)
        print(
            f"ingested {pdf.name}: {len(res.text_chunks)} text chunks, "
            f"{len(res.images)} images"
        )
        ingested.append(res)

    if not ingested:
        print("nothing ingested.")
        return 1

    index = MultimodalIndex().build(ingested)
    out = index.save()
    print(f"index saved -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
