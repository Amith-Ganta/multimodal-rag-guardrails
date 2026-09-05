"""PDF ingestion: extract text chunks and images from a PDF.

Text is split with a recursive splitter (chunk_size/overlap from settings).
Images are extracted, converted to RGB PNG, and kept as base64 so the vision
model can be shown them later as data URIs. Each image also gets a stable id so
the retriever can point at it and the answer builder can fetch its bytes.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import re

from langchain_text_splitters import RecursiveCharacterTextSplitter

from .config import SETTINGS

# A numbered section heading on its own line, e.g. "5.3" followed by
# "Optimizer", or "6.1" / "Machine Translation". PyMuPDF emits these as separate
# short lines because they are typeset on their own line in the source PDF.
_SECTION_NUM_RE = re.compile(r"^\d+(?:\.\d+)*$")
# A heading's title line: a handful of words, no sentence-ending punctuation.
_HEADING_TEXT_RE = re.compile(r"^[A-Z][A-Za-z0-9 ,\-/&()]{0,60}$")
# Longest trailing heading block we are willing to move to the next chunk.
_MAX_HEADING_CARRY_CHARS = 80


def _trailing_heading(text: str) -> str:
    """Return the section heading stranded at the END of a chunk, or "".

    The recursive splitter cuts on character count, with no idea that
    "5.3\nOptimizer" is the title of the paragraph that begins in the NEXT
    chunk. When it splits there, the heading's most discriminative word lands
    on the preceding chunk -- which is about something else entirely -- while
    the paragraph that actually answers a question about that heading no longer
    contains the word at all. On the Transformer paper this put "Optimizer" on
    the hardware/schedule chunk and left the Adam/beta/epsilon paragraph with
    no lexical hook for the query "Which optimizer ... ?".
    """
    lines = text.rstrip().split("\n")
    carry: list[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            break
        if _SECTION_NUM_RE.match(stripped) or (
            _HEADING_TEXT_RE.match(stripped) and len(stripped.split()) <= 6
        ):
            carry.insert(0, stripped)
            if sum(len(c) for c in carry) > _MAX_HEADING_CARRY_CHARS:
                return ""
            # A bare section number is the top of the heading block; stop here.
            if _SECTION_NUM_RE.match(stripped):
                break
        else:
            break
    # Require the number+title pair, so we do not strip an ordinary short line.
    if len(carry) >= 2 and _SECTION_NUM_RE.match(carry[0]):
        return "\n".join(carry)
    return ""


def _rebalance_headings(chunks: List[str]) -> List[str]:
    """Move a trailing section heading onto the chunk it actually introduces."""
    out = list(chunks)
    for i in range(len(out) - 1):
        heading = _trailing_heading(out[i])
        if not heading:
            continue
        body = out[i][: out[i].rstrip().rfind(heading)].rstrip()
        if not body:
            continue  # chunk is only the heading; leave it attached forward
        out[i] = body
        # The splitter's overlap window often already replays the heading (and
        # the line before it) at the top of the next chunk. Prepending blindly
        # would duplicate it, so only add it when it is not already there.
        nxt = out[i + 1]
        if heading not in nxt[: len(heading) + _MAX_HEADING_CARRY_CHARS]:
            nxt = f"{heading}\n{nxt}"
        out[i + 1] = nxt
    return out


@dataclass
class TextChunk:
    doc_id: str
    page: int
    chunk_index: int
    text: str

    @property
    def id(self) -> str:
        return f"{self.doc_id}::p{self.page}::c{self.chunk_index}"


@dataclass
class ImageItem:
    doc_id: str
    page: int
    img_index: int
    b64_png: str  # base64-encoded PNG bytes, no data-URI prefix

    @property
    def id(self) -> str:
        return f"{self.doc_id}::p{self.page}::img{self.img_index}"

    def data_uri(self) -> str:
        return f"data:image/png;base64,{self.b64_png}"


@dataclass
class IngestResult:
    doc_id: str
    text_chunks: List[TextChunk] = field(default_factory=list)
    images: List[ImageItem] = field(default_factory=list)
    # id -> base64 PNG, for O(1) lookup when building the vision message.
    image_store: Dict[str, str] = field(default_factory=dict)


def ingest_pdf(pdf_path: str | Path, doc_id: str | None = None) -> IngestResult:
    """Extract text chunks and images from one PDF file."""
    import fitz  # PyMuPDF; imported lazily so the module loads without it.
    from PIL import Image

    pdf_path = Path(pdf_path)
    if doc_id is None:
        doc_id = pdf_path.stem

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=SETTINGS.text_chunk_size,
        chunk_overlap=SETTINGS.text_chunk_overlap,
    )

    result = IngestResult(doc_id=doc_id)
    doc = fitz.open(pdf_path)
    try:
        for page_num, page in enumerate(doc):
            # --- text ---
            page_text = page.get_text().strip()
            if page_text:
                page_chunks = _rebalance_headings(splitter.split_text(page_text))
                for c_i, chunk in enumerate(page_chunks):
                    result.text_chunks.append(
                        TextChunk(
                            doc_id=doc_id,
                            page=page_num,
                            chunk_index=c_i,
                            text=chunk,
                        )
                    )
            # --- images ---
            for img_i, img in enumerate(page.get_images(full=True)):
                xref = img[0]
                try:
                    base = doc.extract_image(xref)
                    pil = Image.open(io.BytesIO(base["image"])).convert("RGB")
                except Exception:
                    # Skip anything PyMuPDF/PIL cannot decode; do not crash a run.
                    continue
                buf = io.BytesIO()
                pil.save(buf, format="PNG")
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                item = ImageItem(
                    doc_id=doc_id,
                    page=page_num,
                    img_index=img_i,
                    b64_png=b64,
                )
                result.images.append(item)
                result.image_store[item.id] = b64
    finally:
        doc.close()

    return result


def load_pil_images(images: List[ImageItem]):
    """Decode base64 PNGs back to PIL images (for CLIP embedding)."""
    import base64 as _b64
    import io as _io

    from PIL import Image

    out = []
    for item in images:
        raw = _b64.b64decode(item.b64_png)
        out.append(Image.open(_io.BytesIO(raw)).convert("RGB"))
    return out
