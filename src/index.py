"""Two FAISS indexes, one per modality, because the dual encoder produces two
different vector spaces.

  * text index  -> TextEncoder space (e.g. 384-dim MiniLM / 1536-dim OpenAI)
  * image index -> CLIP space (512-dim)

Both use inner-product search on L2-normalised vectors, so the score is cosine
similarity in [-1, 1]. Payloads (chunk text, image ids) are kept in parallel
Python lists and saved as JSON next to the FAISS files so a rebuilt process can
answer without re-embedding.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Tuple

import numpy as np

from .config import SETTINGS
from .encoders import get_image_encoder, get_text_encoder
from .ingest import IngestResult, TextChunk, load_pil_images

_WORD_RE = re.compile(r"[a-z0-9]+")

# Common words carry no discriminative signal: nearly every chunk in an English
# document contains "the", "in", "does", etc., so without this filter every
# candidate gets roughly the same overlap score and the rerank can't tell a
# relevant chunk from an unrelated one.
_STOPWORDS = frozenset(
    """
    a an the this that these those is are was were be been being
    of to in on at for with from by as and or but not no nor
    it its it's what which who whom does do did doing have has had
    can could will would shall should may might must about into
    over under above below between out up down than then so if
    """.split()
)

# A chunk made up mostly of tokenizer padding/special tokens is degenerate:
# it can still land a high raw cosine score (padding embeds near the mean of
# the space) but has no real content, so it must never win a rerank.
_DEGENERATE_TOKEN_RE = re.compile(r"<\s*(pad|eos|bos|unk|s|/s)\s*>", re.IGNORECASE)

# The appendix figure pages (Figures 3-5) are attention-visualisation plots: a
# short caption sitting on top of a long strip of raw tokenizer output
# ("... <EOS> <pad> <pad> ..."). Two of those chunks carry only one or two
# <pad> markers after the caption, so a flat ">= 3 hits" rule lets them through
# and they then outrank real prose on any "attention"-flavoured query. Score the
# DENSITY of special tokens instead of the raw count: real prose is ~0% special
# tokens, a visualisation strip is several percent, regardless of length.
_DEGENERATE_TOKEN_RATIO = 0.02


def _is_degenerate_chunk(text: str) -> bool:
    """True when a chunk is mostly tokenizer artefacts rather than readable prose.

    Such a chunk can still land a high raw cosine score (padding embeds near the
    mean of the space) while carrying no answerable content, so it must never
    win a rerank.
    """
    hits = len(_DEGENERATE_TOKEN_RE.findall(text))
    if hits >= 3:
        return True
    words = len(_WORD_RE.findall(text.lower())) or 1
    return hits > 0 and (hits / words) >= _DEGENERATE_TOKEN_RATIO


# Terms that describe the shape of a question rather than its subject. They
# survive the stopword filter but match nearly every chunk in a research paper
# ("used", "models", "train", "paper"), so counting them makes the lexical score
# saturate: on "Which optimizer and hyperparameters were used to train the
# models?" the chunk about code availability scored the SAME as the chunk that
# names Adam. Down-weighting them lets the discriminative terms decide.
#
# "hyperparameters"/"parameters"/"settings"/"configuration" belong in this same
# bucket: they name the GENERAL category of thing a methodology section
# discusses, so almost every training-setup chunk in a paper mentions one of
# them, without saying WHICH hyperparameter. On the query above, the chunks
# about beam search and GPU/step timing both contain the literal word
# "hyperparameters" and so tied the Adam-optimizer chunk on keyword overlap
# despite not naming an optimizer at all, while also winning on raw cosine
# similarity -- letting two topically-adjacent-but-non-answering chunks outrank
# the chunk that actually names Adam/beta/epsilon.
_LOW_SALIENCE = frozenset(
    """
    used use using paper models model train trained training illustrate
    illustrates show shows shown describe describes described mechanism
    method methods approach result results work
    hyperparameter hyperparameters parameter parameters setting settings
    configuration configurations
    """.split()
)

# Weight a rare, on-topic term this many times more than a generic one.
#
# At 3.0, a query with only one truly salient word (e.g. "optimizer" in
# "Which optimizer and hyperparameters were used to train the models?")
# still lets a chunk that matches two low-salience words (e.g. "used" +
# "hyperparameters") out-overlap the chunk containing that one salient word,
# because 2 * 1.0 > 1 * 3.0. That let the beam-search chunk -- which never
# names an optimizer -- keep a higher combined score than the Adam chunk.
# 5.0 makes a single salient match worth more than any pair of generic ones
# still possible under this vocabulary, so the chunk that actually names the
# subject the query asks about wins the tie.
_SALIENT_WEIGHT = 5.0


def _keyword_overlap(query: str, text: str) -> float:
    """Weighted fraction of the query's keywords that appear in text.

    The bi-encoder embeds for semantic similarity, so a chunk that's
    topically adjacent but doesn't mention the query's specific named
    entities (e.g. "Figure 2", "optimizer") can outrank the chunk that
    actually answers the question. This lexical signal corrects for that
    without needing a second model.

    Matches are weighted, not counted. An unweighted count treats "optimizer"
    and "used" as equally informative, which flattens the signal to a constant
    across candidates and makes the rerank a no-op (or worse: it ranked the
    true Adam/beta/epsilon chunk BELOW three irrelevant ones, because that
    chunk happens to omit the filler words the others share).
    """
    query_words = set(_WORD_RE.findall(query.lower())) - _STOPWORDS
    if not query_words:
        return 0.0
    text_words = set(_WORD_RE.findall(text.lower())) - _STOPWORDS

    def weight(word: str) -> float:
        return 1.0 if word in _LOW_SALIENCE else _SALIENT_WEIGHT

    total = sum(weight(w) for w in query_words)
    matched = sum(weight(w) for w in query_words & text_words)
    return matched / total if total else 0.0


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
        # Over-fetch a wider candidate pool than we need so the keyword-overlap
        # rerank below has room to pull up a lexically-exact match that the
        # bi-encoder alone ranked outside the final top-k.
        pool = min(k * 4, len(self.text_payloads))
        scores, idxs = self.text_index.search(qv, pool)
        candidates: List[TextHit] = []
        for score, idx in zip(scores[0], idxs[0]):
            if idx < 0:
                continue
            chunk = self.text_payloads[idx]
            if _is_degenerate_chunk(chunk.text):
                continue
            candidates.append(TextHit(chunk=chunk, score=float(score)))
        if not candidates:
            return []

        def combined(hit: TextHit) -> float:
            # Cosine similarity is in [-1, 1]; keyword overlap in [0, 1].
            # At the old 0.2 weight the lexical term spanned at most 0.2 while
            # observed cosine gaps between the right chunk and a wrong one ran
            # to ~0.15-0.2, so a decisive lexical match still could not move a
            # chunk past a merely topical one. 0.35 lets an exact match on the
            # query's salient terms overturn a modest embedding deficit while
            # still leaving cosine the dominant term.
            return hit.score + 0.35 * _keyword_overlap(query, hit.chunk.text)

        candidates.sort(key=combined, reverse=True)
        return candidates[:k]

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
        with open(directory / "payloads.json", "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "text_payloads": [asdict(c) for c in self.text_payloads],
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
        with open(directory / "payloads.json", "r", encoding="utf-8") as fh:
            data = json.load(fh)
        obj.text_payloads = [TextChunk(**c) for c in data["text_payloads"]]
        obj.image_ids = data["image_ids"]
        obj.image_meta = [tuple(m) for m in data["image_meta"]]
        obj.image_store = data["image_store"]
        return obj
