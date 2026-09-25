"""Totals that dropped a resource, counted one twice, or used a second rate.

- The deep audit's dedup keyed on resource id and waste type, not region. A
  DynamoDB table or RDS instance with the same name in two regions is two
  resources; the cheaper one was dropped from the total.
- The sweep counted every unattached Elastic IP twice: once in the ipv4
  aggregate and once more per address from cleanup.idle's elastic_ip scan. The
  aggregate's title also counted only the unattached addresses while its total
  covered the ones on stopped instances too.
- kubernetes_costs turned daily cost into monthly with x30 (a 720-hour month)
  while every other monthly figure uses aws_prices.HOURS_PER_MONTH (730).
- public_ipv4 kept its own copy of the IPv4 rate.
"""
from __future__ import annotations

import inspect

import pytest

from finops import aws_prices


# ── dedup is per region ──────────────────────────────────────────────────────

def test_same_name_in_two_regions_is_two_resources():
    from finops.analyzers.optimizer import _dedup_findings

    out = _dedup_findings([
        {"resource_id": "orders", "waste_type": "dynamodb_overprovisioned_capacity",
         "estimated_monthly_savings": 120.0, "region": "us-east-1"},
        {"resource_id": "orders", "waste_type": "dynamodb_overprovisioned_capacity",
         "estimated_monthly_savings": 80.0, "region": "eu-west-1"},
        {"resource_id": "prod-db", "waste_type": "rds_idle_no_connections",
         "estimated_monthly_savings": 175.0, "region": "us-east-1"},
        {"resource_id": "prod-db", "waste_type": "rds_overprovisioned",
         "estimated_monthly_savings": 90.0, "region": "eu-west-1"},
    ])
    assert sorted((f["resource_id"], f["region"]) for f in out) == [
        ("orders", "eu-west-1"), ("orders", "us-east-1"),
        ("prod-db", "eu-west-1"), ("prod-db", "us-east-1")]
    assert sum(f["estimated_monthly_savings"] for f in out) == 465.0


def test_dedup_still_collapses_within_one_region():
    from finops.analyzers.optimizer import _dedup_findings

    out = _dedup_findings([
        {"resource_id": "db", "waste_type": "rds_idle_no_connections",
         "estimated_monthly_savings": 175.0, "region": "us-east-1"},
        {"resource_id": "db", "waste_type": "rds_overprovisioned",
         "estimated_monthly_savings": 90.0, "region": "us-east-1"},
        {"resource_id": "db", "waste_type": "rds_overprovisioned",
         "estimated_monthly_savings": 60.0, "region": "us-east-1"},
    ])
    assert [(f["waste_type"], f["estimated_monthly_savings"]) for f in out] == [
        ("rds_idle_no_connections", 175.0)]


def test_the_key_separates_its_parts():
    # "ab" + "c" and "a" + "bc" are different resources.
    from finops.analyzers.optimizer import _dedup_findings

    out = _dedup_findings([
        {"resource_id": "ab", "waste_type": "c", "estimated_monthly_savings": 1.0, "region": "r"},
        {"resource_id": "a", "waste_type": "bc", "estimated_monthly_savings": 1.0, "region": "r"},
    ])
    assert len(out) == 2


# ── the sweep counts an Elastic IP once ──────────────────────────────────────

def test_the_sweep_does_not_scan_elastic_ips_twice():
    from finops.recommendations import sweep

    specs = {name: kwargs for name, _, kwargs in sweep.build_specs(object(), ["us-east-1"])}
    assert "elastic_ip" not in specs["idle_resources"]["resource_types"]
    assert set(specs["idle_resources"]["resource_types"]) == {
        "ebs_volume", "snapshot", "stopped_ec2", "load_balancer"}
    assert "ipv4" in specs


def test_the_ipv4_title_counts_what_its_total_covers():
    from finops.recommendations.sweep import normalise

    data = {
        "unattached_eips": [{"allocation_id": "a1"}, {"allocation_id": "a2"}],
        "stopped_instance_eips": [{"allocation_id": "s1"}, {"allocation_id": "s2"},
                                  {"allocation_id": "s3"}],
        "total_monthly_waste": round(5 * aws_prices.PUBLIC_IPV4_PER_MONTH, 2),
    }
    [f] = normalise("ipv4", data)
    assert f["title"] == "Release 5 idle Elastic IP(s) (2 unattached, 3 on stopped instances)"
    assert f["monthly_savings"] == 18.25
    assert "$3.65 per IP" in f["detail"]


# ── one month, one rate ──────────────────────────────────────────────────────

def test_kubernetes_costs_uses_the_730_hour_month():
    from finops.connectors import kubernetes_costs

    assert kubernetes_costs._DAYS_PER_MONTH == aws_prices.HOURS_PER_MONTH / 24
    src = inspect.getsource(kubernetes_costs)
    assert "* 30," not in src and "* 30)" not in src


def test_public_ipv4_rates_come_from_aws_prices():
    from finops.recommendations import public_ipv4

    assert public_ipv4.IPV4_HOURLY_RATE is aws_prices.PUBLIC_IPV4_HOURLY
    assert public_ipv4.IPV4_MONTHLY_RATE is aws_prices.PUBLIC_IPV4_PER_MONTH
    assert public_ipv4.IPV4_MONTHLY_RATE == pytest.approx(3.65)
