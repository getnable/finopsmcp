"""Regression tests for the AI-cost tools a demo surfaced as broken.

1. get_bedrock_costs / recommend_bedrock_model_routing reported $0 Bedrock spend
   because the Cost Explorer query exact-matched SERVICE="Amazon Bedrock", while
   modern Claude-on-Bedrock spend bills under per-model SKU services like
   "Claude Sonnet 4.5 (Amazon Bedrock Edition)". Now both discover every Bedrock
   service name first, and attribute per-model SKUs by their service name.
2. The Textract env breakdown inflated the "unknown" bucket to N x the real total
   by accumulating across all N env tag keys. Now it returns one key's breakdown.
3. optimize_ai_spend's planner must import + run (a stale build once reported a
   missing module).
"""
import asyncio

import pytest


class _FakeCE:
    """Minimal Cost Explorer stub that answers SERVICE discovery and detail."""

    def __init__(self, discovery_services, detail_groups):
        self.discovery_services = discovery_services
        # detail_groups: list of (service, usage_type, amount)
        self.detail_groups = detail_groups

    def get_cost_and_usage(self, **kw):
        keys = [g["Key"] for g in kw.get("GroupBy", [])]
        has_filter = bool(kw.get("Filter"))
        # Discovery: grouped by SERVICE, unfiltered.
        if keys == ["SERVICE"] and not has_filter:
            return {"ResultsByTime": [{"Groups": [
                {"Keys": [s], "Metrics": {"UnblendedCost": {"Amount": "1.0"}}}
                for s in self.discovery_services
            ]}]}
        # Detail: grouped by SERVICE + USAGE_TYPE, filtered to discovered services.
        if keys == ["SERVICE", "USAGE_TYPE"]:
            return {"ResultsByTime": [{"Groups": [
                {"Keys": [s, u], "Metrics": {"UnblendedCost": {"Amount": str(a)}}}
                for (s, u, a) in self.detail_groups
            ]}]}
        return {"ResultsByTime": []}


# All spend lives under a per-model SKU service, NOT plain "Amazon Bedrock", so
# the old exact-match query would have reported $0.
_SKU = "Claude Sonnet 4.5 (Amazon Bedrock Edition)"
_SKU_DETAIL = [
    (_SKU, "USE1-InputTokenCount", 800.0),
    (_SKU, "USE1-OutputTokenCount", 200.0),
]


def test_bedrock_analyzer_includes_per_model_sku(monkeypatch):
    from finops.connectors.aws_services import bedrock as bd

    fake = _FakeCE(discovery_services=[_SKU], detail_groups=_SKU_DETAIL)
    monkeypatch.setattr(bd, "_make_ce", lambda role_arn=None: fake)

    out = bd.BedrockAnalyzer().get_costs(days=30)
    assert "No Amazon Bedrock spend" not in out
    assert "1,000.00" in out          # 800 + 200, would have been $0 before
    assert "Sonnet" in out            # attributed to the model via service name


def test_bedrock_routing_finds_per_model_sku_spend():
    from finops.recommendations import bedrock_routing as br

    fake = _FakeCE(discovery_services=[_SKU], detail_groups=_SKU_DETAIL)
    costs = br._get_bedrock_ce_costs(fake, "2026-05-01", "2026-06-01")

    assert costs, "routing found no Bedrock spend (regression)"
    total = sum(m["total_cost"] for m in costs.values())
    assert abs(total - 1000.0) < 0.01
    assert "claude-sonnet-4-5" in costs   # normalized from the SKU service name


def test_textract_env_breakdown_does_not_multiply_unknown():
    from finops.recommendations import textract_env as tx

    # Untagged Textract spend: every env tag-key query returns the full $5,000 as
    # an empty-tag (unknown) group. With 5 env tag keys, the old code summed to
    # $25,000; it must stay at the real $5,000.
    class _UntaggedCE:
        def get_cost_and_usage(self, **kw):
            return {"ResultsByTime": [{"Groups": [
                {"Keys": ["Environment$"], "Metrics": {"UnblendedCost": {"Amount": "5000.0"}}}
            ]}]}

    buckets = tx._get_tagged_env_breakdown(_UntaggedCE(), "2026-05-01", "2026-06-01")
    assert abs(buckets["unknown"] - 5000.0) < 0.01
    assert sum(buckets.values()) <= 5000.0 + 0.01


def test_textract_env_breakdown_uses_first_tagged_key():
    from finops.recommendations import textract_env as tx

    class _TaggedCE:
        def get_cost_and_usage(self, **kw):
            key = kw["GroupBy"][0]["Key"]
            if key == "Environment":      # first key carries real env tags
                return {"ResultsByTime": [{"Groups": [
                    {"Keys": ["Environment$prod"], "Metrics": {"UnblendedCost": {"Amount": "3000.0"}}},
                    {"Keys": ["Environment$qa"], "Metrics": {"UnblendedCost": {"Amount": "1000.0"}}},
                ]}]}
            return {"ResultsByTime": []}

    buckets = tx._get_tagged_env_breakdown(_TaggedCE(), "2026-05-01", "2026-06-01")
    assert abs(buckets["prod"] - 3000.0) < 0.01
    assert abs(buckets["qa"] - 1000.0) < 0.01
    assert buckets["unknown"] == 0.0


def test_optimize_ai_spend_planner_runs_on_demo_data():
    # The optimizer's import chain must resolve and run end-to-end on demo data
    # (the tool reported a missing-module crash in a stale build).
    from finops.demo_data import llm_costs, bedrock_split
    from finops.analytics.ai_optimizer import build_optimization_plan

    plan = build_optimization_plan(llm_costs(), days=30, bedrock_split=bedrock_split())
    assert isinstance(plan, dict)
    assert "addressable_savings_monthly_usd" in plan
