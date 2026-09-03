"""NeMo Guardrails wrapper around the multimodal answer pipeline.

Two rails, matching the house config from Project 1:
  * input rail  (self check input):  blocks off-domain, jailbreak, and
    secret/PII-extraction requests before any retrieval or model call.
  * output rail (self check output): blocks answers that would leak a secret,
    a system prompt, or private PII.

The self-check verdict (yes = unsafe/block, no = safe/allow) is turned into an
allow/block decision by NeMo's own built-in `is_content_safe` output parser,
which the self-check flows register by default; this module does not override it.

Guardrails only see text, so the input rail runs on the user's question string
and the output rail runs on the final answer string. The retrieval + vision call
happens between the two rails: it is registered as the rails passthrough
function, so NeMo runs our own answerer instead of its generic LLM generation,
and the input/output self-check rails still wrap it.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Optional

from .answer import Answer, answer_query
from .config import SETTINGS
from .index import MultimodalIndex


_log = logging.getLogger("multimodal_rag.guard")

# The Answer captured by the rails passthrough, per request. A ContextVar is
# isolated per asyncio task and per thread, so concurrent /ask calls (each a
# worker thread running rails.generate on its own event loop) never overwrite
# each other's captured answer. An instance attribute would leak one request's
# sources into another's response under load.
_CURRENT_ANSWER: contextvars.ContextVar[Optional[Answer]] = contextvars.ContextVar(
    "current_answer", default=None
)

BLOCKED_MESSAGE = (
    "I can't help with that. I answer questions about the ingested document "
    "(its text and images) and how this retrieval system works."
)


def _user_question(context: dict, events: list) -> str:
    """Pull the user's question for the passthrough call.

    Prefer the `last_user_message` context key NeMo maintains, but fall back to
    the last UserMessage in the event stream (the same source NeMo itself reads
    at generation.py) so the answerer never runs on an empty string if that key
    is absent. The event `text` may be a plain string or a list of typed parts.
    """
    question = context.get("last_user_message")
    if isinstance(question, str) and question.strip():
        return question

    for event in reversed(events):
        if isinstance(event, dict) and event.get("type") == "UserMessage":
            text = event.get("text", "")
            if isinstance(text, list):
                return " ".join(
                    part["text"]
                    for part in text
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            return text or ""
    return ""


class GuardedRAG:
    """Runs the multimodal RAG pipeline behind NeMo input/output rails."""

    def __init__(self, index: MultimodalIndex) -> None:
        self.index = index
        self._rails = None
        # Set once we discover the rails cannot be built (missing nemoguardrails,
        # bad config, or no judge key). After that we stop retrying and run the
        # pipeline unguarded so the service stays usable instead of erroring on
        # every call.
        self._rails_unavailable = False

    def _build_rails(self):
        from nemoguardrails import LLMRails, RailsConfig

        config = RailsConfig.from_path(_config_path())
        rails = LLMRails(config)

        # NeMo already registers a correct `is_content_safe` output parser for
        # the self-check flows, so we do not override it. The passthrough
        # function below is what NeMo calls to produce the answer, in place of
        # its generic LLM generation; the input and output self-check rails run
        # before and after it. Its signature is fixed by NeMo 0.24.0:
        # `async def fn(context: dict, events: list) -> str`.
        async def rag_answer(context: dict, events: list) -> str:
            question = _user_question(context, events)
            ans = answer_query(self.index, question, tag="guarded-rag")
            _CURRENT_ANSWER.set(ans)
            return ans.text

        rails.register_action(rag_answer, name="rag_answer")
        rails.passthrough_fn = rag_answer
        return rails

    @property
    def rails(self):
        if self._rails is None:
            self._rails = self._build_rails()
        return self._rails

    def _answer_unguarded(self, question: str) -> dict:
        ans = answer_query(self.index, question, tag="unguarded-rag")
        return {"answer": ans.text, "blocked": False, "answer_obj": ans}

    def ask(self, question: str) -> dict:
        """Return {'answer', 'blocked', 'answer_obj'}.

        Guardrails run only when they actually can: they must be enabled in
        settings, and the self-check rails call an LLM (gpt-4o-mini), so a judge
        key must be present. When guardrails are disabled, no key is available,
        or the rails fail to build or run (for example nemoguardrails is not
        installed, or the config cannot be read), the pipeline runs UNGUARDED so
        the service stays usable instead of erroring on every call. The return
        shape is identical in every case; a degraded call reports blocked=False.
        """
        if not SETTINGS.guardrails_enabled:
            return self._answer_unguarded(question)

        # The self-check input/output rails are LLM-judged (gpt-4o-mini). With no
        # judge key they cannot run at all, so guarding is impossible; degrade to
        # unguarded rather than raise on the first call.
        if not SETTINGS.has_openai_key:
            if not self._rails_unavailable:
                self._rails_unavailable = True
                _log.warning(
                    "Guardrails are enabled but no OPENAI_API_KEY is set; the "
                    "self-check rails cannot run. Answering UNGUARDED. Set a key "
                    "to enable the input/output rails."
                )
            return self._answer_unguarded(question)

        if self._rails_unavailable:
            return self._answer_unguarded(question)

        # Reset the per-request slot so `blocked` reflects only this call: if the
        # input rail blocks, the passthrough never runs and the captured answer
        # must stay None even when a previous call produced one.
        token = _CURRENT_ANSWER.set(None)
        try:
            try:
                result = self.rails.generate(
                    messages=[{"role": "user", "content": question}]
                )
            except Exception:  # noqa: BLE001 - never let a rails failure crash a call
                # Missing nemoguardrails, an unreadable config, or a judge-call
                # failure lands here. Mark rails unavailable so later calls skip
                # straight to the unguarded path instead of paying the same
                # failure.
                self._rails_unavailable = True
                _log.exception(
                    "Guardrails failed to run; falling back to an UNGUARDED "
                    "answer for this and subsequent calls."
                )
                return self._answer_unguarded(question)

            last_answer = _CURRENT_ANSWER.get()
            content = result["content"] if isinstance(result, dict) else str(result)
            blocked = (
                last_answer is None or content.strip() == BLOCKED_MESSAGE.strip()
            )
            return {
                "answer": content,
                "blocked": blocked,
                "answer_obj": last_answer,
            }
        finally:
            _CURRENT_ANSWER.reset(token)


def _config_path() -> str:
    from .config import GUARDRAILS_DIR

    return str(GUARDRAILS_DIR)
