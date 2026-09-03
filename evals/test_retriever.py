"""Offline DeepEval gate for the RETRIEVER, judged on its own.

Grounded against the installed DeepEval (4.2.0) in this project's venv. Verified
symbols: ContextualRecallMetric, ContextualPrecisionMetric, LLMTestCase,
assert_test -- all import and construct in this venv.

Why a separate retriever gate
-----------------------------
The end-to-end gate in test_multimodal_rag.py scores the FINAL answer. When it
regresses, it cannot tell you WHERE: a retrieval miss (the right passage was
never fetched) and a generation miss (the passage was fetched but the model
ignored it) both look the same there. This module isolates the retrieval step so
the two failure modes separate cleanly.

It scores the retrieved text context against each golden, using no generation at
all:
  * ContextualRecallMetric    -> "did retrieval fetch the passages needed to
                                 support the expected answer?" (misses = recall drop)
  * ContextualPrecisionMetric -> "are the relevant passages ranked above the
                                 irrelevant ones?" (ranking quality)

Both are LLM-judged: they compare the retrieved passages against the golden's
expected_output. Judge model: gpt-4o-mini (SETTINGS.eval_judge_model), so
OPENAI_API_KEY must be set. The metric constructors build the judge client
eagerly, so with no key present the suite is SKIPPED (not silently passed) --
exactly like the answer gate.

Run:
    cd AI-JOB-Search-Project-3
    deepeval test run evals/test_retriever.py

Scores are probabilistic evidence with reasons attached, not proof. Thresholds
are explicit below and come from SETTINGS.eval_threshold.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.answer import retrieve
from src.config import GOLDENS_DIR, SETTINGS
from src.index import MultimodalIndex

# --- guard: skip the whole module cleanly when the judge key is absent -------
_HAS_KEY = bool(os.environ.get("OPENAI_API_KEY"))
pytestmark = pytest.mark.skipif(
    not _HAS_KEY,
    reason="OPENAI_API_KEY not set; DeepEval judge (gpt-4o-mini) cannot run.",
)

JUDGE_MODEL = SETTINGS.eval_judge_model  # "gpt-4o-mini"
THRESHOLD = SETTINGS.eval_threshold  # 0.7


def _judge():
    """Length-robust judge, matching the answer gate. Falls back to the plain
    model-name string if the model class is unavailable in this build."""
    try:
        from evals.robust_judge import RobustOpenAIModel

        return RobustOpenAIModel(
            model=JUDGE_MODEL,
            generation_kwargs={"max_tokens": 16384},
        )
    except Exception:
        return JUDGE_MODEL


def _load_goldens() -> list[dict]:
    path = Path(GOLDENS_DIR) / "multimodal_goldens.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["goldens"]


@pytest.fixture(scope="module")
def index() -> MultimodalIndex:
    """Load the prebuilt index. Build it first with scripts/build_index.py."""
    return MultimodalIndex.load()


@pytest.mark.parametrize("golden", _load_goldens(), ids=lambda g: g["id"])
def test_retrieval_quality(index, golden):
    from deepeval import assert_test
    from deepeval.metrics import (
        ContextualPrecisionMetric,
        ContextualRecallMetric,
    )
    from deepeval.test_case import LLMTestCase

    question = golden["question"]
    # Retrieve only. No gateway / LLM call happens here: we grade what the
    # retriever fetched, independent of how the model later used it.
    ctx = retrieve(index, question)
    retrieval_context = ctx.as_retrieval_context()

    # ContextualRecall/Precision need the expected answer to judge whether the
    # retrieved passages cover and rank the information it depends on.
    case = LLMTestCase(
        input=question,
        # actual_output is required by LLMTestCase; the contextual metrics score
        # retrieval_context vs expected_output, not this field. We pass the
        # expected answer so the case is well-formed without inventing an output.
        actual_output=golden.get("expected_output", ""),
        expected_output=golden.get("expected_output"),
        retrieval_context=retrieval_context or None,
    )

    if not retrieval_context:
        pytest.fail(
            f"Retriever returned no context for golden {golden['id']!r}; "
            "recall/precision cannot be judged. Rebuild the index."
        )

    judge = _judge()
    metrics = [
        ContextualRecallMetric(
            threshold=THRESHOLD, model=judge, include_reason=True
        ),
        ContextualPrecisionMetric(
            threshold=THRESHOLD, model=judge, include_reason=True
        ),
    ]
    assert_test(case, metrics)
