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
import re
import threading
import time
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


# --- optional pacing for answer-generation calls ----------------------------
# Mirrors evals/robust_judge.py's judge-side throttle. Off by default (limit=0
# / interval=0) so normal API/production traffic is never gated; the eval gate
# turns this on via GATEWAY_MAX_CONCURRENCY / GATEWAY_MIN_INTERVAL_SECONDS
# because it drives many concurrent answer-generation calls (one per golden)
# on the SAME OpenAI org TPM budget the judge is already pacing itself against.
# Without this, the two independently-throttled call paths still collide on
# one shared TPM pool -- confirmed in CI via sustained 200000/200000 TPM 429s
# from LiteLLM's fallback chain while the judge's own pacing looked healthy.
_GATEWAY_SEMAPHORE: Optional[threading.Semaphore] = None
_GATEWAY_SEMAPHORE_LOCK = threading.Lock()
_GATEWAY_RATE_LOCK = threading.Lock()
_GATEWAY_LAST_CALL_AT = 0.0

_RETRY_AFTER_RE = re.compile(r"try again in ([\d.]+)(ms|s)", re.IGNORECASE)
_GATEWAY_RATE_LIMIT_MAX_RETRIES = 3
_GATEWAY_RATE_LIMIT_FALLBACK_WAIT_SECONDS = 2.0


def _gateway_semaphore() -> Optional[threading.Semaphore]:
    global _GATEWAY_SEMAPHORE
    limit = SETTINGS.gateway_max_concurrency
    if limit <= 0:
        return None
    if _GATEWAY_SEMAPHORE is None:
        with _GATEWAY_SEMAPHORE_LOCK:
            if _GATEWAY_SEMAPHORE is None:
                _GATEWAY_SEMAPHORE = threading.Semaphore(limit)
    return _GATEWAY_SEMAPHORE


def _gateway_throttle() -> None:
    min_interval = SETTINGS.gateway_min_interval_seconds
    if min_interval <= 0:
        return
    global _GATEWAY_LAST_CALL_AT
    with _GATEWAY_RATE_LOCK:
        wait = _GATEWAY_LAST_CALL_AT + min_interval - time.time()
        if wait > 0:
            time.sleep(wait)
        _GATEWAY_LAST_CALL_AT = time.time()


def _is_gateway_rate_limit_error(exc: Exception) -> bool:
    name = exc.__class__.__name__
    if name == "RateLimitError":
        return True
    return "RateLimitError" in name or "rate_limit" in str(exc).lower()


def _gateway_rate_limit_wait_seconds(exc: Exception) -> float:
    match = _RETRY_AFTER_RE.search(str(exc))
    if not match:
        return _GATEWAY_RATE_LIMIT_FALLBACK_WAIT_SECONDS
    value, unit = match.groups()
    seconds = float(value) / 1000.0 if unit.lower() == "ms" else float(value)
    return min(max(seconds, 0.05) * 1.2, 30.0)


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

        def _do_call():
            _gateway_throttle()
            last_exc: Optional[Exception] = None
            for attempt in range(_GATEWAY_RATE_LIMIT_MAX_RETRIES + 1):
                try:
                    return completion(**kwargs)
                except Exception as exc:  # noqa: BLE001 - re-raised below if not a 429
                    if not _is_gateway_rate_limit_error(exc):
                        raise
                    last_exc = exc
                    if attempt < _GATEWAY_RATE_LIMIT_MAX_RETRIES:
                        time.sleep(_gateway_rate_limit_wait_seconds(exc))
            raise last_exc

        start = time.time()
        semaphore = _gateway_semaphore()
        if semaphore is not None:
            with semaphore:
                resp = _do_call()
        else:
            resp = _do_call()
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
