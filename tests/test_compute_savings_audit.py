"""Compute and commitment savings, each figure held to its primary source.

Every test here pins a number an audit of the compute and commitment engines
found wrong, with the inputs, the wrong figure and the right one. Where a
figure comes from the public AWS Price List, the offer, region and version it
was read from are named next to it.
"""
from __future__ import annotations

import asyncio
import inspect


# ── the sweep reports a percentage once, not times a hundred ─────────────────
#
# graviton._compute_savings and spot_adoption both hand over savings_pct as a
# percentage (15.0 for fifteen percent). sweep.normalise multiplied it by 100
# again, so run_full_cost_audit and the report exports printed an m5.xlarge to
# m7g.xlarge move as "1500% saving" and a spot conversion as "7200% saving".

def test_the_sweep_prints_a_graviton_saving_as_the_percentage_it_is():
    from finops.recommendations.graviton import _compute_savings
    from finops.recommendations.sweep import normalise

    current, saving, pct = _compute_savings("m5.xlarge", "m7g.xlarge")
    # us-east-1 Linux on-demand: m5.xlarge 0.192, m7g.xlarge 0.1632 a hour.
    assert (current, saving, pct) == (140.16, 21.02, 15.0)
    [f] = normalise("graviton", [{
        "instance_id": "i-1", "instance_type": "m5.xlarge",
        "graviton_equivalent": "m7g.xlarge", "savings_estimate": saving,
        "savings_pct": pct, "region": "us-east-1",
        "current_monthly_cost_estimate": current}])
    assert f["detail"].startswith("15% saving")
    assert "1500%" not in f["detail"]


def test_the_sweep_prints_a_spot_saving_as_the_percentage_it_is():
    from finops.recommendations.sweep import normalise

    [f] = normalise("spot", [{
        "instance_id": "i-2", "instance_type": "m5.large", "monthly_savings": 41.35,
        "savings_pct": 59.0, "recommendation": "RECOMMENDED"}])
    assert f["detail"] == "59% saving"


# ── a Graviton migration is like for like, never a hidden downsize ───────────
#
# AWS Price List, AmazonEC2 us-east-1 offer, version 20261012193925 (vCPU and
# Memory columns, Linux on-demand rates):
#
#   m5.24xlarge   96 vCPU, 384 GiB, 4.608   ->  m7g.16xlarge  64 vCPU, 256 GiB, 2.6112
#   c5.9xlarge    36 vCPU,  72 GiB, 1.53    ->  c7g.8xlarge   32 vCPU,  64 GiB, 1.16
#   c5.18xlarge   72 vCPU, 144 GiB, 3.06    ->  c7g.16xlarge  64 vCPU, 128 GiB, 2.32
#
# Each of those maps a box onto a smaller one, so the "Graviton saving" was
# 43.3%, 24.2% and 24.2% where the like-for-like per-vCPU gap is 15.0% and
# 14.7%. m7g and c7g stop at 16xlarge (64 vCPU) and have no 9xlarge, so
# these three have no same-size Graviton equivalent in the table at all.
# Every other entry keeps its size, and keeping the size suffix is exactly
# keeping vCPU and memory in these families.

_DOWNSIZES_DRESSED_AS_GRAVITON = {
    "m5.24xlarge": "m7g.16xlarge",
    "c5.9xlarge": "c7g.8xlarge",
    "c5.18xlarge": "c7g.16xlarge",
}


def test_no_graviton_mapping_changes_the_instance_size():
    from finops.recommendations.graviton import GRAVITON_MAP

    for src, dst in _DOWNSIZES_DRESSED_AS_GRAVITON.items():
        assert GRAVITON_MAP.get(src) != dst, f"{src} -> {dst} is a downsize"
    for src, dst in GRAVITON_MAP.items():
        assert src.split(".")[1] == dst.split(".")[1], (src, dst)


def test_an_unpriced_graviton_target_gets_no_invented_saving():
    """The fallback quoted a flat 20% when the Graviton type had no price.
    The same price list puts the mapped pairs between 5.1% (m5a, c6a to
    m7g, c7g) and 19.2% (t3 to t4g), never 20%, so the flat figure is above
    every pair it could stand in for. Unknown is reported as unknown."""
    from finops.recommendations.graviton import _compute_savings

    current, saving, pct = _compute_savings("m5.large", "m99g.huge")
    assert current == 70.08
    assert (saving, pct) == (0.0, 0.0)


def test_the_mapped_graviton_savings_are_the_list_price_gap():
    from finops.recommendations.graviton import GRAVITON_MAP, _compute_savings

    pcts = {src: _compute_savings(src, dst)[2] for src, dst in GRAVITON_MAP.items()}
    assert min(pcts.values()) == 5.1 and max(pcts.values()) == 19.2
    assert pcts["m5.xlarge"] == 15.0 and pcts["t3.medium"] == 19.2


