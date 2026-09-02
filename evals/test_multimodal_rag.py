"""Offline DeepEval gate for the multimodal RAG pipeline.

Grounded against the installed DeepEval (4.2.0). Verified symbols:
  AnswerRelevancyMetric, FaithfulnessMetric, GEval, LLMTestCase,
  LLMTestCaseParams, assert_test  -- all import in this venv.

Run:
    cd AI-JOB-Search-Project-3
    deepeval test run evals/test_multimodal_rag.py

Judge model: gpt-4o-mini (SETTINGS.eval_judge_model). This calls OpenAI, so
OPENAI_API_KEY must be set. The metric constructors build the judge client
eagerly, so with no key present the suite is SKIPPED (not silently passed).

What each metric maps to:
  * AnswerRelevancyMetric  -> "did the answer actually address the question?"
  * FaithfulnessMetric     -> "is every claim grounded in the retrieved context?"
                              (the strongest anti-hallucination signal here)
  * GEval "Correctness"    -> "does the answer contain the expected facts?"
                              criteria derived from each golden's expected_facts.

Scores are probabilistic evidence with reasons attached, not proof. Thresholds
are explicit below and come from SETTINGS.eval_threshold.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.answer import answer_query
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

# The judge's structured-verdict calls (FaithfulnessMetric especially) can run
# long: one verdict object per extracted claim, each with a free-text reason.
# On some goldens (g1 here) that verdict list plus its reasons overruns
# gpt-4o-mini's HARD 16384-completion-token ceiling, and OpenAI's parse helper
# raises openai.LengthFinishReasonError before DeepEval sees a result. That kills
# the metric's async task, leaves DeepEval's test-run object as None, and
# surfaces at teardown as "AttributeError: 'NoneType' object has no attribute
# 'test_cases_lookup_map'" -- reddening EVERY test, not just the one that
# overran. Raising max_tokens cannot fix this: 16384 is the model ceiling, not a
# default. So we request that ceiling AND wrap the judge so a length overrun
# retries once compactly (verdicts without the unscored reason strings), which
# preserves the real yes/no/idk score while fitting the budget. See
# evals/robust_judge.py for the full rationale.
_JUDGE_MAX_TOKENS = 16384


def _judge():
    """Build the length-robust judge model.

    Returns a RobustOpenAIModel (a thin OpenAIModel subclass) so a truncated
    Faithfulness verdict degrades to a compact retry instead of crashing the
    whole gate. Falls back to the plain model-name string if this DeepEval build
    does not expose the model class, so the gate still runs instead of erroring
    on import.
    """
    try:
        from evals.robust_judge import RobustOpenAIModel

        return RobustOpenAIModel(
            model=JUDGE_MODEL,
            generation_kwargs={"max_tokens": _JUDGE_MAX_TOKENS},
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
    idx = MultimodalIndex.load()
    return idx


@pytest.mark.parametrize("golden", _load_goldens(), ids=lambda g: g["id"])
def test_golden_answer(index, golden):
    from deepeval import assert_test
    from deepeval.metrics import (
        AnswerRelevancyMetric,
        FaithfulnessMetric,
        GEval,
    )
    from deepeval.test_case import LLMTestCase, LLMTestCaseParams

    question = golden["question"]
    ans = answer_query(index, question, tag="eval")
    retrieval_context = ans.context.as_retrieval_context()

    case = LLMTestCase(
        input=question,
        actual_output=ans.text,
        expected_output=golden.get("expected_output"),
        retrieval_context=retrieval_context or None,
    )

    judge = _judge()  # judge with the raised max_tokens cap (or name fallback)

    facts = ", ".join(golden.get("expected_facts", []))
    correctness = GEval(
        name="Correctness",
        criteria=(
            "Determine whether the actual output is factually correct for the "
            "question and includes these expected facts: "
            f"{facts}. Minor wording differences are fine; missing or "
            "contradicting a key fact should lower the score."
        ),
        evaluation_params=[
            LLMTestCaseParams.INPUT,
            LLMTestCaseParams.ACTUAL_OUTPUT,
            LLMTestCaseParams.EXPECTED_OUTPUT,
        ],
        threshold=0.5,
        model=judge,
    )
    relevancy = AnswerRelevancyMetric(
        threshold=THRESHOLD, model=judge, include_reason=True
    )
    metrics = [correctness, relevancy]
    if retrieval_context:
        metrics.append(
            FaithfulnessMetric(
                threshold=THRESHOLD, model=judge, include_reason=True
            )
        )

    assert_test(case, metrics)
