"""Convenience wrapper around the DeepEval offline gate.

This just shells out to the real gate so you have one command to run:
    python scripts/run_eval.py

It is equivalent to:
    deepeval test run evals/test_multimodal_rag.py

The gate needs OPENAI_API_KEY in the environment (the judge model is
gpt-4o-mini). Without a key the tests skip cleanly rather than failing, so this
wrapper is safe to run either way. It also refuses to run if no index exists,
pointing you at build_index.py first.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import INDEX_DIR  # noqa: E402


def main() -> int:
    if not (Path(INDEX_DIR) / "text.faiss").exists():
        print(
            "No index found. Build one first:\n"
            "    python scripts/make_sample_pdf.py\n"
            "    python scripts/build_index.py"
        )
        return 1

    if not os.getenv("OPENAI_API_KEY"):
        print(
            "OPENAI_API_KEY is not set. The judge model (gpt-4o-mini) needs it.\n"
            "The gate will SKIP every test without a key, so nothing is measured.\n"
            "Set the key, then re-run."
        )

    # DeepEval exposes its CLI as the `deepeval.cli.main` module (console script
    # `deepeval`); `python -m deepeval` fails because the package has no
    # __main__. Invoke the CLI module so this works without the console script
    # being on PATH.
    cmd = [
        sys.executable,
        "-m",
        "deepeval.cli.main",
        "test",
        "run",
        str(ROOT / "evals" / "test_multimodal_rag.py"),
    ]

    # Windows-specific environment fixes for the gate:
    #  * PYTHONUTF8=1 forces UTF-8 stdout so DeepEval's rich progress bar can
    #    print its emoji glyphs; the legacy cp1252 console otherwise raises
    #    UnicodeEncodeError on characters like the target/warning symbols and
    #    crashes each test on teardown.
    #  * The judge model (gpt-4o-mini) verdict calls can be slow on a cold or
    #    rate-limited connection; the default per-attempt/per-task timeouts are
    #    too tight and mark metrics as timed out. Raise them so a slow judge
    #    call completes instead of failing the whole test.
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE", "300")
    env.setdefault("DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE", "600")
    env.setdefault("DEEPEVAL_TASK_GATHER_BUFFER_SECONDS_OVERRIDE", "120")

    print("running:", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(ROOT), env=env)


if __name__ == "__main__":
    raise SystemExit(main())