def _graviton_tool_text(monkeypatch, candidates):
    import asyncio

    import finops.server as server
    from finops.recommendations import graviton

    aws = server.CLOUD_CONNECTORS.get("aws")

    async def _configured():
        return True

    async def _scan(*_a, **_kw):
        return candidates

    monkeypatch.setattr(aws, "is_configured", _configured)
    monkeypatch.setattr(graviton, "scan_graviton_opportunities", _scan)
    return asyncio.run(server.scan_graviton_migration_opportunities(regions=["us-east-1"]))


def test_the_graviton_tool_does_not_promise_an_in_place_type_change(monkeypatch):
    """An x86_64 AMI does not boot on an arm64 instance type, so moving to
    Graviton is a relaunch from an arm64 image, which is what the finding's own
    remediation says. The tool's table told the reader most workloads need
    "only an instance type change and a reboot"."""
    from finops.recommendations.graviton import _compute_savings

    current, saving, pct = _compute_savings("m5.xlarge", "m7g.xlarge")
    text = _graviton_tool_text(monkeypatch, [{
        "instance_id": "i-1", "instance_type": "m5.xlarge", "graviton_equivalent": "m7g.xlarge",
        "current_monthly_cost_estimate": current, "savings_estimate": saving,
        "savings_pct": pct, "region": "us-east-1", "name_tag": "web", "account_id": "1",
        "finding": None}])
    assert "only an instance type change and a reboot" not in text
    assert "arm64 image" in text
    # The basis of the figure is stated: on-demand list, before any RI or
    # Savings Plan the instance may already have.
    assert "list price" in text


def test_the_graviton_tool_does_not_advertise_a_saving_its_table_cannot_reach():
    import finops.server as server

    doc = server.scan_graviton_migration_opportunities.__doc__ or ""
    assert "20-40%" not in doc


# ── a Compute Savings Plan saves what AWS's rate card says it does ──────────
#
# AWS Price List, AWSComputeSavingsPlan us-east-1, version 20261013035015,
# 1 year No Upfront, matched to the AmazonEC2 us-east-1 Linux on-demand rates
# (version 20261012193925): across the 168 instance types aws_prices holds the
# discount is 18.6% to 29.1%, median 27.1% (m5.large 0.096 -> 0.071, 26.0%;
# c5.xlarge 0.17 -> 0.123, 27.6%). Fargate (AmazonECS offer, same dates) is
# 0.04048 -> 0.032384 a vCPU-hour, 20.0%. The engine assumed 34%, above the
# highest rate of any type in the table.

def test_the_compute_savings_plan_rate_is_the_price_list_median():
    from finops.recommendations.commitments import _COMPUTE_SP_DISCOUNT

    assert round(1 - _COMPUTE_SP_DISCOUNT, 3) == 0.271


def test_a_compute_savings_plan_recommendation_saves_27_percent_of_its_baseline():
    from finops.recommendations.commitments import _build_recommendations

    recs = _build_recommendations(20.0, 33000.0, 0.0, 0.0,
                                  monthly_uncovered_series=[12000.0, 10000.0, 11000.0])
    [sp] = [r for r in recs if r["type"] == "savings_plan"]
    assert sp["baseline_monthly_uncovered_usd"] == 10000.0
    # Was 3,400 saved on a 6,600 commitment: a 6,600 commitment at the real
    # rate covers 9,053 of on-demand and saves 2,453, not 3,400.
    assert sp["monthly_savings"] == 2710.0
    assert sp["commitment_per_month"] == 7290.0
    assert sp["commitment_per_month"] + sp["monthly_savings"] == 10000.0


def _commitment_tool(monkeypatch, uncovered_3mo: float):
    from finops.recommendations import commitments as c
    import finops.server as server

    analysis = c.CommitmentAnalysis(
        savings_plan_coverage_pct=10.0, savings_plan_utilization_pct=99.0,
        savings_plan_unused_usd=0.0, ri_coverage_pct=10.0, ri_utilization_pct=99.0,
        ri_unused_usd=0.0, uncovered_on_demand_usd=uncovered_3mo)
    monkeypatch.setattr(c, "analyze_commitments", lambda *a, **k: analysis)
    out = server.get_commitment_analysis()
    if inspect.isawaitable(out):
        out = asyncio.run(out)
    return out


def test_the_if_you_bought_more_projection_adds_up(monkeypatch):
    """It said a commitment of half the uncovered on-demand spend "would cover
    ~50%" of it, then took 34% of the commitment as the saving. A commitment C
    covers C / (1 - d) of on-demand spend, so the three figures could not all
    be true. Covering half of 10,000 a month at 27.1% is a 3,645 commitment
    saving 1,355."""
    out = _commitment_tool(monkeypatch, 30000.0)
    proj = out["actionable_analysis"]["if_you_bought_more"]
    assert proj["additional_monthly_commitment_usd"] == 3645.0
    assert proj["projected_monthly_savings_usd"] == 1355.0
    assert proj["projected_annual_savings_usd"] == 16260.0
    covered = proj["additional_monthly_commitment_usd"] + proj["projected_monthly_savings_usd"]
    assert covered == 5000.0


