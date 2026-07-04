"""Tests for finops.recommendations.gcp_recommender.

These run against Google's REAL Recommender proto types (recommender_v1.Recommendation,
google.type.Money, protobuf Duration), not hand-rolled stand-ins. That is deliberate:
we have no GCP account with spend to dogfood against, so the next best confidence is a
contract test that fails the moment a field name or shape drifts from what the SDK
actually returns. It already earned its keep: the real API surfaces CostProjection.duration
as a datetime.timedelta, and reading `.seconds` on a multi-day span returns the intra-day
remainder (0), which would silently wreck every normalized savings figure. The fake
fixtures never caught that; these do.

google-cloud-recommender is in the [dev] extra so this runs in CI. importorskip keeps a
bare local checkout green.
"""
from __future__ import annotations

import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

recommender_v1 = pytest.importorskip("google.cloud.recommender_v1")
from google.protobuf import duration_pb2  # noqa: E402
from google.type import money_pb2  # noqa: E402

from finops.recommendations import gcp_recommender as mod  # noqa: E402
from finops.recommendations.gcp_recommender import (  # noqa: E402
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
MONTH = 30 * 86400


# ── real-proto builders ─────────────────────────────────────────────────────────


def _money(units, nanos=0, currency="USD"):
    return money_pb2.Money(currency_code=currency, units=units, nanos=nanos)


def _rec(name, monthly_cost="__unset__", duration_secs=MONTH, state="ACTIVE",
         description="", subtype="", currency="USD", cost="__default__",
         with_projection=True):
    """Build a real recommender_v1.Recommendation the way list_recommendations returns
    one. monthly_cost is the (negative for savings) cost over duration_secs; pass
    with_projection=False to model a recommendation that carries no cost projection."""
    if cost == "__default__":
        cost = (_money(int(monthly_cost), 0, currency)
                if monthly_cost not in ("__unset__", None) else None)

    impact_kwargs = {"category": recommender_v1.Impact.Category.COST}
    if with_projection and cost is not None:
        impact_kwargs["cost_projection"] = recommender_v1.CostProjection(
            cost=cost, duration=duration_pb2.Duration(seconds=duration_secs),
        )
    return recommender_v1.Recommendation(
        name=name,
        description=description,
        recommender_subtype=subtype,
        primary_impact=recommender_v1.Impact(**impact_kwargs),
        state_info=recommender_v1.RecommendationStateInfo(state=state),
    )


def _client(projects=("proj-alpha",)):
    return SimpleNamespace(project_ids=lambda: list(projects))


def _run(recs_by_recommender, client=None, **kw):
    """recs_by_recommender: {recommender_id: [rec, ...]}."""
    def _fake(project, recommender):
        return list(recs_by_recommender.get(recommender, []))
    with patch(f"{MOD}._list_recommendations", side_effect=_fake):
        return asyncio.run(get_gcp_recommendations(client or _client(), **kw))


# ── money + duration math (against real types) ───────────────────────────────────


def test_money_units_combines_units_and_nanos():
    assert _money_units(_money(12, 500_000_000)) == 12.5
    # Real savings money is fully negative in both fields.
    assert _money_units(_money(-40, -500_000_000)) == -40.5
    assert _money_units(None) == 0.0


def test_duration_seconds_reads_real_timedelta_not_intraday_remainder():
    # The real proto hands back a datetime.timedelta. A 3-year span's `.seconds` is 0
    # (intra-day remainder); total_seconds() is the truth. This is the drift guard.
    proj = recommender_v1.CostProjection(
        duration=duration_pb2.Duration(seconds=3 * 365 * 86400))
    d = proj.duration
    assert isinstance(d, datetime.timedelta)
    assert d.seconds == 0                      # the trap
    assert _duration_seconds(d) == 3 * 365 * 86400  # the truth


def test_duration_seconds_defaults_to_a_month_when_missing_or_zero():
    assert _duration_seconds(None) == MONTH
    assert _duration_seconds(datetime.timedelta(0)) == MONTH
    assert _duration_seconds(datetime.timedelta(days=7)) == 7 * 86400


def test_monthly_savings_negates_and_normalizes_to_month():
    rec = _rec("r1", monthly_cost=-100, duration_secs=MONTH)
    monthly, currency = _monthly_savings(rec)
    assert monthly == 100.0
    assert currency == "USD"


def test_monthly_savings_normalizes_a_multi_year_commitment():
    # A CUD projects -$3600 over 3 years; per 30-day month ~ $98.63.
    rec = _rec("cud", monthly_cost=-3600, duration_secs=3 * 365 * 86400)
    monthly, _ = _monthly_savings(rec)
    assert 95 < monthly < 102


def test_monthly_savings_zero_when_cost_not_a_saving():
    assert _monthly_savings(_rec("r", monthly_cost=50))[0] == 0.0          # cost increase
    assert _monthly_savings(_rec("r", with_projection=False))[0] == 0.0    # no projection


def test_state_name_reads_enum_and_is_permissive_on_missing():
    assert _state_name(_rec("r", monthly_cost=-10, state="ACTIVE")) == "ACTIVE"
    assert _state_name(_rec("r", monthly_cost=-10, state="DISMISSED")) == "DISMISSED"
    # A non-proto object with no state_info must not blow up (defensive path).
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
    assert f["finding"]["kind"] == "investigation"
    assert f["finding"]["est_monthly_savings"] is None   # invariant: no precise $ on inferred
    assert f["estimated_monthly_savings"] > 0            # raw estimate kept for ranking


def test_non_active_recommendations_are_skipped():
    recs = {IDLE_VM: [
        _rec("a/b/recommendations/a", monthly_cost=-30, state="DISMISSED"),
        _rec("a/b/recommendations/b", monthly_cost=-30, state="SUCCEEDED"),
        _rec("a/b/recommendations/c", monthly_cost=-30, state="ACTIVE"),
    ]}
    out = _run(recs)
    assert out["total_findings"] == 1
    assert out["findings"][0]["resource_id"] == "c"


def test_zero_saving_recommendations_are_dropped():
    recs = {MACHINE: [
        _rec("a/b/recommendations/a", monthly_cost=50),        # cost increase
        _rec("a/b/recommendations/b", with_projection=False),  # no projection
    ]}
    out = _run(recs)
    assert out["total_findings"] == 0


def test_findings_sorted_and_aggregated_by_bucket():
    recs = {
        IDLE_VM: [_rec("a/b/recommendations/v", monthly_cost=-20, description="idle vm")],
        MACHINE: [_rec("a/b/recommendations/m", monthly_cost=-600, description="rightsize")],
    }
    out = _run(recs)
    assert [f["estimated_monthly_savings"] for f in out["findings"]] == [600.0, 20.0]
    assert out["total_estimated_monthly_savings"] == 620.0
    assert out["by_category"]["vm_rightsizing"]["monthly_savings"] == 600.0
    assert out["by_severity"]["high"]["count"] == 1
    assert out["by_severity"]["low"]["count"] == 1
    assert out["by_project"]["proj-alpha"]["count"] == 2


def test_invalid_project_ids_rejected():
    out = _run({}, client=SimpleNamespace(project_ids=lambda: ["Bad_Project!"]))
    assert "error" in out


def test_explicit_recommenders_filter_is_honoured():
    recs = {IDLE_VM: [_rec("a/b/recommendations/v", monthly_cost=-20)],
            MACHINE: [_rec("a/b/recommendations/m", monthly_cost=-600)]}
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
    good = {IDLE_VM: [_rec("a/b/recommendations/v", monthly_cost=-20)]}

    def _fake(project, recommender):
        if recommender == MACHINE:
            raise RuntimeError("API not enabled")
        return list(good.get(recommender, []))

    with patch(f"{MOD}._list_recommendations", side_effect=_fake):
        out = asyncio.run(get_gcp_recommendations(_client()))
    assert out["total_findings"] == 1
    assert any(e["recommender"] == MACHINE for e in out["errors"])
    assert "setup_hint" not in out


def test_every_recommender_has_remediation():
    """A finding must never surface without a confirm-first next step."""
    for rid, meta in RECOMMENDERS.items():
        assert meta["category"] in mod._REMEDIATION, f"no remediation for {rid}"
        assert mod._REMEDIATION[meta["category"]], f"empty remediation for {rid}"
