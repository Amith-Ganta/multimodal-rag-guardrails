"""Dual-encoder embeddings for multimodal RAG.

Design decision (the whole reason this project exists): the course notebook put
BOTH text and images through CLIP's text/image towers into one 512-dim space.
CLIP's text tower silently truncates at 77 tokens, so any chunk longer than a
sentence or two loses its tail before it is ever embedded. That is a real
retrieval bug, not a style choice.

Here we use a DUAL ENCODER instead:

  * text  -> a dedicated text embedder (MiniLM locally, or OpenAI
             text-embedding-3-small over the gateway). No 77-token cap.
  * images -> CLIP's image tower.

The two live in different vector spaces with different dimensions, so they get
two separate FAISS indexes (see index.py). A text query is embedded by BOTH the
text embedder (to hit the text index) and CLIP's text tower (to hit the image
index cross-modally), which is exactly what CLIP's text tower is good at: short
query strings, well under 77 tokens.
"""

from __future__ import annotations

import functools
from typing import List

import numpy as np

from .config import SETTINGS


def _l2_normalize(vecs: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalisation so inner product equals cosine similarity."""
    vecs = np.asarray(vecs, dtype="float32")
    if vecs.ndim == 1:
        vecs = vecs[None, :]
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


# ---------------------------------------------------------------------------
# Text encoder
# ---------------------------------------------------------------------------
class TextEncoder:
    """Encodes text chunks and text queries into a text-only vector space.

    Backend is chosen by settings: "minilm" (local, offline, no key) or
    "openai" (text-embedding-3-small). Both return L2-normalised float32.
    """

    def __init__(self) -> None:
        self.backend = SETTINGS.text_encoder_backend
        self._minilm = None
        self._dim: int | None = None
        if self.backend == "minilm":
            # Lazy import so the module loads even without the model downloaded.
            from sentence_transformers import SentenceTransformer

            self._minilm = SentenceTransformer(SETTINGS.minilm_model)
            self._dim = int(self._minilm.get_sentence_embedding_dimension())
        elif self.backend == "openai":
            # 1536 dims for text-embedding-3-small.
            self._dim = 1536
        else:
            raise ValueError(
                f"Unknown TEXT_ENCODER_BACKEND {self.backend!r}; "
                "use 'minilm' or 'openai'."
            )

    @property
    def dim(self) -> int:
        assert self._dim is not None
        return self._dim

    def encode(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype="float32")
        if self.backend == "minilm":
            vecs = self._minilm.encode(
                texts, convert_to_numpy=True, show_progress_bar=False
            )
            return _l2_normalize(vecs)
        # OpenAI backend, via the official client (keeps the embed call simple;
        # the chat/vision path is what goes through LiteLLM).
        from openai import OpenAI

        client = OpenAI()
        resp = client.embeddings.create(
            model=SETTINGS.openai_text_embed_model, input=texts
        )
        vecs = np.array([d.embedding for d in resp.data], dtype="float32")
        return _l2_normalize(vecs)

    def encode_one(self, text: str) -> np.ndarray:
        return self.encode([text])[0]


# ---------------------------------------------------------------------------
# Image encoder (CLIP)
# ---------------------------------------------------------------------------
class ImageEncoder:
    """CLIP image tower for images, plus CLIP text tower for cross-modal query.

    The text tower here is used ONLY for short query strings (well under CLIP's
    77-token limit), never for document chunks, so the truncation bug cannot
    bite. Document text always goes through TextEncoder instead.
    """

    def __init__(self) -> None:
        import torch
        from transformers import CLIPModel, CLIPProcessor

        self._torch = torch
        self.model = CLIPModel.from_pretrained(SETTINGS.clip_model)
        self.processor = CLIPProcessor.from_pretrained(SETTINGS.clip_model)
        self.model.eval()
        self._dim = int(self.model.config.projection_dim)

    @property
    def dim(self) -> int:
        return self._dim

    def _projected(self, out):
        """Return the projected CLIP embedding tensor across transformers versions.

        transformers 4.x returned the projected tensor directly from
        get_image_features / get_text_features. transformers 5.x returns a
        BaseModelOutputWithPooling whose `pooler_output` holds the projected
        (projection_dim) embedding. Handle both.
        """
        if self._torch.is_tensor(out):
            return out
        pooled = getattr(out, "pooler_output", None)
        if pooled is None:
            raise TypeError(
                "CLIP feature call returned no tensor and no pooler_output"
            )
        return pooled

    def encode_images(self, images) -> np.ndarray:
        """images: list of PIL.Image (RGB). Returns L2-normalised float32."""
        if not images:
            return np.zeros((0, self.dim), dtype="float32")
        torch = self._torch
        inputs = self.processor(images=images, return_tensors="pt")
        with torch.no_grad():
            feats = self._projected(self.model.get_image_features(**inputs))
        return _l2_normalize(feats.cpu().numpy())

    def encode_query_text(self, texts: List[str]) -> np.ndarray:
        """Encode SHORT query strings with CLIP's text tower for image search."""
        if not texts:
            return np.zeros((0, self.dim), dtype="float32")
        torch = self._torch
        # max_length=77 is CLIP's hard cap; queries are short so this is fine.
        inputs = self.processor(
            text=texts,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=77,
        )
        with torch.no_grad():
            feats = self._projected(self.model.get_text_features(**inputs))
        return _l2_normalize(feats.cpu().numpy())


@functools.lru_cache(maxsize=1)
def get_text_encoder() -> TextEncoder:
    return TextEncoder()


@functools.lru_cache(maxsize=1)
def get_image_encoder() -> ImageEncoder:
    return ImageEncoder()
