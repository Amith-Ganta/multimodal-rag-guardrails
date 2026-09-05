"""Unit tests for src/gateway.py's LLMGateway.summary()/total_cost().

These pre-populate call_log directly with hand-built CallRecord instances, so
no LiteLLM call or API key is needed - summary()/total_cost() are pure
aggregations over call_log taken under the instance lock.
"""

from __future__ import annotations

from src.gateway import CallRecord, LLMGateway


def _record(**overrides) -> CallRecord:
    base = dict(
        model_requested="gpt-4o",
        model_served="gpt-4o",
        prompt_tokens=100,
        completion_tokens=50,
        latency_sec=1.0,
        cost_usd=0.01,
        cached=False,
        tag="test",
    )
    base.update(overrides)
    return CallRecord(**base)


def test_summary_on_empty_call_log():
    gw = LLMGateway()
    summary = gw.summary()
    assert summary["calls"] == 0
    assert summary["total_cost_usd"] == 0
    assert summary["total_prompt_tokens"] == 0
    assert summary["total_completion_tokens"] == 0
    assert summary["models_served"] == []


def test_total_cost_sums_cost_usd_across_calls():
    gw = LLMGateway()
    gw.call_log.append(_record(cost_usd=0.01))
    gw.call_log.append(_record(cost_usd=0.02))
    gw.call_log.append(_record(cost_usd=0.03))
    assert round(gw.total_cost(), 6) == 0.06


def test_summary_aggregates_tokens_and_latency():
    gw = LLMGateway()
    gw.call_log.append(_record(prompt_tokens=100, completion_tokens=50, latency_sec=2.0))
    gw.call_log.append(_record(prompt_tokens=200, completion_tokens=75, latency_sec=4.0))

    summary = gw.summary()
    assert summary["calls"] == 2
    assert summary["total_prompt_tokens"] == 300
    assert summary["total_completion_tokens"] == 125
    assert summary["total_latency_sec"] == 6.0
    assert summary["avg_latency_sec"] == 3.0


def test_summary_models_served_is_sorted_and_deduplicated():
    gw = LLMGateway()
    gw.call_log.append(_record(model_served="gpt-4o-mini"))
    gw.call_log.append(_record(model_served="gpt-4o"))
    gw.call_log.append(_record(model_served="gpt-4o"))

    summary = gw.summary()
    assert summary["models_served"] == ["gpt-4o", "gpt-4o-mini"]


def test_summary_includes_cached_calls_in_totals():
    gw = LLMGateway()
    gw.call_log.append(_record(cached=True, cost_usd=0.0))
    gw.call_log.append(_record(cached=False, cost_usd=0.05))

    summary = gw.summary()
    assert summary["calls"] == 2
    assert summary["total_cost_usd"] == 0.05
