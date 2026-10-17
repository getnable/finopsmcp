"""Unit tests for the unified-scan assembler (scan v2).

The load-bearing guarantee is free-by-default: without --spend, the gather may
touch only free signals. The CRITICAL test asserts the AI path is invoked with
exclude_cloud_native=True, which is exactly what keeps Bedrock's Cost Explorer
calls (and Vertex's BigQuery query) off the default path. If that ever regresses,
a free tool would start charging the user's own cloud account.
"""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import pytest

import finops.scan_assembler as sa


@pytest.fixture(autouse=True)
def _no_worker_outlives_its_test():
    """A provider abandoned on timeout keeps its pool worker, which is not a
    daemon thread. `nable scan`'s Ctrl-C and crash paths hard-exit when such a
    thread is alive (cli_scan._threads_lingering), so a worker left running
    here made tests/test_cli_scan_says_what_happened.py's Ctrl-C test call
    os._exit(130) and end the whole run when it came next. A test that hangs a
    provider must release it before it ends."""
    before = set(threading.enumerate())
    yield
    left = [t for t in threading.enumerate() if t not in before and not t.daemon]
    for t in left:
        t.join(timeout=1.0)
    alive = [t.name for t in left if t.is_alive()]
    assert not alive, f"worker thread(s) still running after the test: {alive}"


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


_ANTHROPIC_401 = ("credential_invalid: Anthropic refused ANTHROPIC_ADMIN_KEY on the usage "
                  "and cost reports (Client error '401 Unauthorized')")


def test_an_ai_provider_that_could_not_be_read_is_named():
    """A fixture account with OpenAI ($600), Anthropic ($450) and a LiteLLM
    proxy connected, Anthropic's admin key refused: get_all_llm_costs said
    partial, failed_providers {"anthropic": ...}, "Not read: anthropic". The
    scan printed "AI & GPU $1,014/mo [estimated]  Openai $600 · Bedrock $414"
    with no word that Anthropic was not read."""
    sample = _llm_sample(total_usd=1014.25, by_provider={"openai": 600.0, "bedrock": 414.25},
                         failed_providers={"anthropic": _ANTHROPIC_401}, partial=True)
    with patch("finops.connectors.llm_costs.get_all_llm_costs", side_effect=lambda **kw: sample):
        blocks, _ = sa.gather_extra_providers(frozenset({"llm"}), spend=True)
    [ai] = [b for b in blocks if b.family == "ai"]
    assert ai.status == "ok" and ai.spend_usd == 1014.25
    assert "not read: anthropic" in (ai.note or ""), ai.note


def test_ai_providers_that_all_failed_are_not_dropped_as_empty():
    """Anthropic alone connected and refused: total 0 and nothing by provider,
    which the empty-block rule above dropped, so the scan showed no AI at all,
    the same as an account with none."""
    sample = _llm_sample(total_usd=0.0, by_provider={},
                         failed_providers={"anthropic": _ANTHROPIC_401}, partial=True)
    with patch("finops.connectors.llm_costs.get_all_llm_costs", side_effect=lambda **kw: sample):
        blocks, _ = sa.gather_extra_providers(frozenset({"llm"}), spend=False)
    [ai] = [b for b in blocks if b.family == "ai"]
    assert ai.status != "ok" and ai.spend_usd is None
    assert "anthropic" in (ai.note or ""), ai.note


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


def test_demo_blocks_mirror_free_spend_boundary():
    # Default demo AI shows usage-API only (no Bedrock); --spend demo AI includes it.
    free = {b.family: b for b in sa.demo_extra_blocks(spend=False)}
    paid = {b.family: b for b in sa.demo_extra_blocks(spend=True)}
    assert set(free) == {"ai", "gcp", "azure"}
    assert "bedrock" not in free["ai"].by_provider     # free path excludes cloud-native AI
    assert "bedrock" in paid["ai"].by_provider          # --spend includes it
    assert free["azure"].spend_usd and free["azure"].recoverable_usd  # Azure free both ways


def test_timeout_marks_abandoned_and_notes():
    release = threading.Event()

    async def _slow(client, *a, **k):
        # Hangs well past the timeout, until the test is done with it.
        for _ in range(200):
            if release.is_set():
                break
            await asyncio.sleep(0.01)
        return {"total_monthly_savings_usd": 1.0}

    with patch("finops.connectors.gcp.GCPConnector", lambda: _FakeGCP(True)), \
         patch("finops.recommendations.gcp_waste.audit_gcp_waste", side_effect=_slow):
        blocks, abandoned = sa.gather_extra_providers(
            frozenset({"gcp"}), spend=False, per_provider_timeout=0.4, overall_budget=1.0)
        release.set()
    gcp = [b for b in blocks if b.family == "gcp"][0]
    assert gcp.status == "timeout"
    assert abandoned is True   # a lingering worker thread -> caller hard-exits


def test_a_hung_provider_does_not_hold_the_scan_past_its_timeout():
    """The timeout used to fire on time and then wait anyway: leaving the
    `with ThreadPoolExecutor` block joined the hung worker, so the scan sat for
    as long as the provider hung. The call must return near the timeout."""
    import threading
    import time

    release = threading.Event()
    with patch.dict(sa._GATHERERS, {"llm": ("ai", lambda spend: release.wait(10))}):
        t0 = time.monotonic()
        blocks, abandoned = sa.gather_extra_providers(
            frozenset({"llm"}), spend=False, per_provider_timeout=0.5, overall_budget=0.5)
        took = time.monotonic() - t0
    release.set()
    assert took < 3.0, f"waited {took:.1f}s for a provider that timed out at 0.5s"
    assert [b.status for b in blocks] == ["timeout"] and abandoned is True

