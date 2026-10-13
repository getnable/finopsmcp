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


# ── spot figures come from the Spot Instance Advisor they cite ───────────────
#
# AWS Spot Instance Advisor public data (spot-bid-advisor.s3.amazonaws.com/
# spot-advisor-data.json, object Last-Modified 2026-03-20), us-east-1 Linux:
#
#   type        savings  interruption band      table said
#   m5.large      59%     >20%                   72%, 2%
#   m5.xlarge     68%     >20%                   71%, 3%
#   m5.2xlarge    64%     >20%                   70%, (default 10%)
#   m6i.large     61%     >20%                   68%, 1%
#   m6i.xlarge    59%     >20%                   69%, 2%
#   c5.large      61%     10-15%                 75%, 5%
#   c5.xlarge     58%     10-15%                 74%, 4%
#   r5.large      62%     >20%                   65%, (default 10%)
#   r5.xlarge     70%     >20%                   64%, (default 10%)
#
# The interruption figures were labelled RECOMMENDED-grade (under 5%) for
# types the source puts in its worst band. The same file shows the bands vary
# by region far more than by type (c5.large is <5% in eu-west-1 and >20% in
# us-east-2), so a us-east-1 figure is no evidence about another region.

_ADVISOR_US_EAST_1 = {
    "m5.large": (0.59, ">20%"), "m5.xlarge": (0.68, ">20%"), "m5.2xlarge": (0.64, ">20%"),
    "m6i.large": (0.61, ">20%"), "m6i.xlarge": (0.59, ">20%"),
    "c5.large": (0.61, "10-15%"), "c5.xlarge": (0.58, "10-15%"),
    "r5.large": (0.62, ">20%"), "r5.xlarge": (0.70, ">20%"),
}


def test_the_spot_tables_are_the_spot_advisor_figures():
    from finops.recommendations import spot_adoption as sa

    for itype, (saving, band) in _ADVISOR_US_EAST_1.items():
        assert sa.SPOT_DISCOUNT[itype] == saving, itype
        assert sa.SPOT_INTERRUPTION_BAND[itype] == band, itype


def _spot_rows(itype: str, region: str):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    from finops.recommendations.spot_adoption import _analyze_region

    ec2, cw = MagicMock(), MagicMock()
    ec2.get_paginator.return_value.paginate.return_value = [{"Reservations": [{"Instances": [{
        "InstanceId": "i-1", "InstanceType": itype,
        "Tags": [{"Key": "Name", "Value": "web"}, {"Key": "env", "Value": "staging"}]}]}]}]
    now = datetime.now(timezone.utc)
    cw.get_metric_statistics.return_value = {"Datapoints": [
        {"Timestamp": now, "Average": 5.0}, {"Timestamp": now, "Average": 6.0}]}
    return _analyze_region(ec2, cw, {"i-1"}, region, [])


def test_an_m5_large_in_us_east_1_is_not_a_spot_recommendation():
    """Staging-tagged, in an ASG, steady CPU: every signal but the interruption
    rate says go. The table put m5.large at 2% and RECOMMENDED it; Spot Advisor
    puts it in the >20% band, which the classifier's own threshold rejects."""
    [row] = _spot_rows("m5.large", "us-east-1")
    assert row["interruption_band"] == ">20%"
    assert row["recommendation"] == "NOT_RECOMMENDED"
    # 70.08 a month on demand at 59% off.
    assert row["monthly_savings"] == 41.35 and row["savings_pct"] == 59.0


def test_a_spot_figure_from_one_region_is_not_applied_to_another():
    [row] = _spot_rows("m5.large", "eu-west-1")
    assert row["interruption_band"] == "unknown"
    assert row["recommendation"] == "POSSIBLE"
    assert row["savings_basis"] == "assumed"
    [row] = _spot_rows("c5.large", "us-east-1")
    assert row["interruption_band"] == "10-15%" and row["recommendation"] == "POSSIBLE"
    assert row["savings_basis"] == "spot_advisor"


def test_the_spot_tool_does_not_promise_60_to_80_percent():
    import finops.server as server
    from finops.recommendations import spot_adoption as sa

    assert "60-80%" not in (server.recommend_spot_adoption.__doc__ or "")
    assert "60-80%" not in (sa.__doc__ or "")


# ── a stop schedule saves the hours it stops, and no more ────────────────────
#
# The recommendation is Mon-Fri 08:00-18:00 UTC: 50 hours on, 118 off, of a
# 168-hour week. The saving was cost x (idle hours / 168), counting every idle
# hour in the week, so an instance idle around the clock was promised its
# whole bill while the schedule offered stops it for 118/168 of the time.

