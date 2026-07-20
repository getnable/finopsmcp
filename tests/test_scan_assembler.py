"""Unit tests for the unified-scan assembler (scan v2).

The load-bearing guarantee is free-by-default: without --spend, the gather may
touch only free signals. The CRITICAL test asserts the AI path is invoked with
exclude_cloud_native=True, which is exactly what keeps Bedrock's Cost Explorer
calls (and Vertex's BigQuery query) off the default path. If that ever regresses,
a free tool would start charging the user's own cloud account.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import finops.scan_assembler as sa


def _llm_sample(**over):
    d = {
        "total_usd": 13300.0,
        "by_provider": {"openai": 9200.0, "anthropic": 4100.0},
        "recommendations": [{"idea": "route 40% of gpt-4 to a cheaper model"}],
    }
    d.update(over)
    return d


# ── CRITICAL: free-by-default ────────────────────────────────────────────────

def test_default_ai_path_excludes_cloud_native():
    calls = {}

    def _fake(**kw):
        calls.update(kw)
        return _llm_sample()

    with patch("finops.connectors.llm_costs.get_all_llm_costs", side_effect=_fake):
        blocks, abandoned = sa.gather_extra_providers(frozenset({"llm"}), spend=False)

    # The whole free-by-default guarantee at the AI layer: no Bedrock/Vertex legs.
    assert calls.get("exclude_cloud_native") is True
    ai = [b for b in blocks if b.family == "ai"][0]
    assert ai.status == "ok"
    assert ai.spend_usd == 13300.0
    assert ai.estimated is True            # AI spend is always [estimated] in v2
    assert ai.early_recoverable is True    # AI recoverable stays [early]
    assert abandoned is False


def test_spend_ai_path_includes_cloud_native():
    calls = {}

    def _fake(**kw):
        calls.update(kw)
        return _llm_sample()

    with patch("finops.connectors.llm_costs.get_all_llm_costs", side_effect=_fake):
        sa.gather_extra_providers(frozenset({"llm"}), spend=True)

    assert calls.get("exclude_cloud_native") is False


def test_empty_ai_block_is_dropped():
    # "llm" is in connected_families for every AWS user; an empty AI result must
    # not render (keeps AWS-only output byte-identical to v1).
    with patch("finops.connectors.llm_costs.get_all_llm_costs",
               side_effect=lambda **kw: _llm_sample(total_usd=0.0, by_provider={})):
        blocks, _ = sa.gather_extra_providers(frozenset({"llm"}), spend=False)
    assert blocks == []


def test_aws_only_families_gather_nothing():
    # "aws" is not an extra-provider gatherer; no work, no blocks.
    blocks, abandoned = sa.gather_extra_providers(frozenset({"aws"}), spend=False)
    assert blocks == [] and abandoned is False


# ── GCP / Azure extraction ───────────────────────────────────────────────────

class _FakeGCP:
    """Stand-in GCPConnector: only is_configured() is exercised by the assembler."""
    def __init__(self, configured=True):
        self._configured = configured

    async def is_configured(self):
        return self._configured


def test_gcp_recoverable_extracted():
    async def _fake_engine(client, *a, **k):
        return {"total_monthly_savings_usd": 410.0,
                "findings": [{"estimated_monthly_savings_usd": 410.0}]}

    with patch("finops.connectors.gcp.GCPConnector", lambda: _FakeGCP(True)), \
         patch("finops.recommendations.gcp_waste.audit_gcp_waste", side_effect=_fake_engine):
        blocks, _ = sa.gather_extra_providers(frozenset({"gcp"}), spend=False)
    gcp = [b for b in blocks if b.family == "gcp"][0]
    assert gcp.status == "ok"
    assert gcp.recoverable_usd == 410.0


def test_azure_spend_and_recoverable_free():
    def _fake_cost(*a, **k):
        return {"total_cost_usd": 6300.0, "by_dimension": [{"name": "VMs", "cost_usd": 4000.0}]}

    def _fake_adv(*a, **k):
        return {"total_monthly_savings_usd": 220.0}

    with patch("finops.connectors.azure_optimize.get_cost_by_dimension", side_effect=_fake_cost), \
         patch("finops.connectors.azure_optimize.get_advisor_cost_recommendations", side_effect=_fake_adv):
        blocks, _ = sa.gather_extra_providers(frozenset({"azure"}), spend=False)
    az = [b for b in blocks if b.family == "azure"][0]
    assert az.status == "ok"
    assert az.spend_usd == 6300.0          # Azure Cost Management is free -> shows by default
    assert az.recoverable_usd == 220.0


# ── per-provider failure isolation ───────────────────────────────────────────

def test_one_provider_error_does_not_sink_the_others():
    async def _boom(client, *a, **k):
        raise RuntimeError("gcp billing export not enabled")

    with patch("finops.connectors.llm_costs.get_all_llm_costs", side_effect=lambda **kw: _llm_sample()), \
         patch("finops.connectors.gcp.GCPConnector", lambda: _FakeGCP(True)), \
         patch("finops.recommendations.gcp_waste.audit_gcp_waste", side_effect=_boom):
        blocks, _ = sa.gather_extra_providers(frozenset({"llm", "gcp"}), spend=False)

    fam = {b.family: b for b in blocks}
    assert fam["ai"].status == "ok"          # the good provider still renders
    assert fam["gcp"].status == "errored"    # the bad one degrades to a note
    assert fam["gcp"].note


def test_gcp_not_connected_is_auth_note():
    with patch("finops.connectors.gcp.GCPConnector", lambda: _FakeGCP(False)):
        blocks, _ = sa.gather_extra_providers(frozenset({"gcp"}), spend=False)
    gcp = [b for b in blocks if b.family == "gcp"][0]
    assert gcp.status == "auth_failed"


def test_spend_grand_total_dedups_cloud_native_ai():
    # Under --spend, Bedrock is in BOTH the AWS spend total and the AI block, so
    # the grand "visible" total must subtract it once: 43,110 (AWS) + 14,300 (AI)
    # - 5,100 (Bedrock) = 52,310, never 57,410.
    import io as _io
    from finops import cli_scan

    spend = {"total": 43110.0, "services": [("EC2", 11400.0)], "period": "MTD"}
    report = {"total_estimated_monthly_savings": 2140.0, "findings": [],
              "regions_scanned": ["us-east-1"], "regions_timed_out": []}
    ai = sa.ProviderBlock(
        family="ai", label="AI & GPU", status="ok", spend_usd=14300.0,
        estimated=True, by_provider={"openai": 9200.0, "bedrock": 5100.0})

    buf = _io.StringIO()
    cli_scan._render(buf, spend, report, demo=False, ce_denied=False, extra_blocks=[ai])
    out = buf.getvalue()
    assert "$52,310" in out          # deduped grand total
    assert "$57,410" not in out      # the double-counted total must not appear


def test_timeout_marks_abandoned_and_notes():
    async def _slow(client, *a, **k):
        await asyncio.sleep(2.0)
        return {"total_monthly_savings_usd": 1.0}

    with patch("finops.connectors.gcp.GCPConnector", lambda: _FakeGCP(True)), \
         patch("finops.recommendations.gcp_waste.audit_gcp_waste", side_effect=_slow):
        blocks, abandoned = sa.gather_extra_providers(
            frozenset({"gcp"}), spend=False, per_provider_timeout=0.4, overall_budget=1.0)
    gcp = [b for b in blocks if b.family == "gcp"][0]
    assert gcp.status == "timeout"
    assert abandoned is True   # a lingering worker thread -> caller hard-exits
