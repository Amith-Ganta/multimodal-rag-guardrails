"""FastAPI surface for the multimodal RAG service.

Endpoints:
  GET  /health         -> liveness + whether an index is loaded
  POST /ask            -> guarded RAG answer (NeMo input/output rails)
  POST /ask_a2a        -> retriever/verifier agent-to-agent answer
  GET  /gateway/summary-> cost/token/model audit from the LLM gateway

The index is loaded once at startup. Build it first with
scripts/build_index.py, otherwise startup logs a warning and the ask endpoints
return 503 until an index exists.

Run:
    uvicorn src.api:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import SETTINGS
from .gateway import get_gateway

app = FastAPI(title="Multimodal RAG (dual-encoder + guardrails + A2A)")

_log = logging.getLogger("multimodal_rag.api")

# Loaded at startup; None until an index exists on disk.
_INDEX = None
_GUARD = None


def _load_index():
    """Load the index and build the guarded pipeline once, at startup.

    Any failure here (no index yet, a partial/corrupt index, a faiss or payload
    read error, or a GuardedRAG construction failure) leaves both globals None
    and is logged rather than raised, so the process still starts and the ask
    endpoints return a clean 503 instead of the whole service crashing on boot.
    A missing index is the expected first-run case; anything else is logged with
    a traceback so it is diagnosable.
    """
    global _INDEX, _GUARD
    from .guard import GuardedRAG
    from .index import MultimodalIndex

    try:
        _INDEX = MultimodalIndex.load()
        _GUARD = GuardedRAG(_INDEX)
    except FileNotFoundError:
        _INDEX = None
        _GUARD = None
        _log.warning(
            "No index found on disk; /ask and /ask_a2a will return 503 until "
            "one is built with scripts/build_index.py."
        )
    except Exception:  # noqa: BLE001 - startup must never hard-crash here
        _INDEX = None
        _GUARD = None
        _log.exception(
            "Failed to load the index or build the guarded pipeline at startup; "
            "the service will start but the ask endpoints will return 503."
        )


@app.on_event("startup")
def _startup() -> None:
    _load_index()


# --- request / response models ---------------------------------------------
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)


class AskResponse(BaseModel):
    answer: str
    blocked: bool
    text_sources: List[str] = []
    image_sources: List[str] = []
    model_served: Optional[str] = None


class A2AResponse(BaseModel):
    answer: str
    accepted: bool
    rounds: int
    transcript: List[Dict[str, Any]]
    text_sources: List[str] = []


# --- endpoints --------------------------------------------------------------
@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "index_loaded": _INDEX is not None,
        "guardrails_enabled": SETTINGS.guardrails_enabled,
    }


def _require_index():
    if _INDEX is None:
        raise HTTPException(
            status_code=503,
            detail="No index loaded. Build one with scripts/build_index.py.",
        )


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest) -> AskResponse:
    _require_index()
    result = _GUARD.ask(req.question)
    ans_obj = result.get("answer_obj")
    text_sources: List[str] = []
    image_sources: List[str] = []
    model_served = None
    if ans_obj is not None:
        text_sources = [h.chunk.id for h in ans_obj.context.text_hits]
        image_sources = [h.image_id for h in ans_obj.context.image_hits]
        model_served = ans_obj.model_served
    return AskResponse(
        answer=result["answer"],
        blocked=result["blocked"],
        text_sources=text_sources,
        image_sources=image_sources,
        model_served=model_served,
    )


@app.post("/ask_a2a", response_model=A2AResponse)
def ask_a2a(req: AskRequest) -> A2AResponse:
    _require_index()
    from .a2a import run_a2a

    res = run_a2a(_INDEX, req.question)
    return A2AResponse(
        answer=res.final_answer,
        accepted=res.accepted,
        rounds=res.rounds,
        transcript=[
            {"sender": m.sender, "kind": m.kind, "payload": m.payload}
            for m in res.transcript
        ],
        text_sources=[h.chunk.id for h in res.context.text_hits],
    )


@app.get("/gateway/summary")
def gateway_summary() -> Dict[str, Any]:
    return get_gateway().summary()
