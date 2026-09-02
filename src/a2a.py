"""A small agent-to-agent (A2A) layer over the multimodal RAG pipeline.

Two agents pass structured messages to each other:

  * RetrieverAgent: takes a question, retrieves multimodal context, drafts a
    grounded answer through the gateway, and emits an AgentMessage carrying the
    answer plus the exact context it used.

  * VerifierAgent: receives that message, checks the drafted answer against the
    same context (is every claim supported? does it answer the question?), and
    replies with a verdict. If the verdict is "revise", it hands back concrete
    feedback the retriever uses to redraft.

The two exchange messages in a short loop (bounded by verify_max_retries) until
the verifier accepts or the retry budget runs out. Messages are plain
dataclasses so the exchange is inspectable and serialisable, which is the point
of the A2A framing: coordination is explicit data, not hidden control flow.

This is a deliberately small, honest A2A: two cooperating roles over a shared
message type. It is not a network protocol or a multi-process broker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from .answer import Answer, RetrievedContext, answer_query, retrieve
from .config import SETTINGS
from .gateway import get_gateway
from .index import MultimodalIndex

Role = Literal["retriever", "verifier"]
Verdict = Literal["accept", "revise"]


@dataclass
class AgentMessage:
    """One turn in the A2A exchange."""

    sender: Role
    kind: str  # "draft" | "verdict"
    payload: Dict[str, Any] = field(default_factory=dict)

    def short(self) -> str:
        return f"[{self.sender}:{self.kind}] " + json.dumps(self.payload, default=str)[:200]


@dataclass
class A2AResult:
    question: str
    final_answer: str
    accepted: bool
    rounds: int
    transcript: List[AgentMessage]
    context: RetrievedContext


class RetrieverAgent:
    """Retrieves context and drafts (or redrafts) a grounded answer."""

    def __init__(self, index: MultimodalIndex) -> None:
        self.index = index

    def draft(
        self, question: str, feedback: Optional[str] = None
    ) -> tuple[Answer, AgentMessage]:
        query = question
        if feedback:
            # Fold the verifier's feedback into the query so retrieval and the
            # answer prompt both see what was missing last round.
            query = f"{question}\n\n[Reviewer feedback to address: {feedback}]"
        context = retrieve(self.index, query)
        ans = answer_query(self.index, query, tag="a2a-retriever", context=context)
        # Keep the original question on the Answer so downstream sees the user's
        # intent, not the feedback-augmented query.
        ans.question = question
        msg = AgentMessage(
            sender="retriever",
            kind="draft",
            payload={
                "question": question,
                "answer": ans.text,
                "context": context.as_retrieval_context(),
            },
        )
        return ans, msg


VERIFIER_SYSTEM = (
    "You are a strict verification agent. You are given a QUESTION, a set of "
    "CONTEXT passages (the only allowed evidence), and a DRAFT ANSWER produced "
    "by another agent. Judge two things: (1) is every factual claim in the "
    "draft supported by the context, and (2) does the draft actually answer the "
    "question. Reply with a single JSON object and nothing else, of the form "
    '{"verdict": "accept" | "revise", "reason": "<one sentence>", '
    '"feedback": "<if revise, a concrete instruction for the drafting agent; '
    'else empty>"}. Choose "revise" if any claim is unsupported or the question '
    "is not answered."
)


class VerifierAgent:
    """Checks a drafted answer against its context and returns a verdict."""

    def verify(self, msg: AgentMessage) -> AgentMessage:
        question = msg.payload.get("question", "")
        answer = msg.payload.get("answer", "")
        context = msg.payload.get("context", [])
        ctx_block = "\n\n---\n\n".join(context) if context else "(no context)"
        user = (
            f"QUESTION:\n{question}\n\n"
            f"CONTEXT:\n{ctx_block}\n\n"
            f"DRAFT ANSWER:\n{answer}\n\n"
            "Return the JSON verdict now."
        )
        gateway = get_gateway()
        raw = gateway.complete(
            [
                {"role": "system", "content": VERIFIER_SYSTEM},
                {"role": "user", "content": user},
            ],
            model=SETTINGS.eval_judge_model,
            tag="a2a-verifier",
        )
        verdict, reason, feedback = _parse_verdict(raw)
        return AgentMessage(
            sender="verifier",
            kind="verdict",
            payload={"verdict": verdict, "reason": reason, "feedback": feedback},
        )


def _parse_verdict(raw: str) -> tuple[Verdict, str, str]:
    """Parse the verifier's JSON reply defensively.

    A malformed reply is treated as "accept" so a judge hiccup never blocks a
    usable answer; the reason records that the parse failed.
    """
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            verdict = obj.get("verdict", "accept")
            verdict = "revise" if str(verdict).lower().startswith("rev") else "accept"
            return verdict, str(obj.get("reason", "")), str(obj.get("feedback", ""))
        except (json.JSONDecodeError, ValueError):
            pass
    return "accept", "verifier reply was not valid JSON; defaulting to accept", ""


def run_a2a(index: MultimodalIndex, question: str) -> A2AResult:
    """Run the retriever/verifier exchange until accept or retries exhausted."""
    retriever = RetrieverAgent(index)
    verifier = VerifierAgent()
    transcript: List[AgentMessage] = []
    feedback: Optional[str] = None
    ans: Optional[Answer] = None
    accepted = False
    max_rounds = max(1, SETTINGS.verify_max_retries + 1)

    round_no = 0
    for round_no in range(1, max_rounds + 1):
        ans, draft_msg = retriever.draft(question, feedback)
        transcript.append(draft_msg)
        verdict_msg = verifier.verify(draft_msg)
        transcript.append(verdict_msg)
        if verdict_msg.payload.get("verdict") == "accept":
            accepted = True
            break
        feedback = verdict_msg.payload.get("feedback") or verdict_msg.payload.get("reason")

    assert ans is not None
    return A2AResult(
        question=question,
        final_answer=ans.text,
        accepted=accepted,
        rounds=round_no,
        transcript=transcript,
        context=ans.context,
    )
