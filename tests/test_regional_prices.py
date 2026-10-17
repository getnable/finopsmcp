"""A resource outside us-east-1 is priced at its region's rate where nable has one,
and says it was priced at us-east-1 list where it does not.

Every rate in aws_prices is us-east-1's. The guard already holds, per region,
the least and the most that region charges over us-east-1 for the EC2 types
nable lists (AWS Price List regional offers, October 2026; eu-west-1 checked
against AmazonEC2 eu-west-1 20261012193925 over 150 listed types: least
1.0689x, c7g.medium $0.0388 vs $0.0363, most 1.25x, inf2.24xlarge), and
judges a command at the least of them, a floor for every listed type. Nothing
outside the guard used it. On the fixture account the spot row for a
c5.xlarge in eu-west-1 priced its on-demand cost at $124.10 ($0.17/hr, the
us-east-1 rate; eu-west-1 lists $0.192), and the unattached 500 GB gp3
volume in eu-west-1 read $40.00 at $0.08/GB-month with nothing to say that
eu-west-1 lists $0.088 (EU-EBS:VolumeUsage.gp3, SKU TXACT7E3PV6ZX2NM).

One EC2 price is now taken at the region's floor. A difference between two
types (a Graviton move) is not bounded by a floor on each side, so it stays
at us-east-1 list, as does everything nable holds no regional rate for, and
those rows say so.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from finops import aws_prices
from finops.aws_prices import EC2_HOURLY, HOURS_PER_MONTH


def test_one_ec2_price_is_taken_at_the_regions_floor():
    rate, basis = aws_prices.ec2_hourly_in_region(0.17, "eu-west-1")
    assert rate == pytest.approx(0.17 * 1.069)
    assert "eu-west-1 floor" in basis and "1.069" in basis, basis
    assert aws_prices.ec2_hourly_in_region(0.17, "us-east-1")[0] == 0.17
    rate, basis = aws_prices.ec2_hourly_in_region(0.17, "xx-nowhere-9")
    assert rate == 0.17 and "us-east-1" in basis and "xx-nowhere-9" in basis, basis


def test_the_guard_and_the_scanners_read_one_table():
    from finops import guard

    assert not hasattr(guard, "_EC2_REGION_RANGE")
    assert aws_prices.EC2_REGION_RANGE["eu-west-1"] == (1.069, 1.25)
    assert aws_prices.EC2_REGION_RANGE["sa-east-1"] == (1.529, 1.95)


def test_a_spot_row_in_eu_west_1_starts_from_the_eu_west_1_floor():
    from unittest.mock import MagicMock

    from finops.recommendations.spot_adoption import _analyze_region

    ec2, cw = MagicMock(), MagicMock()
    ec2.get_paginator.return_value.paginate.return_value = [{"Reservations": [{"Instances": [{
        "InstanceId": "i-0eubatch", "InstanceType": "c5.xlarge",
        "Tags": [{"Key": "env", "Value": "staging"}]}]}]}]
    now = datetime.now(UTC)
    cw.get_metric_statistics.return_value = {"Datapoints": [
        {"Timestamp": now, "Average": 25.0}, {"Timestamp": now, "Average": 26.0}]}
    [row] = _analyze_region(ec2, cw, {"i-0eubatch"}, "eu-west-1", [])
    ondemand = round(EC2_HOURLY["c5.xlarge"] * 1.069 * HOURS_PER_MONTH, 2)
    assert row["monthly_ondemand_cost"] == ondemand == 132.66
    assert row["monthly_savings"] == pytest.approx(ondemand - round(ondemand * 0.39, 2))
    assert "eu-west-1 floor" in row["ondemand_basis"], row


def test_a_nonprod_schedule_in_eu_west_1_starts_from_the_eu_west_1_floor():
    import asyncio
    from unittest.mock import MagicMock, patch

    from finops.recommendations.nonprod_scheduler import identify_nonprod_resources

    inst = {"InstanceId": "i-eu", "InstanceType": "m5.large",
            "Tags": [{"Key": "env", "Value": "dev"}]}
    ec2, cw = MagicMock(), MagicMock()
    ec2.get_paginator.return_value.paginate.return_value = [
        {"Reservations": [{"Instances": [inst]}]}]
    cw.get_metric_statistics.return_value = {"Datapoints": []}
    boto3 = MagicMock()
    boto3.client.side_effect = lambda svc, **kw: ec2 if svc == "ec2" else cw
    with patch("finops.recommendations.nonprod_scheduler.boto3", boto3):
        out = asyncio.run(identify_nonprod_resources(aws_client=MagicMock(),
                                                     regions=["eu-west-1"]))
    [row] = out["schedulable_instances"]
    assert row["monthly_cost_estimate"] == round(EC2_HOURLY["m5.large"] * 1.069 * HOURS_PER_MONTH, 2)
    assert "eu-west-1 floor" in row["price_basis"], row


def test_an_idle_ec2_instance_in_eu_west_1_is_priced_at_the_floor():
    from finops.analyzers import waste
    from tests.test_audit_error_paths import _CloudWatch, _ec2_pages, _PagedClient

    cw = _CloudWatch({"CPUUtilization": [{"Average": 0.4}] * 300,
                      "NetworkOut": [{"Sum": 1024.0}] * 300})
    [f] = waste.check_idle_ec2(_PagedClient(_ec2_pages("m5.large")), cw, region="eu-west-1")
    assert f["estimated_monthly_savings"] == round(
        EC2_HOURLY["m5.large"] * 1.069 * HOURS_PER_MONTH, 2)
    assert f["rate_region"] == "eu-west-1"
    assert "eu-west-1 floor" in f["detail"], f["detail"]


def test_a_deep_audit_row_with_no_regional_rate_says_us_east_1_list():
    from finops.analyzers.optimizer import _label_rate_region

    rows = [
        {"waste_type": "unattached_ebs_volume", "region": "eu-west-1",
         "estimated_monthly_savings": 40.0, "detail": "Unattached gp3 volume, 500 GB."},
        {"waste_type": "unattached_ebs_volume", "region": "us-east-1",
         "estimated_monthly_savings": 16.0, "detail": "Unattached gp3 volume, 200 GB."},
        # Compute Optimizer prices in the account's own region.
        {"waste_type": "compute_optimizer_overprovisioned_ec2", "region": "eu-west-1",
         "estimated_monthly_savings": 50.0, "detail": "From Compute Optimizer."},
    ]
    _label_rate_region(rows)
    eu, use1, co = rows
    assert eu["rate_region"] == "us-east-1"
    assert "priced at us-east-1 list" in eu["detail"], eu["detail"]
    assert "rate_region" not in use1 and "us-east-1 list" not in use1["detail"]
    assert "rate_region" not in co and "us-east-1 list" not in co["detail"]


def test_the_scan_says_which_regions_were_priced_at_us_east_1_list():
    from finops.cli_scan import _group_findings

    [g] = _group_findings([
        {"waste_type": "unattached_ebs_volume", "region": "eu-west-1",
         "estimated_monthly_savings": 40.0, "rate_region": "us-east-1"},
        {"waste_type": "unattached_ebs_volume", "region": "us-east-1",
         "estimated_monthly_savings": 16.0}])
    assert g["us_east_1_list_in"] == ["eu-west-1"], g


def test_sweep_rows_outside_us_east_1_say_how_they_were_priced():
    from finops.cleanup.idle import IdleResource
    from finops.recommendations.sweep import normalise

    [vol] = normalise("idle_resources", [IdleResource(
        resource_type="ebs_volume", resource_id="vol-0euun", region="eu-west-1",
        account_id="1", name="", idle_since="2026-08-03", idle_days=75,
        monthly_cost_usd=40.0, reason="Unattached gp3 volume, 500 GB")])
    assert "priced at us-east-1 list" in vol["detail"], vol["detail"]
    [grav] = normalise("graviton", [{
        "instance_id": "i-0eubatch", "instance_type": "c5.xlarge",
        "graviton_equivalent": "c7g.xlarge", "savings_estimate": 18.25,
        "savings_pct": 14.7, "region": "eu-west-1"}])
    assert "priced at us-east-1 list" in grav["detail"], grav["detail"]
    [spot] = normalise("spot", [{
        "instance_id": "i-0eubatch", "instance_type": "c5.xlarge", "region": "eu-west-1",
        "monthly_savings": 80.92, "savings_pct": 61.0, "recommendation": "RECOMMENDED",
        "ondemand_basis": "eu-west-1 floor: the us-east-1 list rate x1.069"}])
    assert "eu-west-1 floor" in spot["detail"], spot["detail"]
    assert vol["us_east_1_list"] and grav["us_east_1_list"]
    assert "us_east_1_list" not in spot   # priced at the eu-west-1 floor


def test_the_audit_table_marks_a_row_priced_at_us_east_1_list(monkeypatch):
    import asyncio

    from finops import server as srv
    from finops.cleanup.idle import IdleResource
    from finops.recommendations import sweep as S
    from finops.tools import cost_queries as cq

    class _AWS:
        async def is_configured(self):
            return True

    monkeypatch.setitem(srv.CLOUD_CONNECTORS, "aws", _AWS())
    monkeypatch.setattr(srv, "require_role", lambda *a, **k: None)
    monkeypatch.setattr(S, "build_specs", lambda aws, regions: [
        ("idle_resources", lambda **k: [IdleResource(
            resource_type="ebs_volume", resource_id="vol-0euun", region="eu-west-1",
            account_id="1", name="vol-0euun", idle_since="2026-08-03", idle_days=75,
            monthly_cost_usd=40.0, reason="Unattached gp3 volume, 500 GB")], {})])
    out = asyncio.run(getattr(cq.run_full_cost_audit, "fn", cq.run_full_cost_audit)())
    assert "| $40.00 (us-east-1 list) |" in out, out


def test_a_cpu_only_rightsizing_row_outside_us_east_1_says_us_east_1_list():
    from finops.recommendations.rightsizing import RightsizingRecommendation

    rec = RightsizingRecommendation(
        instance_id="i-eu", instance_type="m5.xlarge", name="eu", region="eu-west-1",
        account_id="1", resource_type="ec2", source="cloudwatch_fallback",
        recommended_type="m5.large", monthly_savings=70.08)
    assert "priced at us-east-1 list" in rec.description, rec.description
    rec.region = "us-east-1"
    assert "us-east-1 list" not in rec.description


def _eks_node(itype: str, zone: str, spot: bool):
    from types import SimpleNamespace as NS

    from finops.connectors.kubernetes import KubernetesConnector

    labels = {"node.kubernetes.io/instance-type": itype, "topology.kubernetes.io/zone": zone}
    if spot:
        labels["eks.amazonaws.com/capacityType"] = "SPOT"
    node = NS(metadata=NS(name="n1", labels=labels),
              status=NS(allocatable={"cpu": "4", "memory": "8Gi"},
                        capacity={"cpu": "4", "memory": "8Gi"}))
    client = NS(CoreV1Api=lambda: NS(list_node=lambda: NS(items=[node])))
    [n] = KubernetesConnector()._get_nodes(client, "aws")
    return n


def test_an_eks_node_outside_us_east_1_is_priced_at_its_regions_floor():
    from finops.aws_prices import EC2_MONTHLY

    n = _eks_node("c5.xlarge", "eu-west-1b", spot=False)
    assert n.monthly_cost == pytest.approx(EC2_MONTHLY["c5.xlarge"] * 1.069)
    assert "eu-west-1 floor" in n.cost_basis, n.cost_basis
    s = _eks_node("c5.xlarge", "eu-west-1b", spot=True)
    assert s.monthly_cost == pytest.approx(EC2_MONTHLY["c5.xlarge"] * 1.069 * 0.39)
    assert "eu-west-1 floor" in s.cost_basis and "median" in s.cost_basis, s.cost_basis
    assert _eks_node("c5.xlarge", "us-east-1a", spot=False).cost_basis == ""
