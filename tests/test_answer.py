"""Unit tests for src/answer.py.

build_vision_messages() is pure and needs no mocking. answer_query() calls
get_gateway().complete_verbose(...), so that is mocked here to avoid any real
LLM call - the assertion that matters is that Answer.model_served comes from
THIS call's CallRecord, not some other source (the source comment explicitly
warns against reading call_log[-1] under concurrency).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import src.answer as answer_module
from src.answer import Answer, RetrievedContext, answer_query, build_vision_messages
from src.gateway import CallRecord
from src.index import ImageHit, TextHit
from src.ingest import TextChunk


def _text_hit(text: str) -> TextHit:
    chunk = TextChunk(doc_id="doc1", page=1, chunk_index=0, text=text)
    return TextHit(chunk=chunk, score=0.9)


def _image_hit(image_id: str) -> ImageHit:
    return ImageHit(image_id=image_id, doc_id="doc1", page=2, score=0.8)


def test_build_vision_messages_with_no_context():
    context = RetrievedContext()
    messages = build_vision_messages("what is this?", MagicMock(image_store={}), context)

    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    content = messages[1]["content"]
    assert content[0]["type"] == "text"
    assert "(no text retrieved)" in content[0]["text"]
    assert content[1]["type"] == "text"
    assert "what is this?" in content[1]["text"]
    assert len(content) == 2  # no image blocks appended


def test_build_vision_messages_includes_text_passages():
    context = RetrievedContext(text_hits=[_text_hit("revenue grew 10%")])
    messages = build_vision_messages("summary?", MagicMock(image_store={}), context)
    content = messages[1]["content"]
    assert "revenue grew 10%" in content[0]["text"]


def test_build_vision_messages_appends_image_block_when_store_has_data():
    context = RetrievedContext(image_hits=[_image_hit("img1")])
    index = MagicMock()
    index.image_store = {"img1": "ZmFrZWJhc2U2NA=="}

    messages = build_vision_messages("what's in the chart?", index, context)
    content = messages[1]["content"]
    image_blocks = [b for b in content if b["type"] == "image_url"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["image_url"]["url"] == "data:image/png;base64,ZmFrZWJhc2U2NA=="


def test_build_vision_messages_skips_image_missing_from_store():
    context = RetrievedContext(image_hits=[_image_hit("missing_img")])
    index = MagicMock()
    index.image_store = {}

    messages = build_vision_messages("q", index, context)
    content = messages[1]["content"]
    image_blocks = [b for b in content if b["type"] == "image_url"]
    assert image_blocks == []


def test_answer_query_uses_model_served_from_this_calls_record(monkeypatch):
    index = MagicMock()
    index.search_text.return_value = [_text_hit("some fact")]
    index.search_images.return_value = []
    index.image_store = {}

    record = CallRecord(
        model_requested="gpt-4o",
        model_served="gpt-4o-2024-08-06",
        prompt_tokens=10,
        completion_tokens=5,
        latency_sec=0.5,
        cost_usd=0.001,
    )
    fake_gateway = MagicMock()
    fake_gateway.complete_verbose.return_value = ("the answer text", record)
    monkeypatch.setattr(answer_module, "get_gateway", lambda: fake_gateway)

    result = answer_query(index, "what happened?", tag="unit-test")

    assert isinstance(result, Answer)
    assert result.text == "the answer text"
    assert result.model_served == "gpt-4o-2024-08-06"
    assert result.question == "what happened?"
    fake_gateway.complete_verbose.assert_called_once()
    _, kwargs = fake_gateway.complete_verbose.call_args
    assert kwargs["tag"] == "unit-test"


def test_answer_query_reuses_precomputed_context(monkeypatch):
    index = MagicMock()
    record = CallRecord(
        model_requested="gpt-4o",
        model_served="gpt-4o",
        prompt_tokens=1,
        completion_tokens=1,
        latency_sec=0.1,
        cost_usd=0.0,
    )
    fake_gateway = MagicMock()
    fake_gateway.complete_verbose.return_value = ("answer", record)
    monkeypatch.setattr(answer_module, "get_gateway", lambda: fake_gateway)

    precomputed = RetrievedContext(text_hits=[_text_hit("preloaded")])
    answer_query(index, "q", context=precomputed)

    index.search_text.assert_not_called()
    index.search_images.assert_not_called()