def _nonprod(cpu_by_instance: dict[str, list[float]], itype: str = "m5.large"):
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock, patch

    from finops.recommendations import nonprod_scheduler as ns

    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    ec2, cw, boto = MagicMock(), MagicMock(), MagicMock()
    ec2.get_paginator.return_value.paginate.return_value = [{"Reservations": [{"Instances": [
        {"InstanceId": iid, "InstanceType": itype,
         "Tags": [{"Key": "Environment", "Value": "dev"}]}
        for iid in cpu_by_instance]}]}]

    def stats(**kw):
        vals = cpu_by_instance.get(kw["Dimensions"][0]["Value"], [])
        return {"Datapoints": [{"Timestamp": base + timedelta(hours=h), "Maximum": v}
                               for h, v in enumerate(vals)]}

    cw.get_metric_statistics.side_effect = stats
    boto.client.side_effect = lambda svc, **kw: ec2 if svc == "ec2" else cw
    with patch.object(ns, "boto3", boto):
        return asyncio.run(ns.identify_nonprod_resources(aws_client=None, regions=["us-east-1"]))


def test_a_schedule_cannot_save_more_than_the_hours_it_stops(monkeypatch):
    from finops.analyzers.cloudwatch import GET_METRIC_DATA_ENV

    monkeypatch.delenv(GET_METRIC_DATA_ENV, raising=False)
    out = _nonprod({"i-idle": [0.5] * 168})
    [row] = out["schedulable_instances"]
    assert row["idle_hours_per_week"] == 168.0
    # m5.large 70.08 a month on demand; the schedule stops it 118 of 168 hours.
    assert row["potential_monthly_savings"] == 49.22
    assert out["total_monthly_waste"] == 49.22


def test_each_scheduler_row_says_whether_its_idle_hours_were_measured(monkeypatch):
    from finops.analyzers.cloudwatch import GET_METRIC_DATA_ENV

    monkeypatch.delenv(GET_METRIC_DATA_ENV, raising=False)
    out = _nonprod({"i-seen": [0.5] * 100 + [60.0] * 68, "i-blank": []})
    rows = {r["instance_id"]: r for r in out["schedulable_instances"]}
    assert rows["i-seen"]["idle_basis"] == "measured"
    # No datapoints: the nights-and-weekends figure is an assumption, and a
    # reader of the row alone could not tell it from a measured 118.
    assert rows["i-blank"]["idle_basis"] == "assumed"
    assert rows["i-blank"]["idle_hours_per_week"] == 118.0


# ── a Fargate downsize is a task size Fargate will run ───────────────────────
#
# Fargate takes only set CPU and memory pairs (AWS ECS API reference,
# RegisterTaskDefinition `memory`, as shipped in botocore's ecs service
# model): 256 CPU units with 512-2048 MiB, 512 with 1024-4096, 1024 with
# 2048-8192, 2048 with 4096-16384, 4096 with 8192-30720. The check halved CPU
# and kept memory, so a 1024 / 8192 task was told to run at 512 / 8192, a
# task definition Fargate rejects, with its saving counted in the total.
#
# Its price was also the x86 one for every task. AmazonECS us-east-1 offer:
# USE1-Fargate-vCPU-Hours:perCPU 0.04048, USE1-Fargate-ARM-vCPU-Hours:perCPU
# 0.03238, so an ARM64 task's saving read 25% high.

class _FargateECS:
    def __init__(self, tasks: dict[str, dict]):
        self._tasks = tasks

    def get_paginator(self, name):
        tasks = self._tasks

        class _P:
            def paginate(self, **_kw):
                if name == "list_clusters":
                    return [{"clusterArns": ["arn:aws:ecs:us-east-1:1:cluster/dev"]}]
                return [{"serviceArns": [f"arn:aws:ecs:us-east-1:1:service/dev/{n}"
                                         for n in tasks]}]
        return _P()

    def describe_services(self, cluster, services):
        return {"services": [{"serviceName": a.rsplit("/", 1)[-1], "serviceArn": a,
                              "launchType": "FARGATE", "desiredCount": 1,
                              "taskDefinition": a.rsplit("/", 1)[-1]} for a in services]}

    def describe_task_definition(self, taskDefinition):
        return {"taskDefinition": self._tasks[taskDefinition]}


def _fargate(tasks: dict[str, dict]) -> dict[str, dict]:
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock

    from finops.analyzers import waste

    now = datetime.now(timezone.utc)
    cw = MagicMock()
    # 40 CPU units used on average, under 20% of any task here.
    cw.get_metric_statistics.return_value = {"Datapoints": [
        {"Timestamp": now - timedelta(hours=h), "Average": 40.0} for h in range(48)]}
    return {f["service"]: f for f in waste.check_ecs_task_rightsizing(
        _FargateECS(tasks), cw, region="us-east-1")}


