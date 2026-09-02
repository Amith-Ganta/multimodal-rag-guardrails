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

from langchain_text_splitters import RecursiveCharacterTextSplitter

from .config import SETTINGS


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
                for c_i, chunk in enumerate(splitter.split_text(page_text)):
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
