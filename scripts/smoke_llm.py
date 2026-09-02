"""Staged end-to-end smoke test with the real key, printing each step so a
hang is attributable to a specific stage. Not part of the app; a diagnostic.
"""
from __future__ import annotations
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def stamp(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


stamp("importing index + loading")
from src.index import MultimodalIndex  # noqa: E402
idx = MultimodalIndex.load()
stamp(f"index loaded: {idx.text_index.ntotal} text, {idx.image_index.ntotal} img")

stamp("retrieval only (no LLM)")
from src.answer import retrieve  # noqa: E402
ctx = retrieve(idx, "overall accuracy of the combined system")
stamp(f"retrieved {len(ctx.text_hits)} text hits, {len(ctx.image_hits)} image hits")

stamp("gateway: one direct completion (bypass guardrails)")
from src.answer import answer_query  # noqa: E402
ans = answer_query(idx, "What overall accuracy did the combined system reach?", tag="smoke-direct")
stamp(f"direct answer_query done, model={ans.model_served}")
print("DIRECT ANSWER:", ans.text[:400], flush=True)

stamp("guardrails: GuardedRAG.ask (this makes several LLM calls)")
from src.guard import GuardedRAG  # noqa: E402
g = GuardedRAG(idx)
r = g.ask("What overall accuracy did the combined system reach?")
stamp(f"guarded ask done, blocked={r['blocked']}")
print("GUARDED ANSWER:", r["answer"][:400], flush=True)

stamp("DONE")
