"""Two FAISS indexes, one per modality, because the dual encoder produces two
different vector spaces.

  * text index  -> TextEncoder space (e.g. 384-dim MiniLM / 1536-dim OpenAI)
  * image index -> CLIP space (512-dim)

Both use inner-product search on L2-normalised vectors, so the score is cosine
similarity in [-1, 1]. Payloads (chunk text, image ids) are kept in parallel
Python lists and pickled next to the FAISS files so a rebuilt process can answer
without re-embedding.
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import numpy as np

from .config import SETTINGS
from .encoders import get_image_encoder, get_text_encoder
from .ingest import IngestResult, TextChunk, load_pil_images


@dataclass
class TextHit:
    chunk: TextChunk
    score: float


@dataclass
class ImageHit:
    image_id: str
    doc_id: str
    page: int
    score: float


class MultimodalIndex:
    """Holds both FAISS indexes plus the payloads needed to answer."""

    def __init__(self) -> None:
        self._faiss = None  # lazy import
        self.text_index = None
        self.image_index = None
        self.text_payloads: List[TextChunk] = []
        self.image_ids: List[str] = []
        self.image_meta: List[Tuple[str, int]] = []  # (doc_id, page) per image
        self.image_store: dict[str, str] = {}  # image_id -> base64 PNG

    # --- build -------------------------------------------------------------
    def build(self, ingested: List[IngestResult]) -> "MultimodalIndex":
        import faiss

        self._faiss = faiss
        text_enc = get_text_encoder()

        # ---- text side ----
        all_chunks: List[TextChunk] = []
        for res in ingested:
            all_chunks.extend(res.text_chunks)
        if all_chunks:
            text_vecs = text_enc.encode([c.text for c in all_chunks])
            self.text_index = faiss.IndexFlatIP(text_enc.dim)
            self.text_index.add(text_vecs)
            self.text_payloads = all_chunks

        # ---- image side ----
        all_images = []
        for res in ingested:
            all_images.extend(res.images)
            self.image_store.update(res.image_store)
        if all_images:
            img_enc = get_image_encoder()
            pil_images = load_pil_images(all_images)
            img_vecs = img_enc.encode_images(pil_images)
            self.image_index = faiss.IndexFlatIP(img_enc.dim)
            self.image_index.add(img_vecs)
            self.image_ids = [im.id for im in all_images]
            self.image_meta = [(im.doc_id, im.page) for im in all_images]

        return self

    # --- search ------------------------------------------------------------
    def search_text(self, query: str, k: int | None = None) -> List[TextHit]:
        if self.text_index is None or not self.text_payloads:
            return []
        k = k or SETTINGS.top_k_text
        qv = get_text_encoder().encode([query])
        scores, idxs = self.text_index.search(qv, min(k, len(self.text_payloads)))
        hits: List[TextHit] = []
        for score, idx in zip(scores[0], idxs[0]):
            if idx < 0:
                continue
            hits.append(TextHit(chunk=self.text_payloads[idx], score=float(score)))
        return hits

    def search_images(self, query: str, k: int | None = None) -> List[ImageHit]:
        """Cross-modal: a TEXT query retrieves images via CLIP's text tower."""
        if self.image_index is None or not self.image_ids:
            return []
        k = k or SETTINGS.top_k_image
        qv = get_image_encoder().encode_query_text([query])
        scores, idxs = self.image_index.search(qv, min(k, len(self.image_ids)))
        hits: List[ImageHit] = []
        for score, idx in zip(scores[0], idxs[0]):
            if idx < 0:
                continue
            doc_id, page = self.image_meta[idx]
            hits.append(
                ImageHit(
                    image_id=self.image_ids[idx],
                    doc_id=doc_id,
                    page=page,
                    score=float(score),
                )
            )
        return hits

    # --- persistence -------------------------------------------------------
    def save(self, directory: str | Path | None = None) -> Path:
        import faiss

        from .config import INDEX_DIR

        directory = Path(directory) if directory else INDEX_DIR
        directory.mkdir(parents=True, exist_ok=True)
        if self.text_index is not None:
            faiss.write_index(self.text_index, str(directory / "text.faiss"))
        if self.image_index is not None:
            faiss.write_index(self.image_index, str(directory / "image.faiss"))
        with open(directory / "payloads.pkl", "wb") as fh:
            pickle.dump(
                {
                    "text_payloads": self.text_payloads,
                    "image_ids": self.image_ids,
                    "image_meta": self.image_meta,
                    "image_store": self.image_store,
                },
                fh,
            )
        return directory

    @classmethod
    def load(cls, directory: str | Path | None = None) -> "MultimodalIndex":
        import faiss

        from .config import INDEX_DIR

        directory = Path(directory) if directory else INDEX_DIR
        obj = cls()
        obj._faiss = faiss
        text_path = directory / "text.faiss"
        image_path = directory / "image.faiss"
        if text_path.exists():
            obj.text_index = faiss.read_index(str(text_path))
        if image_path.exists():
            obj.image_index = faiss.read_index(str(image_path))
        with open(directory / "payloads.pkl", "rb") as fh:
            data = pickle.load(fh)
        obj.text_payloads = data["text_payloads"]
        obj.image_ids = data["image_ids"]
        obj.image_meta = data["image_meta"]
        obj.image_store = data["image_store"]
        return obj
