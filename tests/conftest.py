"""Shared fixtures for the multimodal-rag test suite."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.index import MultimodalIndex


@pytest.fixture
def empty_index() -> MultimodalIndex:
    """A freshly constructed index with no .build() call.

    search_text()/search_images() short-circuit to [] on a None/empty index,
    so this is already a valid "empty index" fixture with no FAISS or encoder
    mocking required.
    """
    return MultimodalIndex()


@pytest.fixture
def settings_override(monkeypatch):
    """Swap a module's SETTINGS singleton for a patched copy.

    Settings is a frozen dataclass, so per-attribute mutation via setattr on
    the instance fails; the correct pattern is dataclasses.replace() on the
    module's own SETTINGS reference (each module holds its own imported
    reference, e.g. src.guard.SETTINGS is not the same lookup as
    src.config.SETTINGS after this swap - it just starts out pointing at the
    same object).
    """

    def _override(module, **overrides):
        current = module.SETTINGS
        patched = replace(current, **overrides)
        monkeypatch.setattr(module, "SETTINGS", patched)
        return patched

    return _override


@pytest.fixture
def clear_openai_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture
def set_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key-not-real")


@pytest.fixture
def clear_groq_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


@pytest.fixture
def set_groq_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test-fake-key-not-real")
