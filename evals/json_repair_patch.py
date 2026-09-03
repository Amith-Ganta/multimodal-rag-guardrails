r"""Make DeepEval's judge-output parser survive one specific malformed-JSON case.

Why this exists
---------------
DeepEval's LLM-judged metrics ask the judge model to return a JSON object and
then parse it with `deepeval.metrics.utils.trimAndLoadJson`. GEval in particular
parses the raw judge content ITSELF (deepeval/metrics/g_eval/g_eval.py: `data =
trimAndLoadJson(res.choices[0].message.content, self)`), so a malformed string
raises from INSIDE DeepEval, on a path a custom judge-model wrapper never sees.

`trimAndLoadJson` already repairs one class of judge sloppiness (a trailing comma
before a closing bracket) but not another: an UNESCAPED BACKSLASH inside a string
value. gpt-4o-mini, writing a free-text `reason`, sometimes emits a stray
backslash (e.g. it writes about "\epsilon", or "\alpha", or a Windows-style path)
without doubling it. That is invalid JSON per the spec, and Python's json raises

    json.decoder.JSONDecodeError: Invalid \escape: line 3 column 109 (char 124)

which DeepEval turns into

    ValueError: Evaluation LLM outputted an invalid JSON. ...

For at least one multimodal golden (the Adam-optimizer / epsilon answer) this is
NOT transient: the judge reproduces the same unescaped backslash on every sample,
so a plain rerun cannot clear it. The verdict itself is fine -- correctness score
8/10 with a sensible reason -- only its SERIALISATION is malformed.

What this patch does
--------------------
It wraps `trimAndLoadJson` with ONE extra, last-resort repair that runs ONLY
after DeepEval's own parse and its trailing-comma retry have both failed: escape
any backslash that is not already part of a valid JSON escape (\" \\ \/ \b \f \n
\r \t \uXXXX), then parse again. If that still fails, it defers to the original
function so the "invalid JSON" error is raised exactly as before -- nothing is
hidden.

What this patch does NOT do
---------------------------
It does not read, alter, coerce, clamp, or fabricate any score, verdict, or
reason. It only makes a syntactically-broken JSON string parseable so the judge's
REAL verdict can be read. A genuinely wrong or unfaithful answer still fails its
metric on the judge's own score. This repairs the transport layer, not the
grade.

Install by importing this module before any DeepEval metric runs (the gate test
files do this at import time).
"""

from __future__ import annotations

import json
import re

import deepeval.metrics.utils as _utils

# Grab the real implementation once, so the wrapper can always fall back to
# DeepEval's exact original behaviour (including its own trailing-comma retry and
# its precise error message / metric.error side effect).
_ORIGINAL_TRIM_AND_LOAD = _utils.trimAndLoadJson

# A backslash that is NOT the start of a valid JSON escape sequence. Valid
# escapes are: \" \\ \/ \b \f \n \r \t and \uXXXX. Anything else after a
# backslash means the backslash was meant literally and must be doubled.
_INVALID_ESCAPE = re.compile(r'\\(?![\\"/bfnrtu])')


def _escape_stray_backslashes(s: str) -> str:
    """Double every backslash that is not part of a valid JSON escape."""
    return _INVALID_ESCAPE.sub(r"\\\\", s)


def _trim_and_load_json_with_escape_repair(input_string, metric=None):
    """trimAndLoadJson + one last-resort invalid-\\escape repair.

    Order of attempts:
      1. DeepEval's original trimAndLoadJson (direct parse, then its own
         trailing-comma repair). If it succeeds, we return its result verbatim.
      2. Only if that raised ValueError("...invalid JSON..."), retry once after
         escaping stray backslashes in the extracted JSON object.
      3. If the repaired parse also fails, re-run the ORIGINAL so the exact same
         error and metric.error side effect surface, unchanged.
    """
    try:
        return _ORIGINAL_TRIM_AND_LOAD(input_string, metric)
    except ValueError:
        if not isinstance(input_string, str):
            raise  # None etc. -- nothing to repair; keep original behaviour.

        start = input_string.find("{")
        end = input_string.rfind("}") + 1
        json_str = (
            input_string[start:end] if start != -1 and end != 0 else ""
        )
        repaired = _escape_stray_backslashes(json_str)
        # Also strip a trailing comma, mirroring the original's own retry, so the
        # two repairs compose instead of one masking the other.
        repaired = re.sub(r",\s*([\]}])", r"\1", repaired)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            # Repair did not help. Defer to the original so the caller sees the
            # identical error and metric.error it would have seen without us.
            return _ORIGINAL_TRIM_AND_LOAD(input_string, metric)


def install() -> None:
    """Rebind trimAndLoadJson everywhere DeepEval already imported it.

    `deepeval.metrics.g_eval.g_eval` (and other metric modules) do
    `from ..utils import trimAndLoadJson`, binding their OWN module-level name to
    the original function object. Patching only `utils` would miss those, so we
    walk every already-imported deepeval submodule and rebind any attribute that
    still points at the original.
    """
    import sys

    _utils.trimAndLoadJson = _trim_and_load_json_with_escape_repair
    for name, module in list(sys.modules.items()):
        if not name.startswith("deepeval") or module is None:
            continue
        if getattr(module, "trimAndLoadJson", None) is _ORIGINAL_TRIM_AND_LOAD:
            module.trimAndLoadJson = _trim_and_load_json_with_escape_repair


# Install on import so a simple `import evals.json_repair_patch` is enough.
install()