def test_the_commitment_gap_ticket_uses_the_same_rate():
    from finops.integrations.ticketing import _commitment_gap_ticket

    title, body, _, _ = _commitment_gap_ticket(
        {"coverage_pct": 20, "uncovered_on_demand_usd": 30000.0})
    # 10,000 a month uncovered, all of it covered at 27.1%: 2,710 a month.
    assert "saves $32,520/yr" in title
    assert "no break-even period" not in body


# ── a Database Savings Plan covers what AWS sells it for ────────────────────
#
# AWS Price List, AWSDatabaseSavingsPlans us-east-1 version 20261007173435,
# matched to the AmazonRDS us-east-1 on-demand offer by usage type and
# operation: every RDS rate is 1 year No Upfront (no other term or payment is
# offered), and every provisioned-instance rate is exactly 20.0% below
# on-demand (570 MySQL, PostgreSQL and MariaDB rows; 35% appears only on
# Aurora serverless capacity). The instance families with a rate at all are
# m7g, m7i, m8a, m8g, m8gd, m8i, m9g, r7g, r7i, r8a, r8g, r8gd, r8i and x2m;
# eu-west-1 and ap-southeast-2 list the same families or fewer. db.t3, db.t4g,
# db.m5, db.m6g, db.m6i, db.r5, db.r6g and db.r6i have no rate: a plan bought
# for them goes unused.

def _dbsp(monkeypatch, groups: dict[str, float], coverage_pct: float) -> dict:
    from unittest.mock import MagicMock

    from finops.recommendations import database_savings_plans as dbsp

    ce = MagicMock()
    ce.get_cost_and_usage.return_value = {"ResultsByTime": [{"Groups": [
        {"Keys": [k], "Metrics": {"UnblendedCost": {"Amount": str(v)}}}
        for k, v in groups.items()]}]}
    covered = 10_000.0 * coverage_pct / 100
    ce.get_savings_plans_coverage.return_value = {"SavingsPlansCoverages": [{
        "Coverage": {"SpendCoveredBySavingsPlans": str(covered), "TotalCost": "10000.0"}}]}
    boto = MagicMock()
    boto.client.return_value = ce
    monkeypatch.setattr(dbsp, "boto3", boto)
    return dbsp.recommend_database_savings_plans()


def test_the_database_savings_plan_rate_is_20_percent():
    from finops.recommendations.database_savings_plans import (
        DATABASE_SP_DISCOUNT_1YR_NO_UPFRONT,
    )

    assert DATABASE_SP_DISCOUNT_1YR_NO_UPFRONT == 0.20


def test_a_database_savings_plan_is_not_sized_on_families_it_cannot_cover(monkeypatch):
    """6,000 a month on db.r5 and 4,000 on db.r7g, nothing covered. The plan
    can only apply to the 4,000: it used to be sized to all 10,000 at 30%,
    a 9.59/hr commitment promising 3,000 a month, of which 6,000 of on-demand
    a month has no Database SP rate to land on."""
    out = _dbsp(monkeypatch, {
        "InstanceUsage:db.r5.large": 4000.0,
        "Multi-AZUsage:db.r5.xlarge": 2000.0,
        "InstanceUsage:db.r7g.large": 3000.0,
        "Multi-AZUsage:db.r7g.large": 1000.0,
        "RDS:GP3-Storage": 900.0,
    }, coverage_pct=0.0)
    assert out["current_monthly_rds_spend"] == 10000.0
    assert out["ineligible_monthly_spend"] == 6000.0
    assert out["uncovered_monthly_spend"] == 4000.0
    assert out["estimated_monthly_savings"] == 800.0
    assert out["recommended_sp_hourly_commitment"] == round(4000.0 * 0.8 / 730, 4)


def test_an_all_older_generation_fleet_gets_no_database_savings_plan(monkeypatch):
    out = _dbsp(monkeypatch, {"InstanceUsage:db.m5.large": 5000.0,
                              "InstanceUsage:db.t3.medium": 500.0}, coverage_pct=0.0)
    assert out["estimated_monthly_savings"] == 0.0
    assert out["recommended_sp_hourly_commitment"] == 0.0
    assert out["finding"] is None


def test_database_savings_plan_text_claims_only_what_is_offered():
    import finops.server as server
    from finops.recommendations import database_savings_plans as dbsp
    from finops.recommendations.sweep import normalise

    assert "45%" not in (dbsp.__doc__ or "")
    assert "45%" not in (server.recommend_database_savings_plans.__doc__ or "")
    [f] = normalise("db_sp", {"estimated_monthly_savings": 800.0})
    assert "35%" not in f["detail"]
