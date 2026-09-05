"""A thin LLM gateway over LiteLLM.

Every model call in this project goes through here, so provider choice,
fallbacks, caching, cost tracking, and per-call logging live in one place. The
pattern follows the course gateway notebook (LiteLLM completion + fallbacks +
completion_cost + local cache + success/failure callbacks), adapted into a small
class with an in-memory audit log.

Multimodal note: LiteLLM's completion() takes the OpenAI-style content-array
message shape, so vision calls (text blocks + image_url data URIs) pass straight
through unchanged.
"""

from __future__ import annotations

import logging
import threading
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .config import SETTINGS

# Keep LiteLLM quiet; it is chatty on import and per call. Scoped to LiteLLM's
# own modules (and pydantic, which it drives hard enough to emit deprecation
# noise) rather than a blanket ignore, so warnings from this project's own
# code are never silently swallowed.
warnings.filterwarnings("ignore", module=r"litellm.*")
warnings.filterwarnings("ignore", module=r"pydantic.*")
logging.getLogger("LiteLLM").setLevel(logging.ERROR)


@dataclass
class CallRecord:
    model_requested: str
    model_served: str
    prompt_tokens: int
    completion_tokens: int
    latency_sec: float
    cost_usd: float
    cached: bool = False
    tag: str = "anonymous"


@dataclass
class LLMGateway:
    """Unified entry point for chat/vision completions across providers."""

    primary_model: str = SETTINGS.vision_model
    fallbacks: tuple[str, ...] = field(default_factory=lambda: SETTINGS.gateway_fallbacks)
    enable_cache: bool = SETTINGS.gateway_cache
    call_log: List[CallRecord] = field(default_factory=list)
    _configured: bool = False
    # Guards call_log and the one-time _configure(). Under FastAPI, each sync
    # endpoint runs in a worker thread, so concurrent /ask calls append here while
    # /gateway/summary iterates it; without this lock a summary read can raise
    # "list changed size during iteration" or read a torn total.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _configure(self) -> None:
        if self._configured:
            return
        with self._lock:
            if self._configured:  # another thread may have configured while we waited
                return
            import litellm

            litellm.suppress_debug_info = True
            litellm.drop_params = True  # ignore params a given provider does not support
            if self.enable_cache:
                from litellm.caching import Cache

                litellm.cache = Cache(type="local")
            self._configured = True

    def complete(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        tag: str = "anonymous",
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        use_fallbacks: bool = True,
    ) -> str:
        """Run one completion through the gateway and return only the text.

        Thin wrapper over complete_verbose for callers that do not need the
        per-call record.
        """
        text, _record = self.complete_verbose(
            messages,
            model=model,
            tag=tag,
            temperature=temperature,
            max_tokens=max_tokens,
            use_fallbacks=use_fallbacks,
        )
        return text

    def complete_verbose(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        tag: str = "anonymous",
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        use_fallbacks: bool = True,
    ) -> Tuple[str, CallRecord]:
        """Run one completion and return (text, this call's CallRecord).

        Returning the record inline lets a caller read the model served for THIS
        call without indexing call_log[-1], which is unsafe under concurrency: a
        different thread may append its own record between this call finishing and
        the caller reading the list.

        Records model served, tokens, latency, and USD cost in call_log (under
        the lock). Fallbacks rescue the call transparently if the primary fails.
        """
        import time

        from litellm import completion, completion_cost

        self._configure()
        model = model or self.primary_model

        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "timeout": SETTINGS.gateway_timeout_sec,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if self.enable_cache:
            kwargs["caching"] = True
        if use_fallbacks and self.fallbacks:
            kwargs["fallbacks"] = list(self.fallbacks)

        start = time.time()
        resp = completion(**kwargs)
        latency = time.time() - start

        try:
            cost = float(completion_cost(completion_response=resp) or 0.0)
        except Exception:
            cost = 0.0

        usage = getattr(resp, "usage", None)
        record = CallRecord(
            model_requested=model,
            model_served=getattr(resp, "model", model),
            prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            latency_sec=round(latency, 3),
            cost_usd=cost,
            cached=bool(getattr(resp, "_hidden_params", {}).get("cache_hit", False)),
            tag=tag,
        )
        with self._lock:
            self.call_log.append(record)
        return resp.choices[0].message.content or "", record

    # --- observability helpers --------------------------------------------
    def total_cost(self) -> float:
        with self._lock:
            return round(sum(r.cost_usd for r in self.call_log), 8)

    def summary(self) -> Dict[str, Any]:
        # Snapshot under the lock so a concurrent append cannot change the list
        # mid-iteration; compute the aggregates off the snapshot.
        with self._lock:
            records = list(self.call_log)
        return {
            "calls": len(records),
            "total_cost_usd": round(sum(r.cost_usd for r in records), 8),
            "total_prompt_tokens": sum(r.prompt_tokens for r in records),
            "total_completion_tokens": sum(r.completion_tokens for r in records),
            "avg_latency_sec": (
                round(sum(r.latency_sec for r in records) / len(records), 3)
                if records
                else 0.0
            ),
            "total_latency_sec": round(sum(r.latency_sec for r in records), 3),
            "models_served": sorted({r.model_served for r in records}),
        }


# One shared gateway for the process.
_GATEWAY: Optional[LLMGateway] = None
_GATEWAY_LOCK = threading.Lock()


def get_gateway() -> LLMGateway:
    global _GATEWAY
    if _GATEWAY is None:
        with _GATEWAY_LOCK:
            if _GATEWAY is None:  # double-checked: only one gateway per process
                _GATEWAY = LLMGateway()
    return _GATEWAY
