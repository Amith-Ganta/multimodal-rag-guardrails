"""Compare a candidate eval snapshot against a blessed baseline, and gate on it.

This is the regression layer the gates cannot provide. A pytest gate answers
"does each answer clear the bar RIGHT NOW?" This answers a different question:
"did anything get WORSE since the baseline, and is that drop a hard fail or just
worth a human look?"

How a metric is judged
----------------------
Every metric here is higher-is-better (DeepEval convention, PII leakage included:
a high PII score means the answer is safe). For each metric present in both
snapshots we look at two things and take the worse verdict:

  1. Absolute floor. If the candidate mean has fallen BELOW its own threshold,
     that is a hard regression regardless of the baseline. The metric is no
     longer passing.
  2. Drop vs baseline. delta = candidate.mean - baseline.mean.
       delta >= -info_tol            -> PASS  (flat or improved, within noise)
       -fail_tol < delta < -info_tol -> REVIEW (a real but sub-critical slide)
       delta <= -fail_tol            -> FAIL  (a large drop; block)

Decision class per metric (mirrors the campusx metric-registry idea, kept small):
  * gate      -> a FAIL or a below-floor result blocks (exit 1).
  * guardrail -> same as gate; used for the safety metrics (leakage/PII) so a
                 slide there is never merely informational.
  * info      -> never blocks on its own; a drop is reported but downgraded to
                 REVIEW at worst. Use for metrics you track but do not yet trust
                 as a hard bar.

Metrics NEW in the candidate (not in the baseline) are reported and, if below
their own floor, treated as a gate FAIL, since a brand-new failing metric should
not slip in unnoticed. Metrics MISSING from the candidate (in baseline, absent
now) are surfaced as REVIEW: maybe intentional, maybe a broken run.

Exit codes: 0 PASS, 1 FAIL (>=1 blocking regression), 2 REVIEW (slides worth a
look but nothing blocking), 3 could not run (missing or unreadable snapshot).

A prompt_hash or git-dirty mismatch does not by itself change the verdict, but it
is printed prominently: comparing across a prompt change means the baseline may
need re-blessing rather than the code being at fault.

Usage:
    python scripts/eval_compare.py CANDIDATE.json                  # vs baseline.json
    python scripts/eval_compare.py CANDIDATE.json --baseline B.json
    python scripts/eval_compare.py --candidate C.json --json       # machine-readable
    python scripts/eval_compare.py C.json --fail-tol 0.15 --info-tol 0.02
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "evals" / "snapshots" / "baseline.json"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_REVIEW = 2
EXIT_CANNOT_RUN = 3

# Metrics whose slide is a safety concern: never downgraded to info.
GUARDRAIL_PREFIXES = ("leakage.",)
# Metrics tracked but not yet trusted as a hard bar. Empty for now; every metric
# defaults to "gate". Listed here so the knob is visible and easy to adjust.
INFO_METRIC_IDS: set[str] = set()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _decision_class(metric_id: str) -> str:
    if metric_id in INFO_METRIC_IDS:
        return "info"
    if any(metric_id.startswith(p) for p in GUARDRAIL_PREFIXES):
        return "guardrail"
    return "gate"


def _verdict_for(
    metric_id: str,
    base: dict | None,
    cand: dict | None,
    fail_tol: float,
    info_tol: float,
) -> dict:
    """Return {metric_id, verdict, blocking, delta, note, ...} for one metric."""
    cls = _decision_class(metric_id)

    # Missing from candidate: present in baseline, gone now.
    if cand is None:
        return {
            "metric_id": metric_id,
            "class": cls,
            "verdict": "REVIEW",
            "blocking": False,
            "delta": None,
            "note": "metric missing from candidate run (baseline had it)",
        }

    cand_mean = cand.get("mean")
    thr = cand.get("threshold")

    # No usable score (n=0 or an error entry): cannot judge -> REVIEW.
    if cand_mean is None:
        return {
            "metric_id": metric_id,
            "class": cls,
            "verdict": "REVIEW",
            "blocking": False,
            "delta": None,
            "note": cand.get("error", "candidate produced no score for this metric"),
        }

    below_floor = thr is not None and cand_mean < thr

    # New metric: not in baseline. Judge on its own floor only.
    if base is None or base.get("mean") is None:
        if below_floor:
            blocking = cls in ("gate", "guardrail")
            return {
                "metric_id": metric_id,
                "class": cls,
                "verdict": "FAIL" if blocking else "REVIEW",
                "blocking": blocking,
                "delta": None,
                "cand_mean": cand_mean,
                "threshold": thr,
                "note": "new metric below its own threshold",
            }
        return {
            "metric_id": metric_id,
            "class": cls,
            "verdict": "PASS",
            "blocking": False,
            "delta": None,
            "cand_mean": cand_mean,
            "threshold": thr,
            "note": "new metric, above floor",
        }

    base_mean = base["mean"]
    delta = round(cand_mean - base_mean, 4)

    # Worst of: absolute floor breach, and drop-vs-baseline bucket.
    if below_floor:
        blocking = cls in ("gate", "guardrail")
        return {
            "metric_id": metric_id,
            "class": cls,
            "verdict": "FAIL" if blocking else "REVIEW",
            "blocking": blocking,
            "delta": delta,
            "cand_mean": cand_mean,
            "base_mean": base_mean,
            "threshold": thr,
            "note": f"below threshold {thr} (mean {cand_mean})",
        }

    if delta <= -fail_tol:
        blocking = cls in ("gate", "guardrail")
        return {
            "metric_id": metric_id,
            "class": cls,
            "verdict": "FAIL" if blocking else "REVIEW",
            "blocking": blocking,
            "delta": delta,
            "cand_mean": cand_mean,
            "base_mean": base_mean,
            "threshold": thr,
            "note": f"dropped {-delta} vs baseline (fail_tol {fail_tol})",
        }

    if delta < -info_tol:
        return {
            "metric_id": metric_id,
            "class": cls,
            "verdict": "REVIEW",
            "blocking": False,
            "delta": delta,
            "cand_mean": cand_mean,
            "base_mean": base_mean,
            "threshold": thr,
            "note": f"slid {-delta} vs baseline (below fail_tol, above noise)",
        }

    return {
        "metric_id": metric_id,
        "class": cls,
        "verdict": "PASS",
        "blocking": False,
        "delta": delta,
        "cand_mean": cand_mean,
        "base_mean": base_mean,
        "threshold": thr,
        "note": "flat or improved within tolerance",
    }


def compare(
    baseline: dict, candidate: dict, fail_tol: float, info_tol: float
) -> dict:
    base_metrics = baseline.get("metrics", {})
    cand_metrics = candidate.get("metrics", {})
    all_ids = sorted(set(base_metrics) | set(cand_metrics))

    rows = [
        _verdict_for(
            mid,
            base_metrics.get(mid),
            cand_metrics.get(mid),
            fail_tol,
            info_tol,
        )
        for mid in all_ids
    ]

    blocking = [r for r in rows if r["blocking"]]
    review = [r for r in rows if not r["blocking"] and r["verdict"] == "REVIEW"]

    if blocking:
        overall, code = "FAIL", EXIT_FAIL
    elif review:
        overall, code = "REVIEW", EXIT_REVIEW
    else:
        overall, code = "PASS", EXIT_PASS

    prompt_changed = baseline.get("prompt_hash") != candidate.get("prompt_hash")
    return {
        "overall": overall,
        "exit_code": code,
        "rows": rows,
        "n_fail": len(blocking),
        "n_review": len(review),
        "prompt_changed": prompt_changed,
        "baseline_sha": baseline.get("git_sha"),
        "candidate_sha": candidate.get("git_sha"),
        "candidate_dirty": candidate.get("git_dirty"),
        "fail_tol": fail_tol,
        "info_tol": info_tol,
    }


def _print_human(result: dict) -> None:
    order = {"FAIL": 0, "REVIEW": 1, "PASS": 2}
    rows = sorted(
        result["rows"], key=lambda r: (order.get(r["verdict"], 9), r["metric_id"])
    )
    print("=" * 72)
    print(
        f"EVAL REGRESSION COMPARE   baseline {result['baseline_sha']} -> "
        f"candidate {result['candidate_sha']}"
        + ("  [DIRTY]" if result.get("candidate_dirty") else "")
    )
    print(
        f"tolerances: fail<= -{result['fail_tol']}  review< -{result['info_tol']}"
    )
    if result["prompt_changed"]:
        print(
            "!! prompt_hash differs between baseline and candidate. A score change "
            "may be the prompt, not a regression -- consider re-blessing."
        )
    print("-" * 72)
    for r in rows:
        d = r.get("delta")
        dstr = f"{d:+.4f}" if isinstance(d, (int, float)) else "  n/a "
        print(
            f"[{r['verdict']:6}] {r['metric_id']:40s} d={dstr:>9}  "
            f"({r['class']}) {r['note']}"
        )
    print("-" * 72)
    print(
        f"OVERALL: {result['overall']}   "
        f"fail={result['n_fail']} review={result['n_review']}   "
        f"(exit {result['exit_code']})"
    )
    print("=" * 72)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "candidate_pos",
        nargs="?",
        help="Candidate snapshot path (positional).",
    )
    ap.add_argument("--candidate", type=Path, default=None, help="Candidate snapshot.")
    ap.add_argument(
        "--baseline",
        type=Path,
        default=BASELINE_PATH,
        help="Baseline snapshot (default: evals/snapshots/baseline.json).",
    )
    ap.add_argument(
        "--fail-tol",
        type=float,
        default=0.10,
        help="Drop >= this vs baseline is a blocking FAIL (default 0.10).",
    )
    ap.add_argument(
        "--info-tol",
        type=float,
        default=0.03,
        help="Drop < this vs baseline is noise / PASS (default 0.03).",
    )
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = ap.parse_args()

    cand_path = args.candidate or (
        Path(args.candidate_pos) if args.candidate_pos else None
    )
    if cand_path is None:
        print("No candidate snapshot given (positional or --candidate).", file=sys.stderr)
        return EXIT_CANNOT_RUN
    if not cand_path.exists():
        print(f"Candidate snapshot not found: {cand_path}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    if not args.baseline.exists():
        print(
            f"Baseline not found: {args.baseline}. Bless one first with "
            "`python scripts/eval_snapshot.py --baseline`.",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN

    baseline = _load(args.baseline)
    candidate = _load(cand_path)
    result = compare(baseline, candidate, args.fail_tol, args.info_tol)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_human(result)
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
