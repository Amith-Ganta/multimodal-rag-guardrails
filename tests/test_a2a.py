"""Unit tests for src/a2a.py.

_parse_verdict() is a pure function - no mocking needed. run_a2a()'s
round-limit behaviour is tested by monkeypatching SETTINGS.verify_max_retries
(via dataclasses.replace, since Settings is frozen) and mocking
RetrieverAgent.draft / VerifierAgent.verify so no real LLM call happens.
"""

from __future__ import annotations

import src.a2a as a2a_module
from src.a2a import AgentMessage, RetrievedContext, _parse_verdict, run_a2a
from src.answer import Answer


def test_parse_verdict_accepts_valid_accept_json():
    raw = '{"verdict": "accept", "reason": "looks correct", "feedback": ""}'
    verdict, reason, feedback = _parse_verdict(raw)
    assert verdict == "accept"
    assert reason == "looks correct"
    assert feedback == ""


def test_parse_verdict_accepts_valid_revise_json():
    raw = '{"verdict": "revise", "reason": "missing detail", "feedback": "add the date"}'
    verdict, reason, feedback = _parse_verdict(raw)
    assert verdict == "revise"
    assert reason == "missing detail"
    assert feedback == "add the date"


def test_parse_verdict_coerces_any_rev_prefixed_verdict_to_revise():
    raw = '{"verdict": "REVISION_NEEDED", "reason": "x", "feedback": "y"}'
    verdict, _, _ = _parse_verdict(raw)
    assert verdict == "revise"


def test_parse_verdict_extracts_json_surrounded_by_extra_text():
    raw = 'Here is my verdict:\n{"verdict": "accept", "reason": "ok", "feedback": ""}\nThanks!'
    verdict, reason, _ = _parse_verdict(raw)
    assert verdict == "accept"
    assert reason == "ok"


def test_parse_verdict_fails_closed_on_malformed_json():
    raw = "{not valid json at all"
    verdict, reason, feedback = _parse_verdict(raw)
    assert verdict == "revise"
    assert "not valid JSON" in reason
    assert feedback == ""


def test_parse_verdict_fails_closed_when_no_braces_found():
    raw = "no json here whatsoever"
    verdict, reason, feedback = _parse_verdict(raw)
    assert verdict == "revise"
    assert "not valid JSON" in reason


def _fake_answer(question: str) -> Answer:
    return Answer(question=question, text="draft answer", context=RetrievedContext())


def test_run_a2a_accepts_on_first_round(monkeypatch):
    draft_msg = AgentMessage(sender="retriever", kind="draft", payload={})
    verdict_msg = AgentMessage(
        sender="verifier",
        kind="verdict",
        payload={"verdict": "accept", "reason": "good", "feedback": ""},
    )

    monkeypatch.setattr(
        a2a_module.RetrieverAgent,
        "draft",
        lambda self, question, feedback=None: (_fake_answer(question), draft_msg),
    )
    monkeypatch.setattr(a2a_module.VerifierAgent, "verify", lambda self, msg: verdict_msg)

    result = run_a2a(index=object(), question="what is the revenue?")

    assert result.accepted is True
    assert result.rounds == 1
    assert len(result.transcript) == 2


def test_run_a2a_stops_at_max_rounds_when_never_accepted(monkeypatch, settings_override):
    settings_override(a2a_module, verify_max_retries=1)  # max_rounds = max(1, 1+1) = 2

    draft_msg = AgentMessage(sender="retriever", kind="draft", payload={})
    revise_msg = AgentMessage(
        sender="verifier",
        kind="verdict",
        payload={"verdict": "revise", "reason": "still wrong", "feedback": "try again"},
    )

    draft_calls = []

    def _draft(self, question, feedback=None):
        draft_calls.append(feedback)
        return _fake_answer(question), draft_msg

    monkeypatch.setattr(a2a_module.RetrieverAgent, "draft", _draft)
    monkeypatch.setattr(a2a_module.VerifierAgent, "verify", lambda self, msg: revise_msg)

    result = run_a2a(index=object(), question="what is the revenue?")

    assert result.accepted is False
    assert result.rounds == 2
    assert len(draft_calls) == 2
    assert draft_calls[0] is None
    assert draft_calls[1] == "try again"


def test_run_a2a_feedback_prefers_explicit_feedback_over_reason(monkeypatch, settings_override):
    settings_override(a2a_module, verify_max_retries=1)

    draft_msg = AgentMessage(sender="retriever", kind="draft", payload={})
    revise_msg = AgentMessage(
        sender="verifier",
        kind="verdict",
        payload={"verdict": "revise", "reason": "reason text", "feedback": "feedback text"},
    )

    seen_feedback = []

    def _draft(self, question, feedback=None):
        seen_feedback.append(feedback)
        return _fake_answer(question), draft_msg

    monkeypatch.setattr(a2a_module.RetrieverAgent, "draft", _draft)
    monkeypatch.setattr(a2a_module.VerifierAgent, "verify", lambda self, msg: revise_msg)

    run_a2a(index=object(), question="q")

    assert seen_feedback[1] == "feedback text"
