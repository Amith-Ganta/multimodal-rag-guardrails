"""Run every eval dimension programmatically and write a stamped score snapshot.

The pytest gates give a per-case pass/fail. This does something they cannot: it
records the ACTUAL per-metric scores so a later run can be compared against a
blessed baseline (scripts/eval_compare.py). A metric that still passes but has
slid from 0.91 to 0.72 is invisible to a gate and obvious to a snapshot diff.

What it does
------------
1. Loads the prebuilt index once.
2. Runs the answer, retriever, and leakage dimensions through
   evals/harness.py (same cases, same metrics, same length-robust judge as the
   gates).
3. Stamps the result with the git SHA, a hash of the live SYSTEM_PROMPT, the
   judge model, and a timestamp, so a snapshot is traceable to the exact code +
   prompt that produced it.
4. Writes JSON to evals/snapshots/ (default) or to --out. With --baseline it
   also updates evals/snapshots/baseline.json, the file compare.py diffs against.

Timestamps come from the OS clock here (this is a plain script, not a DeepEval
workflow), so there is no restriction on reading the clock.

Judge: gpt-4o-mini (SETTINGS.eval_judge_model) with the Groq gpt-oss-20b
fallback, so OPENAI_API_KEY must be set. With no key the script exits 3 WITHOUT
writing a snapshot, so an empty run can never be mistaken for a clean baseline.
If a key IS set but every dimension still errors (deepeval not importable from
this interpreter, index missing, etc.) the script exits 5 and again writes
nothing, for the same reason.

Exit codes: 0 ok, 3 no judge key, 4 index could not load, 5 all dimensions
errored (no real score produced). Only exit 0 writes a snapshot.

Usage:
    cd AI-JOB-Search-Project-3
    python scripts/eval_snapshot.py                 # timestamped candidate snapshot
    python scripts/eval_snapshot.py --baseline      # also bless it as the baseline
    python scripts/eval_snapshot.py --out foo.json  # explicit path
    python scripts/eval_snapshot.py --dims retriever leakage
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

# Make the project root importable when run as a bare script.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals import harness  # noqa: E402
from src.config import ROOT as CFG_ROOT  # noqa: E402
from src.config import SETTINGS  # noqa: E402
from src.index import MultimodalIndex  # noqa: E402

SNAP_DIR = Path(CFG_ROOT) / "evals" / "snapshots"
BASELINE_PATH = SNAP_DIR / "baseline.json"

EXIT_OK = 0
EXIT_NO_KEY = 3
EXIT_ERROR = 4
EXIT_ALL_FAILED = 5


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _git_dirty() -> bool:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return bool(out.stdout.strip())
    except Exception:
        return False


def _prompt_hash() -> str:
    """Short hash of the live SYSTEM_PROMPT.

    A prompt edit changes the numbers, so the snapshot records which prompt it
    measured. compare.py surfaces a mismatch as a reason to re-bless rather than
    trust a stale baseline.
    """
    try:
        from src.answer import SYSTEM_PROMPT

        return hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12]
    except Exception:
        return "unknown"


def _timestamp() -> str:
    # Import here so the module still imports where datetime.now is restricted.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(dims: list[str], out: Path | None, bless: bool) -> int:
    if not SETTINGS.has_openai_key:
        print(
            "OPENAI_API_KEY not set; the DeepEval judge (gpt-4o-mini) cannot run. "
            "No snapshot written (exit 3).",
            file=sys.stderr,
        )
        return EXIT_NO_KEY

    try:
        index = MultimodalIndex.load()
    except Exception as exc:  # index missing / unreadable
        print(
            f"Could not load the index ({exc!r}). Build it first with "
            "scripts/build_index.py. No snapshot written (exit 4).",
            file=sys.stderr,
        )
        return EXIT_ERROR

    metrics: dict[str, dict] = {}
    for dim in dims:
        runner = harness.DIMENSIONS.get(dim)
        if runner is None:
            print(f"Unknown dimension {dim!r}; skipping.", file=sys.stderr)
            continue
        print(f"[snapshot] running dimension: {dim} ...", file=sys.stderr)
        try:
            metrics.update(runner(index))
        except Exception as exc:
            print(
                f"[snapshot] dimension {dim!r} failed: {exc!r}. "
                "Recording it as an error entry, not a passing score.",
                file=sys.stderr,
            )
            metrics[f"{dim}._error"] = {
                "metric_id": f"{dim}._error",
                "n": 0,
                "min": None,
                "mean": None,
                "pass_rate": None,
                "threshold": None,
                "higher_is_better": True,
                "error": repr(exc),
            }

    # A snapshot is only worth writing if at least one metric produced a real
    # score. If every dimension errored (a broken import, a missing index, a
    # wrong interpreter), the metrics map holds nothing but "_error" entries.
    # Writing that -- and worse, blessing it as a baseline -- would let an empty
    # run masquerade as a clean baseline, which is exactly what the missing-key
    # guard above exists to prevent. Refuse it here too.
    real_scores = [
        m for mid, m in metrics.items()
        if not mid.endswith("._error") and m.get("mean") is not None
    ]
    if not real_scores:
        errored = sorted(mid for mid in metrics if mid.endswith("._error"))
        print(
            "[snapshot] every dimension failed to produce a score "
            f"({', '.join(errored) or 'no metrics at all'}). "
            "No snapshot written and nothing blessed (exit 5). "
            "Check that deepeval is importable from THIS interpreter and that "
            "the index is built.",
            file=sys.stderr,
        )
        return EXIT_ALL_FAILED

    snapshot = {
        "schema": "multimodal-rag-eval-snapshot/v1",
        "created_utc": _timestamp(),
        "git_sha": _git_sha(),
        "git_dirty": _git_dirty(),
        "prompt_hash": _prompt_hash(),
        "judge_model": SETTINGS.eval_judge_model,
        "fallback_judge_model": SETTINGS.eval_fallback_judge_model,
        "dimensions": dims,
        "metrics": metrics,
    }

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    if out is None:
        stamp = snapshot["created_utc"].replace(":", "").replace("-", "")
        out = SNAP_DIR / f"snapshot-{stamp}-{snapshot['git_sha']}.json"
    out.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[snapshot] wrote {out}")

    if bless:
        BASELINE_PATH.write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[snapshot] blessed as baseline -> {BASELINE_PATH}")

    # Human-readable one-line-per-metric summary to stderr.
    for mid, m in sorted(metrics.items()):
        mean = m.get("mean")
        pr = m.get("pass_rate")
        thr = m.get("threshold")
        print(
            f"    {mid:42s} mean={mean if mean is not None else 'NA':<6} "
            f"pass_rate={pr if pr is not None else 'NA':<5} thr={thr}",
            file=sys.stderr,
        )
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dims",
        nargs="+",
        default=list(harness.DIMENSIONS.keys()),
        choices=list(harness.DIMENSIONS.keys()),
        help="Which dimensions to snapshot (default: all).",
    )
    ap.add_argument("--out", type=Path, default=None, help="Explicit output path.")
    ap.add_argument(
        "--baseline",
        action="store_true",
        help="Also write evals/snapshots/baseline.json (bless this run).",
    )
    args = ap.parse_args()
    return run(args.dims, args.out, args.baseline)


if __name__ == "__main__":
    raise SystemExit(main())
