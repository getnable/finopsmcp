"""Tests for finops.recommendations.nlb_cross_zone."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from finops.recommendations.nlb_cross_zone import (
    CROSS_AZ_COST_PER_GB,
    CROSS_AZ_TRAFFIC_FRACTION,
    _MIN_MONTHLY_COST_THRESHOLD,
    _build_disable_command,
    _is_cross_zone_enabled,
    audit_nlb_cross_zone_costs,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def _make_aws_client():
    client = MagicMock()
    client._session = None
    return client


def _make_nlb(name: str, arn: str, lb_type: str = "network") -> dict:
    return {
        "LoadBalancerName": name,
        "LoadBalancerArn": arn,
        "Type": lb_type,
    }


# ── unit: _is_cross_zone_enabled ─────────────────────────────────────────────

def test_is_cross_zone_enabled_true():
    client = MagicMock()
    client.describe_load_balancer_attributes.return_value = {
        "Attributes": [
            {"Key": "load_balancing.cross_zone.enabled", "Value": "true"},
            {"Key": "deletion_protection.enabled", "Value": "false"},
        ]
    }
    assert _is_cross_zone_enabled(client, "arn:aws:...") is True


def test_is_cross_zone_enabled_false():
    client = MagicMock()
    client.describe_load_balancer_attributes.return_value = {
        "Attributes": [
            {"Key": "load_balancing.cross_zone.enabled", "Value": "false"},
        ]
    }
    assert _is_cross_zone_enabled(client, "arn:aws:...") is False


def test_is_cross_zone_enabled_missing_attribute_returns_false():
    client = MagicMock()
    client.describe_load_balancer_attributes.return_value = {
        "Attributes": [{"Key": "deletion_protection.enabled", "Value": "false"}]
    }
    assert _is_cross_zone_enabled(client, "arn:aws:...") is False


# ── unit: _build_disable_command ──────────────────────────────────────────────

def test_build_disable_command_contains_arn():
    arn = "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/net/my-nlb/abc"
    cmd = _build_disable_command(arn)
    assert arn in cmd
    assert "load_balancing.cross_zone.enabled" in cmd
    assert "Value=false" in cmd


# ── unit: cost formula ────────────────────────────────────────────────────────

def test_cross_zone_cost_formula():
    processed_gb = 1000.0
    expected = processed_gb * CROSS_AZ_TRAFFIC_FRACTION * CROSS_AZ_COST_PER_GB
    # 1000 GB * 0.5 * 0.01 = $5.00
    assert abs(expected - 5.0) < 0.001


# ── integration: ALBs skipped ────────────────────────────────────────────────

def test_albs_skipped():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session
        elbv2_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: elbv2_client if svc == "elbv2" else cw_client

        elbv2_client.describe_load_balancers.return_value = {
            "LoadBalancers": [
                _make_nlb("my-alb", "arn:aws:...:alb", lb_type="application")
            ]
        }

        result = _run(audit_nlb_cross_zone_costs(aws_client=aws_client, regions=["us-east-1"]))

    assert result == []


# ── integration: cross-zone disabled NLBs skipped ────────────────────────────

def test_cross_zone_disabled_nlb_skipped():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session
        elbv2_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: elbv2_client if svc == "elbv2" else cw_client

        elbv2_client.describe_load_balancers.return_value = {
            "LoadBalancers": [_make_nlb("my-nlb", "arn:aws:...:nlb")]
        }
        elbv2_client.describe_load_balancer_attributes.return_value = {
            "Attributes": [{"Key": "load_balancing.cross_zone.enabled", "Value": "false"}]
        }

        result = _run(audit_nlb_cross_zone_costs(aws_client=aws_client, regions=["us-east-1"]))

    assert result == []


# ── integration: high-traffic NLB flagged for action ────────────────────────

def test_high_traffic_nlb_flagged():
    aws_client = _make_aws_client()
    nlb_arn = "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/net/big-nlb/abc123"

    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session
        elbv2_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: elbv2_client if svc == "elbv2" else cw_client

        elbv2_client.describe_load_balancers.return_value = {
            "LoadBalancers": [_make_nlb("big-nlb", nlb_arn)]
        }
        elbv2_client.describe_load_balancer_attributes.return_value = {
            "Attributes": [{"Key": "load_balancing.cross_zone.enabled", "Value": "true"}]
        }
        # 10 TB processed = 10,000 GB -> cross-az cost = 10000 * 0.5 * 0.01 = $50
        ten_tb_bytes = 10_000 * 1024 ** 3
        cw_client.get_metric_statistics.return_value = {
            "Datapoints": [{"Sum": ten_tb_bytes}]
        }

        result = _run(audit_nlb_cross_zone_costs(aws_client=aws_client, regions=["us-east-1"]))

    assert len(result) == 1
    finding = result[0]
    assert finding["nlb_name"] == "big-nlb"
    assert finding["cross_zone_enabled"] is True
    assert finding["estimated_cross_az_cost"] > _MIN_MONTHLY_COST_THRESHOLD
    assert finding["recommendation"] == "disable_cross_zone_lb_to_eliminate_cross_az_charges"
    assert nlb_arn in finding["disable_command"]


# ── integration: low-traffic NLB marked monitor only ────────────────────────

def test_low_traffic_nlb_monitor_only():
    aws_client = _make_aws_client()
    nlb_arn = "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/net/small-nlb/xyz"

    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session
        elbv2_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: elbv2_client if svc == "elbv2" else cw_client

        elbv2_client.describe_load_balancers.return_value = {
            "LoadBalancers": [_make_nlb("small-nlb", nlb_arn)]
        }
        elbv2_client.describe_load_balancer_attributes.return_value = {
            "Attributes": [{"Key": "load_balancing.cross_zone.enabled", "Value": "true"}]
        }
        # 100 GB -> cost = 100 * 0.5 * 0.01 = $0.50 < $10 threshold
        hundred_gb_bytes = 100 * 1024 ** 3
        cw_client.get_metric_statistics.return_value = {
            "Datapoints": [{"Sum": hundred_gb_bytes}]
        }

        result = _run(audit_nlb_cross_zone_costs(aws_client=aws_client, regions=["us-east-1"]))

    assert len(result) == 1
    assert result[0]["recommendation"] == "monitor_no_action_needed"
    assert result[0]["estimated_cross_az_cost"] < _MIN_MONTHLY_COST_THRESHOLD


# ── integration: sorted by cost descending ────────────────────────────────────

def test_sorted_by_cost_descending():
    aws_client = _make_aws_client()
    cheap_arn = "arn:aws:...:loadbalancer/net/cheap-nlb/aaa"
    expensive_arn = "arn:aws:...:loadbalancer/net/expensive-nlb/bbb"

    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session
        elbv2_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: elbv2_client if svc == "elbv2" else cw_client

        elbv2_client.describe_load_balancers.return_value = {
            "LoadBalancers": [
                _make_nlb("cheap-nlb", cheap_arn),
                _make_nlb("expensive-nlb", expensive_arn),
            ]
        }
        elbv2_client.describe_load_balancer_attributes.return_value = {
            "Attributes": [{"Key": "load_balancing.cross_zone.enabled", "Value": "true"}]
        }

        def cw_stats(**kw):
            lb_dim = next(
                (d["Value"] for d in kw.get("Dimensions", []) if d["Name"] == "LoadBalancer"),
                "",
            )
            if "cheap" in lb_dim:
                return {"Datapoints": [{"Sum": 100 * 1024 ** 3}]}
            return {"Datapoints": [{"Sum": 10_000 * 1024 ** 3}]}

        cw_client.get_metric_statistics.side_effect = cw_stats

        result = _run(audit_nlb_cross_zone_costs(aws_client=aws_client, regions=["us-east-1"]))

    assert len(result) == 2
    assert result[0]["estimated_cross_az_cost"] >= result[1]["estimated_cross_az_cost"]
    assert result[0]["nlb_name"] == "expensive-nlb"


# ── integration: empty region ─────────────────────────────────────────────────

def test_returns_empty_when_no_nlbs():
    aws_client = _make_aws_client()

    with patch("boto3.Session") as mock_cls:
        session = MagicMock()
        mock_cls.return_value = session
        elbv2_client = MagicMock()
        cw_client = MagicMock()
        session.client.side_effect = lambda svc, **kw: elbv2_client if svc == "elbv2" else cw_client

        elbv2_client.describe_load_balancers.return_value = {"LoadBalancers": []}

        result = _run(audit_nlb_cross_zone_costs(aws_client=aws_client, regions=["us-east-1"]))

    assert result == []
