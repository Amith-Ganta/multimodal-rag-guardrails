"""A DeepEval judge model that survives the gpt-4o-mini output ceiling.

Why this exists
---------------
The FaithfulnessMetric asks the judge for a structured object: one verdict per
extracted claim, each optionally carrying a free-text ``reason``. DeepEval sends
this through ``client.beta.chat.completions.parse(...)``. On some goldens the
response hits gpt-4o-mini's HARD 16384-completion-token ceiling; OpenAI's parse
helper then raises ``openai.LengthFinishReasonError`` before DeepEval sees any
result. That exception kills the metric's async task, which leaves DeepEval's
global test-run object as ``None`` and surfaces at teardown as
``AttributeError: 'NoneType' object has no attribute 'test_cases_lookup_map'`` --
reddening EVERY test in the run, not just the one that overran.

Golden g1 is the concrete case. Its Correctness (0.94) and Relevancy (1.0) both
pass, so the answer itself is sound. But the faithfulness verdict call makes
gpt-4o-mini run all the way to 16384 completion tokens from only a ~640-token
prompt: the model degenerates into a repetition loop under this specific
structured-output request, not a genuinely huge verdict list. Raising
``max_tokens`` cannot fix that (16384 is the model ceiling), and dropping the
reasons is not enough either, because the loop fills the budget regardless.

The two-stage fix, and why it stays honest
------------------------------------------
FaithfulnessMetric._calculate_score reads ONLY ``verdict.verdict`` (the
yes/no/idk decision). The ``reason`` string is never used in scoring -- it only
appears in the verbose log.

1. Compact retry (same model). On a length overrun we first retry ONCE on
   gpt-4o-mini, appending an instruction to return verdicts WITHOUT reasons.
   When the overflow was driven by verbose reasons this alone fits the budget
   and preserves the exact yes/no/idk decisions that produce the real score.

2. Larger-output fallback judge. When even the compact retry overruns -- the
   gpt-4o-mini repetition loop, which no prompt tweak escapes -- we delegate that
   ONE schema call to a fallback judge with a much larger output budget
   (Groq ``openai/gpt-oss-120b`` by default, already this project's gateway
   fallback provider). It speaks the same OpenAI-compatible structured-output
   API, so it returns the same ``Verdicts`` object DeepEval expects and the
   metric scores normally. Every other verdict in the run still comes from
   gpt-4o-mini; only the call that gpt-4o-mini physically cannot complete is
   re-judged, and by a real model reading the same claims against the same
   context. No score is fabricated.

If no fallback judge is configured, or the fallback also fails, we raise a clear,
typed error so the metric fails visibly and honestly, rather than letting an
opaque teardown AttributeError take the whole gate down with it.
"""

from __future__ import annotations

import os

from deepeval.models import OpenAIModel

try:  # openai is a hard dependency of the OpenAIModel judge; import defensively
    from openai import LengthFinishReasonError
except Exception:  # pragma: no cover - only if the SDK layout changes
    LengthFinishReasonError = None  # type: ignore[assignment]


# Appended to the judge prompt on a length-overrun retry. It asks for the
# scoring-relevant field only (the verdict) and explicitly drops the free-text
# reasons that caused the overflow.
_COMPACT_INSTRUCTION = (
    "\n\nIMPORTANT: Your previous response was too long and was truncated. "
    "Return ONLY the verdict for each claim (yes / no / idk). Leave every "
    "reason field empty or omit it entirely. Do not add any explanation, "
    "preamble, or commentary. Keep the JSON as short as possible."
)


def _augment_prompt(prompt, extra: str):
    """Append ``extra`` to a prompt that may be a str or a chat-style list."""
    if isinstance(prompt, str):
        return prompt + extra
    # DeepEval passes plain strings here, but be defensive about list prompts.
    if isinstance(prompt, list):
        augmented = list(prompt)
        augmented.append({"type": "text", "text": extra})
        return augmented
    return prompt


def build_fallback_judge():
    """Build the larger-output fallback judge, or return None if unconfigured.

    Reads the model, base URL, and key-env-var name from SETTINGS. The key is
    read from the named environment variable only (never hardcoded, never
    logged). Returns None when the key is absent so the gate degrades to the
    clear RuntimeError instead of erroring on a missing credential.
    """
    try:
        from src.config import SETTINGS
    except Exception:
        return None

    key_env = SETTINGS.eval_fallback_judge_key_env
    api_key = os.getenv(key_env)
    if not api_key:
        return None

    try:
        return OpenAIModel(
            model=SETTINGS.eval_fallback_judge_model,
            base_url=SETTINGS.eval_fallback_judge_base_url,
            api_key=api_key,
            temperature=0,
        )
    except Exception:
        return None


class RobustOpenAIModel(OpenAIModel):
    """OpenAIModel that rescues a truncated structured verdict.

    Behaviour is identical to ``OpenAIModel`` for every call that fits inside the
    model's output budget. Only a ``LengthFinishReasonError`` on a schema call is
    handled, in two stages: a compact retry on the same model (verdicts without
    reasons), then, if that also overruns, one delegation to a larger-output
    fallback judge. Everything else -- other exceptions, non-schema calls, the
    returned objects -- is left exactly as the base class produces it.
    """

    def __init__(self, *args, fallback_judge=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Resolve the fallback lazily/once. Stored on the instance so a single
        # judge object reuses one fallback client across calls.
        if fallback_judge is None:
            fallback_judge = build_fallback_judge()
        self._fallback_judge = fallback_judge

    def _is_length_error(self, exc: Exception) -> bool:
        if LengthFinishReasonError is not None and isinstance(
            exc, LengthFinishReasonError
        ):
            return True
        # Fallback match if the SDK symbol could not be imported.
        return exc.__class__.__name__ == "LengthFinishReasonError"

    def _ceiling_error(self, cause: Exception) -> RuntimeError:
        return RuntimeError(
            "Judge verdict exceeded the model output ceiling even after a "
            "compact retry, and no larger-output fallback judge was available "
            f"(primary judge: {self.name}). Set EVAL_FALLBACK_JUDGE_* and its "
            "API key, or use a primary judge with a larger output budget for "
            "this golden."
        )

    def generate(self, prompt, schema=None):
        try:
            return super().generate(prompt, schema=schema)
        except Exception as exc:  # noqa: BLE001 - re-raised unless a length overrun
            if schema is None or not self._is_length_error(exc):
                raise
            compact = _augment_prompt(prompt, _COMPACT_INSTRUCTION)
            try:
                return super().generate(compact, schema=schema)
            except Exception as exc2:  # noqa: BLE001
                if not self._is_length_error(exc2):
                    raise
                if self._fallback_judge is None:
                    raise self._ceiling_error(exc2) from exc2
                # Delegate this one call to the larger-output judge. Use the
                # original (reason-bearing) prompt: the fallback has the budget,
                # so it can return full verdicts.
                result = self._fallback_judge.generate(prompt, schema=schema)
                return result

    async def a_generate(self, prompt, schema=None):
        try:
            return await super().a_generate(prompt, schema=schema)
        except Exception as exc:  # noqa: BLE001
            if schema is None or not self._is_length_error(exc):
                raise
            compact = _augment_prompt(prompt, _COMPACT_INSTRUCTION)
            try:
                return await super().a_generate(compact, schema=schema)
            except Exception as exc2:  # noqa: BLE001
                if not self._is_length_error(exc2):
                    raise
                if self._fallback_judge is None:
                    raise self._ceiling_error(exc2) from exc2
                result = await self._fallback_judge.a_generate(
                    prompt, schema=schema
                )
                return result
