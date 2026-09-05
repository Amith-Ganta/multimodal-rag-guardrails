"""Unit tests for src/index.py: empty-index short-circuits and save/load
round-trips. No encoder or LLM mocking is needed here - text_index/image_index
stay None until .build() runs, so search_text()/search_images() return []
without ever calling get_text_encoder()/get_image_encoder()."""

from __future__ import annotations

from src.index import MultimodalIndex
from src.ingest import TextChunk


def test_search_text_on_empty_index_returns_empty_list(empty_index):
    assert empty_index.search_text("anything") == []


def test_search_images_on_empty_index_returns_empty_list(empty_index):
    assert empty_index.search_images("anything") == []


def test_search_text_empty_index_does_not_touch_encoders(empty_index, monkeypatch):
    import src.index as index_module

    def _boom(*args, **kwargs):
        raise AssertionError("get_text_encoder should not be called on an empty index")

    monkeypatch.setattr(index_module, "get_text_encoder", _boom)
    assert empty_index.search_text("q") == []


def test_save_load_round_trip_empty_index(empty_index, tmp_path):
    saved_dir = empty_index.save(tmp_path)
    assert saved_dir == tmp_path
    assert (tmp_path / "payloads.json").exists()
    assert not (tmp_path / "text.faiss").exists()
    assert not (tmp_path / "image.faiss").exists()

    loaded = MultimodalIndex.load(tmp_path)
    assert loaded.text_index is None
    assert loaded.image_index is None
    assert loaded.text_payloads == []
    assert loaded.image_ids == []
    assert loaded.image_meta == []
    assert loaded.image_store == {}


def test_save_load_round_trip_with_real_text_index(tmp_path):
    faiss = __import__("faiss")

    idx = MultimodalIndex()
    chunk = TextChunk(doc_id="doc1", page=1, chunk_index=0, text="hello world")
    vec = __import__("numpy").ones((1, 8), dtype="float32")
    faiss.normalize_L2(vec)
    idx.text_index = faiss.IndexFlatIP(8)
    idx.text_index.add(vec)
    idx.text_payloads = [chunk]

    idx.save(tmp_path)
    assert (tmp_path / "text.faiss").exists()

    loaded = MultimodalIndex.load(tmp_path)
    assert loaded.text_index is not None
    assert loaded.text_index.ntotal == 1
    assert loaded.text_payloads == [chunk]
    assert loaded.image_index is None
