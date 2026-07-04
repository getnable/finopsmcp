"""Tests for finops.recommendations.gcp_recommender.

The native GCP Recommender API is the deeper counterpart to the resource scanner.
These tests cover the money/duration math (units+nanos, normalize-to-month, sign),
the state filter (only ACTIVE), the envelope mapping (measured idle vs inferred
committed-use), and the aggregation/error-hint report shape. The single SDK seam
(_list_recommendations) is patched so no google-cloud-recommender install is needed.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from finops.recommendations import gcp_recommender as mod
from finops.recommendations.gcp_recommender import (
    RECOMMENDERS,
    _duration_seconds,
    _monthly_savings,
    _money_units,
    _state_name,
    get_gcp_recommendations,
)

MOD = "finops.recommendations.gcp_recommender"
IDLE_VM = "google.compute.instance.IdleResourceRecommender"
MACHINE = "google.compute.instance.MachineTypeRecommender"
CUD = "google.compute.commitment.UsageCommitmentRecommender"


# ── stand-ins shaped like the proto ────────────────────────────────────────────


def _money(units, nanos=0, currency="USD"):
    return SimpleNamespace(units=units, nanos=nanos, currency_code=currency)


def _duration(seconds):
    return SimpleNamespace(seconds=seconds, nanos=0)


def _rec(name, monthly_cost=None, duration_secs=30 * 86400, state="ACTIVE",
         description="", subtype="", currency="USD", cost=None):
    """A recommendation stand-in. monthly_cost is the (negative for savings) cost
    over duration_secs; pass cost directly for the non-savings / missing cases."""
    if cost is None and monthly_cost is not None:
        cost = _money(int(monthly_cost), 0, currency)
    proj = None
    if cost is not None:
        proj = SimpleNamespace(cost=cost, duration=_duration(duration_secs))
    impact = SimpleNamespace(cost_projection=proj)
    state_info = SimpleNamespace(state=SimpleNamespace(name=state))
    return SimpleNamespace(
        name=name, primary_impact=impact, state_info=state_info,
        description=description, recommender_subtype=subtype,
    )


def _client(projects=("proj-alpha",)):
    return SimpleNamespace(project_ids=lambda: list(projects))


def _run(recs_by_recommender, client=None, **kw):
    """recs_by_recommender: {recommender_id: [rec, ...]}."""
    def _fake(project, recommender):
        return list(recs_by_recommender.get(recommender, []))
    with patch(f"{MOD}._list_recommendations", side_effect=_fake):
        return asyncio.run(get_gcp_recommendations(client or _client(), **kw))


# ── money + duration math ───────────────────────────────────────────────────────


def test_money_units_combines_units_and_nanos():
    assert _money_units(_money(12, 500_000_000)) == 12.5
    assert _money_units(None) == 0.0


def test_duration_seconds_defaults_to_a_month_when_missing_or_zero():
    month = 30 * 86400
    assert _duration_seconds(None) == month
    assert _duration_seconds(_duration(0)) == month
    assert _duration_seconds(_duration(7 * 86400)) == 7 * 86400


def test_monthly_savings_negates_and_normalizes_to_month():
    # -$100 over a 30-day window -> $100/mo saving.
    rec = _rec("r1", monthly_cost=-100, duration_secs=30 * 86400)
    monthly, currency = _monthly_savings(rec)
    assert monthly == 100.0
    assert currency == "USD"


def test_monthly_savings_normalizes_a_multi_year_commitment():
    # A CUD projects -$3600 over 3 years; per 30-day month that's ~$98.63.
    three_years = 3 * 365 * 86400
    rec = _rec("cud", monthly_cost=-3600, duration_secs=three_years)
    monthly, _ = _monthly_savings(rec)
    assert 95 < monthly < 102  # 3600 * (30/1095) ≈ 98.6


def test_monthly_savings_zero_when_cost_not_a_saving():
    # Positive cost (a spend-increasing / reliability rec) is not a saving.
    assert _monthly_savings(_rec("r", monthly_cost=50))[0] == 0.0
    # Missing cost projection entirely.
    assert _monthly_savings(_rec("r", cost=None))[0] == 0.0


def test_state_name_reads_active_and_permits_unlabelled():
    assert _state_name(_rec("r", monthly_cost=-10, state="ACTIVE")) == "ACTIVE"
    assert _state_name(_rec("r", monthly_cost=-10, state="DISMISSED")) == "DISMISSED"
    assert _state_name(SimpleNamespace(state_info=None)) == "ACTIVE"


# ── report behaviour ─────────────────────────────────────────────────────────────


def test_active_savings_recommendation_becomes_a_finding():
    recs = {IDLE_VM: [_rec(
        "projects/proj-alpha/locations/us-central1-a/recommenders/x/recommendations/abc",
        monthly_cost=-40, description="Delete idle VM 'web-old'",
    )]}
    out = _run(recs)
    assert out["total_findings"] == 1
    f = out["findings"][0]
    assert f["category"] == "idle_vm"
    assert f["estimated_monthly_savings"] == 40.0
    assert f["resource_id"] == "abc"
    # Idle is measured evidence -> a recommendation, savings preserved.
    assert f["finding"]["kind"] == "recommendation"
    assert f["finding"]["est_monthly_savings"] == 40.0
    assert out["total_estimated_annual_savings"] == 480.0


def test_committed_use_is_an_investigation_not_a_recommendation():
    recs = {CUD: [_rec(
        "projects/proj-alpha/locations/global/recommenders/x/recommendations/cud1",
        monthly_cost=-1200, duration_secs=365 * 86400,
        description="Purchase a 1-year committed use discount",
    )]}
    out = _run(recs)
    f = out["findings"][0]
    assert f["category"] == "committed_use"
    # Inferred evidence: envelope forces it to an investigation with no $ on the
    # envelope, though the raw finding keeps the estimate for ranking.
    assert f["finding"]["kind"] == "investigation"
    assert f["finding"]["est_monthly_savings"] is None
    assert f["estimated_monthly_savings"] > 0


def test_non_active_recommendations_are_skipped():
    recs = {IDLE_VM: [
        _rec("a", monthly_cost=-30, state="DISMISSED"),
        _rec("b", monthly_cost=-30, state="SUCCEEDED"),
        _rec("c", monthly_cost=-30, state="ACTIVE"),
    ]}
    out = _run(recs)
    assert out["total_findings"] == 1
    assert out["findings"][0]["resource_id"] == "c"


def test_zero_saving_recommendations_are_dropped():
    recs = {MACHINE: [
        _rec("a", monthly_cost=50),      # cost increase, not a saving
        _rec("b", cost=None),            # no projection
    ]}
    out = _run(recs)
    assert out["total_findings"] == 0


def test_findings_sorted_and_aggregated_by_bucket():
    recs = {
        IDLE_VM: [_rec("v", monthly_cost=-20, description="idle vm")],
        MACHINE: [_rec("m", monthly_cost=-600, description="rightsize")],
    }
    out = _run(recs)
    # Sorted by savings desc.
    assert [f["estimated_monthly_savings"] for f in out["findings"]] == [600.0, 20.0]
    assert out["total_estimated_monthly_savings"] == 620.0
    assert out["by_category"]["vm_rightsizing"]["monthly_savings"] == 600.0
    assert out["by_severity"]["high"]["count"] == 1   # 600 -> high
    assert out["by_severity"]["low"]["count"] == 1    # 20 -> low
    assert out["by_project"]["proj-alpha"]["count"] == 2


def test_invalid_project_ids_rejected():
    out = _run({}, client=SimpleNamespace(project_ids=lambda: ["Bad_Project!"]))
    assert "error" in out


def test_explicit_recommenders_filter_is_honoured():
    recs = {IDLE_VM: [_rec("v", monthly_cost=-20)],
            MACHINE: [_rec("m", monthly_cost=-600)]}
    calls = []

    def _fake(project, recommender):
        calls.append(recommender)
        return list(recs.get(recommender, []))

    with patch(f"{MOD}._list_recommendations", side_effect=_fake):
        out = asyncio.run(get_gcp_recommendations(_client(), recommenders=[IDLE_VM]))
    assert calls == [IDLE_VM]
    assert out["recommenders_run"] == [IDLE_VM]
    assert out["total_findings"] == 1


def test_all_errored_yields_setup_hint():
    def _boom(project, recommender):
        raise RuntimeError("PermissionDenied: recommender.recommendations.list")

    with patch(f"{MOD}._list_recommendations", side_effect=_boom):
        out = asyncio.run(get_gcp_recommendations(_client()))
    assert out["total_findings"] == 0
    assert len(out["errors"]) == len(RECOMMENDERS)
    assert "setup_hint" in out
    assert "recommender.viewer" in out["setup_hint"]


def test_one_recommender_error_does_not_sink_the_rest():
    good = {IDLE_VM: [_rec("v", monthly_cost=-20)]}

    def _fake(project, recommender):
        if recommender == MACHINE:
            raise RuntimeError("API not enabled")
        return list(good.get(recommender, []))

    with patch(f"{MOD}._list_recommendations", side_effect=_fake):
        out = asyncio.run(get_gcp_recommendations(_client()))
    assert out["total_findings"] == 1              # the good one survived
    assert any(e["recommender"] == MACHINE for e in out["errors"])
    assert "setup_hint" not in out                 # not a total failure


def test_every_recommender_has_remediation():
    """A finding must never surface without a confirm-first next step."""
    for rid, meta in RECOMMENDERS.items():
        assert meta["category"] in mod._REMEDIATION, f"no remediation for {rid}"
        assert mod._REMEDIATION[meta["category"]], f"empty remediation for {rid}"
