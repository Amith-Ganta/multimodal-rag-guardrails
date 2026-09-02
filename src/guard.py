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

from typing import Optional

from .answer import Answer, answer_query
from .config import SETTINGS
from .index import MultimodalIndex


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
        self._last_answer: Optional[Answer] = None

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
            self._last_answer = ans
            return ans.text

        rails.register_action(rag_answer, name="rag_answer")
        rails.passthrough_fn = rag_answer
        return rails

    @property
    def rails(self):
        if self._rails is None:
            self._rails = self._build_rails()
        return self._rails

    def ask(self, question: str) -> dict:
        """Return {'answer', 'blocked', 'answer_obj'}.

        If guardrails are disabled in settings, the pipeline runs unguarded so
        the system is still usable offline; the return shape is unchanged.
        """
        if not SETTINGS.guardrails_enabled:
            ans = answer_query(self.index, question, tag="unguarded-rag")
            self._last_answer = ans
            return {"answer": ans.text, "blocked": False, "answer_obj": ans}

        # Reset so `blocked` reflects only this call: if the input rail blocks,
        # the passthrough never runs and `_last_answer` must stay None even when
        # a previous call on this instance produced an answer.
        self._last_answer = None
        result = self.rails.generate(
            messages=[{"role": "user", "content": question}]
        )
        content = result["content"] if isinstance(result, dict) else str(result)
        blocked = self._last_answer is None or content.strip() == BLOCKED_MESSAGE.strip()
        return {
            "answer": content,
            "blocked": blocked,
            "answer_obj": self._last_answer,
        }


def _config_path() -> str:
    from .config import GUARDRAILS_DIR

    return str(GUARDRAILS_DIR)
