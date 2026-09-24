"""Tests for finops.recommendations.bedrock_routing."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from finops.llm_prices import MODEL_PRICES
from finops.recommendations import bedrock_routing
from finops.recommendations.bedrock_routing import (
    _cost_per_invocation,
    _normalize_model_id,
    _parse_model_from_usage_type,
    recommend_bedrock_model_routing,
)


# ── unit: _parse_model_from_usage_type ───────────────────────────────────────

def test_parse_usage_type_strips_region_prefix():
    usage = "USE1-anthropic.claude-3-5-sonnet-20241022:input-tokens"
    result = _parse_model_from_usage_type(usage)
    assert "use1" not in result
    assert "sonnet" in result


def test_parse_usage_type_strips_token_suffix():
    usage = "USE1-anthropic.claude-3-haiku-20240307:output-tokens"
    result = _parse_model_from_usage_type(usage)
    assert "output-tokens" not in result
    assert "haiku" in result


def test_parse_usage_type_no_colon():
    usage = "USW2-anthropic.claude-3-5-sonnet-20241022"
    result = _parse_model_from_usage_type(usage)
    assert "sonnet" in result


# ── unit: _normalize_model_id ─────────────────────────────────────────────────

def test_normalize_sonnet_returns_canonical():
    raw = "anthropic.claude-3-5-sonnet-20241022"
    result = _normalize_model_id(raw)
    assert "sonnet" in result


def test_normalize_haiku_returns_canonical():
    raw = "anthropic.claude-3-haiku-20240307"
    result = _normalize_model_id(raw)
    assert "haiku" in result


def test_normalize_unknown_returns_raw():
    raw = "some-unknown-model-v9"
    result = _normalize_model_id(raw)
    assert result == raw


def test_normalize_opus_returns_canonical():
    raw = "anthropic.claude-opus-4-20250514"
    result = _normalize_model_id(raw)
    assert "opus" in result


# ── unit: prices come from llm_prices ────────────────────────────────────────

def test_the_module_keeps_no_price_table_of_its_own():
    assert not hasattr(bedrock_routing, "MODEL_PRICING")


def test_haiku_cheaper_than_sonnet():
    assert MODEL_PRICES["claude-3-5-haiku"].input < MODEL_PRICES["claude-sonnet-4-6"].input
    assert MODEL_PRICES["claude-3-5-haiku"].output < MODEL_PRICES["claude-sonnet-4-6"].output


@pytest.mark.parametrize("raw,canonical", [
    ("anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5"),
    ("Claude Sonnet 4.6", "claude-sonnet-4-6"),
    ("anthropic.claude-3-5-haiku-20241022-v1:0", "claude-3-5-haiku"),
    ("claude-haiku-3-5", "claude-3-5-haiku"),
    ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
    ("anthropic.claude-opus-4-8", "claude-opus-4-8"),
    ("anthropic.claude-opus-4-20250514-v1:0", "claude-opus-4"),
])
def test_normalize_resolves_to_the_shared_table(raw, canonical):
    assert _normalize_model_id(raw) == canonical


def test_haiku_4_5_is_priced_as_itself_not_claude_3_haiku():
    # The family fallback sent every unrecognised "haiku" to Claude 3 Haiku's
    # $0.25/$1.25, a quarter of Haiku 4.5's $1/$5.
    model = _normalize_model_id("anthropic.claude-haiku-4-5-20251001-v1:0")
    assert _cost_per_invocation(model, 1_000_000, 1_000_000) == pytest.approx(6.00)


def test_opus_4_5_and_later_are_not_priced_as_opus_4():
    # And every "opus" to Opus 4's $15/$75, three times Opus 4.5 and later.
    for raw in ("anthropic.claude-opus-4-8", "Claude Opus 4.5", "claude-opus-5"):
        assert _cost_per_invocation(_normalize_model_id(raw), 1_000_000, 1_000_000) == 30.0
    assert _cost_per_invocation(_normalize_model_id("claude-opus-4-20250514"),
                                1_000_000, 1_000_000) == 90.0


def test_a_model_without_a_confirmed_price_is_not_given_a_siblings():
    # Claude 3.5 Sonnet is not on the current pricing page. It stays itself.
    raw = "anthropic.claude-3-5-sonnet-20241022"
    assert _normalize_model_id(raw) == raw
    assert _cost_per_invocation(raw, 1_000_000, 1_000_000) == 0.0


# ── unit: _cost_per_invocation ────────────────────────────────────────────────

def test_cost_per_invocation_sonnet_higher_than_haiku():
    sonnet_cost = _cost_per_invocation("claude-sonnet-4-6", avg_input_tokens=300, avg_output_tokens=100)
    haiku_cost = _cost_per_invocation("claude-haiku-3-5", avg_input_tokens=300, avg_output_tokens=100)
    assert sonnet_cost > haiku_cost


def test_cost_per_invocation_unknown_model_returns_zero():
    cost = _cost_per_invocation("unknown-model-xyz", avg_input_tokens=500, avg_output_tokens=200)
    assert cost == 0.0


def test_cost_per_invocation_formula():
    # claude-haiku-4-5: $1 input, $5 output per 1M tokens
    # 100 input tokens, 50 output tokens
    # (100 * 1 + 50 * 5) / 1_000_000 = 350 / 1_000_000 = 0.00035
    cost = _cost_per_invocation("claude-haiku-4-5", avg_input_tokens=100, avg_output_tokens=50)
    expected = (100 * 1.00 + 50 * 5.00) / 1_000_000
    assert abs(cost - expected) < 1e-10


# ── integration helpers ───────────────────────────────────────────────────────

def _ce_side_effect(usage_type: str, amount: float, service: str = "Amazon Bedrock"):
    """CE side_effect answering SERVICE discovery and SERVICE+USAGE_TYPE detail,
    matching the discover-then-filter query the routing code now issues."""
    def _se(**kw):
        keys = [g["Key"] for g in kw.get("GroupBy", [])]
        if keys == ["SERVICE"]:
            return {"ResultsByTime": [{"Groups": [
                {"Keys": [service], "Metrics": {"UnblendedCost": {"Amount": str(amount)}}}
            ]}]}
        return {"ResultsByTime": [{"Groups": [
            {"Keys": [service, usage_type], "Metrics": {"UnblendedCost": {"Amount": str(amount)}}}
        ]}]}
    return _se


def _cw_publishing(*model_ids: str) -> MagicMock:
    """A CloudWatch client that lists these ModelId dimension values under
    AWS/Bedrock and answers get_metric_statistics only for them."""
    cw = MagicMock()
    cw.get_paginator.return_value.paginate.return_value = [{"Metrics": [
        {"Namespace": "AWS/Bedrock", "MetricName": "Invocations",
         "Dimensions": [{"Name": "ModelId", "Value": m}]} for m in model_ids]}]
    return cw


def _only_for(model_ids, answer):
    """Wrap a metric side effect so it answers only when asked by a published id."""
    def side(Namespace, MetricName, Dimensions, **kw):
        assert Namespace == "AWS/Bedrock"
        [dim] = Dimensions
        if dim["Name"] != "ModelId" or dim["Value"] not in model_ids:
            return {"Datapoints": []}
        return answer(Namespace=Namespace, MetricName=MetricName, **kw)
    return side


def _make_cw_metric_response(value: float) -> dict:
    return {
        "Datapoints": [{"Sum": value}]
    }


# ── integration: no Bedrock spend ────────────────────────────────────────────

def test_recommend_returns_empty_when_no_spend():
    with patch("finops.recommendations.bedrock_routing._make_ce") as mock_ce_fn, \
         patch("finops.recommendations.bedrock_routing._make_cw"):
        ce = MagicMock()
        ce.get_cost_and_usage.return_value = {"ResultsByTime": [{"Groups": []}]}
        mock_ce_fn.return_value = ce

        result = recommend_bedrock_model_routing(days=30)

    assert result["models_in_use"] == []
    assert result["routing_opportunities"] == []
    assert result["total_monthly_savings"] == 0.0


# ── integration: short-task Sonnet usage flags routing opportunity ─────────────

def test_recommend_flags_short_task_sonnet():
    with patch("finops.recommendations.bedrock_routing._make_ce") as mock_ce_fn, \
         patch("finops.recommendations.bedrock_routing._make_cw") as mock_cw_fn:

        ce = MagicMock()
        ce.get_cost_and_usage.side_effect = _ce_side_effect(
            "USE1-anthropic.claude-sonnet-4-5-20250929-v1:0:input-tokens",
            300.0,
        )
        mock_ce_fn.return_value = ce

        published = ("anthropic.claude-sonnet-4-5-20250929-v1:0",)
        cw = _cw_publishing(*published)
        # Avg tokens: 200 input, 80 output = short task
        def _metric_side(Namespace, MetricName, **kw):
            if MetricName == "Invocations":
                return _make_cw_metric_response(5000.0)
            if MetricName == "InputTokenCount":
                return _make_cw_metric_response(5000 * 200)  # avg 200 tokens
            if MetricName == "OutputTokenCount":
                return _make_cw_metric_response(5000 * 80)   # avg 80 tokens
            return {"Datapoints": []}
        cw.get_metric_statistics.side_effect = _only_for(published, _metric_side)
        mock_cw_fn.return_value = cw

        result = recommend_bedrock_model_routing(days=30)

    assert len(result["routing_opportunities"]) > 0
    opp = result["routing_opportunities"][0]
    assert "haiku" in opp["recommended_model"]
    assert opp["monthly_savings"] > 0
    assert opp["projected_monthly_cost"] < opp["current_monthly_cost"]


# ── integration: an unpriced source model is not sized ────────────────────────

def test_recommend_skips_a_source_model_it_cannot_price():
    # Short-task Claude 3.5 Sonnet traffic used to be sized at Sonnet 4.5's rate
    # through the family fallback. With no confirmed price there is nothing to
    # size the saving against, so no dollar figure is invented for it.
    with patch("finops.recommendations.bedrock_routing._make_ce") as mock_ce_fn, \
         patch("finops.recommendations.bedrock_routing._make_cw") as mock_cw_fn:
        ce = MagicMock()
        ce.get_cost_and_usage.side_effect = _ce_side_effect(
            "USE1-anthropic.claude-3-5-sonnet-20241022:input-tokens", 300.0)
        mock_ce_fn.return_value = ce
        cw = MagicMock()
        cw.get_metric_statistics.return_value = _make_cw_metric_response(5000.0)
        mock_cw_fn.return_value = cw

        result = recommend_bedrock_model_routing(days=30)

    assert result["models_in_use"][0]["model_id"] == "anthropic.claude-3-5-sonnet-20241022"
    assert result["routing_opportunities"] == []


# ── integration: high-token Sonnet usage skipped (complex reasoning) ──────────

def test_recommend_skips_complex_reasoning_tasks():
    with patch("finops.recommendations.bedrock_routing._make_ce") as mock_ce_fn, \
         patch("finops.recommendations.bedrock_routing._make_cw") as mock_cw_fn:

        ce = MagicMock()
        ce.get_cost_and_usage.side_effect = _ce_side_effect(
            "USE1-anthropic.claude-sonnet-4-5-20250929-v1:0:input-tokens",
            500.0,
        )
        mock_ce_fn.return_value = ce

        published = ("anthropic.claude-sonnet-4-5-20250929-v1:0",)
        cw = _cw_publishing(*published)
        # avg 3000 input tokens = complex reasoning, should NOT route
        def _metric_side(Namespace, MetricName, **kw):
            if MetricName == "Invocations":
                return _make_cw_metric_response(200.0)
            if MetricName == "InputTokenCount":
                return _make_cw_metric_response(200 * 3000)
            if MetricName == "OutputTokenCount":
                return _make_cw_metric_response(200 * 1500)
            return {"Datapoints": []}
        cw.get_metric_statistics.side_effect = _only_for(published, _metric_side)
        mock_cw_fn.return_value = cw

        result = recommend_bedrock_model_routing(days=30)

    # No routing opportunity because avg input > 2000 tokens
    assert result["routing_opportunities"] == []


# ── integration: Haiku-only usage produces no routing opportunity ─────────────

def test_recommend_no_opportunity_for_haiku_only():
    with patch("finops.recommendations.bedrock_routing._make_ce") as mock_ce_fn, \
         patch("finops.recommendations.bedrock_routing._make_cw") as mock_cw_fn:

        ce = MagicMock()
        ce.get_cost_and_usage.side_effect = _ce_side_effect(
            "USE1-anthropic.claude-haiku-3-20240307:input-tokens",
            50.0,
        )
        mock_ce_fn.return_value = ce

        cw = MagicMock()
        cw.get_metric_statistics.return_value = _make_cw_metric_response(0.0)
        mock_cw_fn.return_value = cw

        result = recommend_bedrock_model_routing(days=30)

    # Haiku is not a routing source
    assert result["routing_opportunities"] == []
    assert result["total_monthly_savings"] == 0.0


# ── integration: output schema completeness ───────────────────────────────────

def test_recommend_output_has_all_required_keys():
    with patch("finops.recommendations.bedrock_routing._make_ce") as mock_ce_fn, \
         patch("finops.recommendations.bedrock_routing._make_cw"):
        ce = MagicMock()
        ce.get_cost_and_usage.return_value = {"ResultsByTime": [{"Groups": []}]}
        mock_ce_fn.return_value = ce

        result = recommend_bedrock_model_routing(days=30)

    required_keys = {
        "models_in_use",
        "routing_opportunities",
        "total_monthly_savings",
        "implementation_note",
    }
    assert required_keys.issubset(set(result.keys()))


# ── integration: savings calculation sanity check ────────────────────────────

def test_routing_savings_are_positive_and_bounded():
    with patch("finops.recommendations.bedrock_routing._make_ce") as mock_ce_fn, \
         patch("finops.recommendations.bedrock_routing._make_cw") as mock_cw_fn:

        ce = MagicMock()
        ce.get_cost_and_usage.side_effect = _ce_side_effect(
            "USE1-anthropic.claude-sonnet-4-5-20250929-v1:0:input-tokens",
            200.0,
        )
        mock_ce_fn.return_value = ce

        cw = MagicMock()
        def _metric_side(Namespace, MetricName, **kw):
            if MetricName == "Invocations":
                return _make_cw_metric_response(2000.0)
            if MetricName == "InputTokenCount":
                return _make_cw_metric_response(2000 * 100)
            if MetricName == "OutputTokenCount":
                return _make_cw_metric_response(2000 * 50)
            return {"Datapoints": []}
        cw.get_metric_statistics.side_effect = _metric_side
        mock_cw_fn.return_value = cw

        result = recommend_bedrock_model_routing(days=30)

    assert result["total_monthly_savings"] >= 0.0
    if result["routing_opportunities"]:
        opp = result["routing_opportunities"][0]
        assert opp["monthly_savings"] >= 0.0
        assert opp["projected_monthly_cost"] >= 0.0
        assert opp["eligible_invocations_pct"] > 0


# ── the ModelId CloudWatch actually publishes under ───────────────────────────

def test_metrics_are_read_by_the_bedrock_model_id_across_profiles():
    """CloudWatch keys AWS/Bedrock by the Bedrock model id, and a model used
    through a cross-region inference profile publishes under that id too.
    Reading by the canonical id returned nothing, so routing never fired."""
    published = ("anthropic.claude-sonnet-4-5-20250929-v1:0",
                 "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                 "anthropic.claude-haiku-4-5-20251001-v1:0")
    with patch("finops.recommendations.bedrock_routing._make_ce") as mock_ce_fn, \
         patch("finops.recommendations.bedrock_routing._make_cw") as mock_cw_fn:
        ce = MagicMock()
        ce.get_cost_and_usage.side_effect = _ce_side_effect(
            "USE1-anthropic.claude-sonnet-4-5-20250929-v1:0:input-tokens", 300.0)
        mock_ce_fn.return_value = ce
        cw = _cw_publishing(*published)

        def _metric_side(Namespace, MetricName, **kw):
            per_id = {"Invocations": 2500.0, "InputTokenCount": 2500 * 200.0,
                      "OutputTokenCount": 2500 * 80.0}
            return _make_cw_metric_response(per_id[MetricName])
        cw.get_metric_statistics.side_effect = _only_for(published[:2], _metric_side)
        mock_cw_fn.return_value = cw

        result = recommend_bedrock_model_routing(days=30)

    asked = {c.kwargs["Dimensions"][0]["Value"] for c in cw.get_metric_statistics.call_args_list}
    assert asked == set(published[:2])          # both Sonnet ids, not Haiku, not "claude-sonnet-4-5"
    [sonnet] = [m for m in result["models_in_use"] if m["model_id"] == "claude-sonnet-4-5"]
    assert sonnet["invocation_count"] == 5000   # summed over the on-demand and profile ids
    assert result["routing_opportunities"]


def test_a_failed_listing_falls_back_to_the_model_key():
    cw = MagicMock()
    cw.get_paginator.side_effect = RuntimeError("AccessDenied")
    assert bedrock_routing._bedrock_model_dimensions(cw) is None