def test_a_fargate_cpu_cut_must_fit_the_task_memory(monkeypatch):
    from finops.analyzers.cloudwatch import GET_METRIC_DATA_ENV

    monkeypatch.delenv(GET_METRIC_DATA_ENV, raising=False)
    out = _fargate({
        "fits": {"cpu": "1024", "memory": "2048"},
        "memory-bound": {"cpu": "1024", "memory": "8192"},
        "big": {"cpu": "2048", "memory": "16384"},
    })
    assert out["fits"]["recommended_cpu_units"] == 512
    # 512 CPU units carry at most 4096 MiB, 1024 at most 8192.
    assert "memory-bound" not in out and "big" not in out


def test_an_arm64_fargate_task_is_priced_at_the_arm_rate(monkeypatch):
    from finops.analyzers.cloudwatch import GET_METRIC_DATA_ENV

    monkeypatch.delenv(GET_METRIC_DATA_ENV, raising=False)
    out = _fargate({
        "x86": {"cpu": "1024", "memory": "2048"},
        "arm": {"cpu": "1024", "memory": "2048",
                "runtimePlatform": {"cpuArchitecture": "ARM64"}},
    })
    # 0.5 vCPU saved, one task, 730 hours.
    assert out["x86"]["estimated_monthly_savings"] == round(0.5 * 0.04048 * 730, 2) == 14.78
    assert out["arm"]["estimated_monthly_savings"] == round(0.5 * 0.03238 * 730, 2) == 11.82


# ── Compute Optimizer evidence is read as AWS documents it ───────────────────
#
# botocore's compute-optimizer service model: the Finding enum is
# Underprovisioned | Overprovisioned | Optimized | NotOptimized, and
# UtilizationMetric.statistic reads "The Compute Optimizer API, Command Line
# Interface (CLI), and SDKs return utilization metrics using only the Maximum
# statistic". InstanceRecommendation carries lookBackPeriodInDays.

def _co_rec(**kw):
    from finops.recommendations.rightsizing import RightsizingRecommendation

    base = dict(instance_id="i-1", instance_type="m5.2xlarge", name="web-1",
                region="us-east-1", account_id="1", resource_type="ec2",
                source="compute_optimizer", monthly_savings=300.0)
    base.update(kw)
    return RightsizingRecommendation(**base)


def test_the_real_overprovisioned_finding_counts_as_one():
    """genuine_savings credited only "OVER_PROVISIONED" and
    "VERY_OVER_PROVISIONED", strings the API never returns, so no Compute
    Optimizer recommendation could score above 60 and genuine_monthly_savings
    was 0 for every account whose findings came from it."""
    from finops.recommendations.genuine_savings import assess

    a = assess(_co_rec(finding="Overprovisioned", avg_cpu_pct=6.0))
    assert a.score == 75 and a.verdict == "genuine_savings"
    assert a.score == assess(_co_rec(finding="OVER_PROVISIONED", avg_cpu_pct=6.0)).score


def test_compute_optimizer_maxima_are_not_reported_as_averages():
    from finops.recommendations.rightsizing import _fetch_ec2_from_co, rightsizing_summary

    class _CO:
        def get_ec2_instance_recommendations(self, **_kw):
            return {"instanceRecommendations": [{
                "instanceArn": "arn:aws:ec2:us-east-1:1:instance/i-1",
                "currentInstanceType": "m5.2xlarge", "finding": "Overprovisioned",
                "lookBackPeriodInDays": 32.0,
                "utilizationMetrics": [
                    {"name": "Cpu", "statistic": "Maximum", "value": 38.0},
                    {"name": "Memory", "statistic": "Maximum", "value": 41.0}],
                "recommendationOptions": [{
                    "instanceType": "m5.xlarge", "rank": 1, "performanceRisk": 1.0,
                    "savingsOpportunity": {"estimatedMonthlySavings": {
                        "value": 140.16, "currency": "USD"}}}]}]}

    [rec] = _fetch_ec2_from_co(_CO(), "1")
    assert "Peak CPU 38.0%" in rec.description and "Avg CPU" not in rec.description
    assert "over 32d" in rec.description
    [row] = rightsizing_summary([rec])["recommendations"]
    assert row["avg_cpu_pct"] is None and row["max_cpu_pct"] == 38.0
    assert row["avg_mem_pct"] is None and row["max_mem_pct"] == 41.0
    assert row["lookback_days"] == 32

    # The emailed and exported report renders that row as a peak.
    from finops.reporting.exporter import build_html_report

    html = build_html_report("r", "2026-09-01", "2026-09-30",
                             rightsizing={"recommendations": [row]})
    assert "peak 38%" in html
