"""Tests for finops.recommendations.spot_diversification."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from finops.recommendations.spot_diversification import (
    _audit_asg,
    _classify_risk,
    _extract_allocation_strategy,
    _extract_instance_types,
    _extract_spot_pct,
    _make_recommendation,
    _scan_region,
    audit_spot_diversification,
)


# ── _classify_risk ────────────────────────────────────────────────────────────

def test_classify_risk_ok_for_five_types() -> None:
    assert _classify_risk(5) == "OK"


def test_classify_risk_ok_for_ten_types() -> None:
    assert _classify_risk(10) == "OK"


def test_classify_risk_medium_for_three_types() -> None:
    assert _classify_risk(3) == "MEDIUM_RISK"


def test_classify_risk_medium_for_four_types() -> None:
    assert _classify_risk(4) == "MEDIUM_RISK"


def test_classify_risk_high_for_two_types() -> None:
    assert _classify_risk(2) == "HIGH_RISK"


def test_classify_risk_high_for_one_type() -> None:
    assert _classify_risk(1) == "HIGH_RISK"


def test_classify_risk_high_for_zero_types() -> None:
    assert _classify_risk(0) == "HIGH_RISK"


# ── _extract_instance_types ───────────────────────────────────────────────────

def test_extract_instance_types_from_overrides() -> None:
    policy = {
        "LaunchTemplate": {
            "Overrides": [
                {"InstanceType": "m5.large"},
                {"InstanceType": "m5.xlarge"},
                {"InstanceType": "m6i.large"},
            ]
        }
    }
    types = _extract_instance_types(policy, None)
    assert set(types) == {"m5.large", "m5.xlarge", "m6i.large"}


def test_extract_instance_types_none_policy_returns_empty() -> None:
    types = _extract_instance_types(None, None)
    assert types == []


def test_extract_instance_types_falls_back_to_launch_template() -> None:
    # No overrides in policy, but launch template has a type
    policy = {"LaunchTemplate": {"Overrides": []}}
    launch_template = {
        "LaunchTemplateSpecification": {"InstanceType": "c5.xlarge"}
    }
    types = _extract_instance_types(policy, launch_template)
    assert types == ["c5.xlarge"]


# ── _extract_allocation_strategy ──────────────────────────────────────────────

def test_extract_allocation_strategy_capacity_optimized() -> None:
    policy = {
        "InstancesDistribution": {
            "SpotAllocationStrategy": "capacity-optimized"
        }
    }
    assert _extract_allocation_strategy(policy) == "capacity-optimized"


def test_extract_allocation_strategy_lowest_price() -> None:
    policy = {
        "InstancesDistribution": {
            "SpotAllocationStrategy": "lowest-price"
        }
    }
    assert _extract_allocation_strategy(policy) == "lowest-price"


def test_extract_allocation_strategy_none_policy() -> None:
    assert _extract_allocation_strategy(None) == "unknown"


def test_extract_allocation_strategy_missing_key() -> None:
    policy = {"InstancesDistribution": {}}
    assert _extract_allocation_strategy(policy) == "unknown"


# ── _extract_spot_pct ─────────────────────────────────────────────────────────

def test_extract_spot_pct_all_spot() -> None:
    policy = {
        "InstancesDistribution": {
            "OnDemandBaseCapacity": 0,
            "OnDemandPercentageAboveBaseCapacity": 0,
        }
    }
    asg = {"DesiredCapacity": 10}
    assert _extract_spot_pct(policy, asg) == 1.0


def test_extract_spot_pct_mixed() -> None:
    policy = {
        "InstancesDistribution": {
            "OnDemandBaseCapacity": 2,
            "OnDemandPercentageAboveBaseCapacity": 0,
        }
    }
    asg = {"DesiredCapacity": 10}
    # 2 on-demand base, rest spot
    spot_pct = _extract_spot_pct(policy, asg)
    assert spot_pct == round(8 / 10, 3)


def test_extract_spot_pct_zero_desired() -> None:
    policy = {
        "InstancesDistribution": {
            "OnDemandBaseCapacity": 0,
            "OnDemandPercentageAboveBaseCapacity": 0,
        }
    }
    asg = {"DesiredCapacity": 0}
    assert _extract_spot_pct(policy, asg) == 0.0


def test_extract_spot_pct_no_policy() -> None:
    assert _extract_spot_pct(None, {}) == 0.0


# ── _audit_asg ────────────────────────────────────────────────────────────────

def _make_asg(
    name: str = "test-asg",
    desired: int = 5,
    instance_types: list[str] | None = None,
    strategy: str = "capacity-optimized",
    od_base: int = 0,
    od_pct_above: int = 0,
) -> dict:
    instance_types = instance_types or ["m5.large", "m5.xlarge", "m6i.large"]
    return {
        "AutoScalingGroupName": name,
        "DesiredCapacity": desired,
        "MixedInstancesPolicy": {
            "LaunchTemplate": {
                "Overrides": [{"InstanceType": t} for t in instance_types]
            },
            "InstancesDistribution": {
                "SpotAllocationStrategy": strategy,
                "OnDemandBaseCapacity": od_base,
                "OnDemandPercentageAboveBaseCapacity": od_pct_above,
            },
        },
    }


def test_audit_asg_returns_none_for_on_demand_only() -> None:
    # 100% on-demand (od_pct_above=100)
    asg = _make_asg(od_pct_above=100)
    result = _audit_asg(asg, "us-east-1")
    assert result is None


def test_audit_asg_high_risk_for_one_type() -> None:
    asg = _make_asg(instance_types=["m5.large"])
    result = _audit_asg(asg, "us-east-1")
    assert result is not None
    assert result["risk_level"] == "HIGH_RISK"
    assert result["instance_types_count"] == 1


def test_audit_asg_medium_risk_for_three_types() -> None:
    asg = _make_asg(instance_types=["m5.large", "m5.xlarge", "m6i.large"])
    result = _audit_asg(asg, "us-east-1")
    assert result is not None
    assert result["risk_level"] == "MEDIUM_RISK"
    assert result["instance_types_count"] == 3


def test_audit_asg_ok_for_five_types() -> None:
    asg = _make_asg(
        instance_types=["m5.large", "m5.xlarge", "m6i.large", "c5.large", "c5.xlarge"]
    )
    result = _audit_asg(asg, "us-east-1")
    assert result is not None
    assert result["risk_level"] == "OK"
    assert result["instance_types_count"] == 5


def test_audit_asg_output_keys() -> None:
    asg = _make_asg(instance_types=["m5.large", "m6i.xlarge"])
    result = _audit_asg(asg, "eu-west-1")
    assert result is not None
    required = {
        "asg_name", "region", "instance_types_count", "instance_types",
        "allocation_strategy", "spot_pct", "recommendation", "risk_level",
    }
    assert required.issubset(result.keys())
    assert result["region"] == "eu-west-1"
    assert result["asg_name"] == "test-asg"


def test_audit_asg_recommendation_mentions_adding_types_for_high_risk() -> None:
    asg = _make_asg(instance_types=["m5.large"])
    result = _audit_asg(asg, "us-east-1")
    assert result is not None
    assert "Add at least 5 instance types" in result["recommendation"]


def test_audit_asg_capacity_optimized_not_flagged() -> None:
    asg = _make_asg(
        instance_types=["m5.large", "m5.xlarge", "m6i.large", "c5.large", "c5.xlarge"],
        strategy="capacity-optimized",
    )
    result = _audit_asg(asg, "us-east-1")
    assert result is not None
    # Should not warn about strategy when already optimal
    assert "capacity-optimized" not in result["recommendation"] or "Consider" not in result["recommendation"]


def test_audit_asg_lowest_price_strategy_flagged() -> None:
    asg = _make_asg(
        instance_types=["m5.large", "m5.xlarge", "m6i.large", "c5.large", "c5.xlarge"],
        strategy="lowest-price",
    )
    result = _audit_asg(asg, "us-east-1")
    assert result is not None
    assert "capacity-optimized" in result["recommendation"]


# ── _scan_region ──────────────────────────────────────────────────────────────

def test_scan_region_handles_exception_gracefully() -> None:
    asg_client = MagicMock()
    asg_client.get_paginator.side_effect = Exception("access denied")
    results = _scan_region(asg_client, "us-east-1")
    assert results == []


def test_scan_region_returns_only_spot_asgs() -> None:
    # One all-spot ASG, one all-on-demand ASG
    spot_asg = {
        "AutoScalingGroupName": "spot-fleet",
        "DesiredCapacity": 5,
        "MixedInstancesPolicy": {
            "LaunchTemplate": {
                "Overrides": [{"InstanceType": "m5.large"}]
            },
            "InstancesDistribution": {
                "SpotAllocationStrategy": "capacity-optimized",
                "OnDemandBaseCapacity": 0,
                "OnDemandPercentageAboveBaseCapacity": 0,
            },
        },
    }
    od_asg = {
        "AutoScalingGroupName": "od-fleet",
        "DesiredCapacity": 5,
        # No MixedInstancesPolicy = all on-demand
    }
    asg_client = MagicMock()
    asg_client.get_paginator.return_value.paginate.return_value = [
        {"AutoScalingGroups": [spot_asg, od_asg]}
    ]
    results = _scan_region(asg_client, "us-east-1")
    asg_names = [r["asg_name"] for r in results]
    assert "spot-fleet" in asg_names
    assert "od-fleet" not in asg_names


# ── audit_spot_diversification integration ────────────────────────────────────

def test_audit_spot_diversification_sorted_by_risk() -> None:
    """HIGH_RISK should come before MEDIUM_RISK which comes before OK."""
    high_risk_asg = _make_asg("asg-high", instance_types=["m5.large"])
    medium_risk_asg = _make_asg("asg-medium", instance_types=["m5.large", "m5.xlarge", "m6i.large"])
    ok_asg = _make_asg(
        "asg-ok",
        instance_types=["m5.large", "m5.xlarge", "m6i.large", "c5.large", "c5.xlarge"],
    )

    with patch("finops.recommendations.spot_diversification.boto3") as mock_boto3:
        ec2_client = MagicMock()
        asg_client = MagicMock()

        mock_boto3.client.side_effect = lambda svc, **kw: {
            "ec2": ec2_client, "autoscaling": asg_client,
        }.get(svc, MagicMock())

        ec2_client.describe_regions.return_value = {
            "Regions": [{"RegionName": "us-east-1"}]
        }
        asg_client.get_paginator.return_value.paginate.return_value = [
            {"AutoScalingGroups": [ok_asg, high_risk_asg, medium_risk_asg]}
        ]

        results = audit_spot_diversification(regions=["us-east-1"])

    assert len(results) == 3
    risk_levels = [r["risk_level"] for r in results]
    assert risk_levels[0] == "HIGH_RISK"
    assert risk_levels[1] == "MEDIUM_RISK"
    assert risk_levels[2] == "OK"


def test_audit_spot_diversification_empty_when_no_spot_asgs() -> None:
    with patch("finops.recommendations.spot_diversification.boto3") as mock_boto3:
        ec2_client = MagicMock()
        asg_client = MagicMock()

        mock_boto3.client.side_effect = lambda svc, **kw: {
            "ec2": ec2_client, "autoscaling": asg_client,
        }.get(svc, MagicMock())

        ec2_client.describe_regions.return_value = {
            "Regions": [{"RegionName": "us-east-1"}]
        }
        asg_client.get_paginator.return_value.paginate.return_value = [
            {"AutoScalingGroups": []}
        ]

        results = audit_spot_diversification(regions=["us-east-1"])

    assert results == []
