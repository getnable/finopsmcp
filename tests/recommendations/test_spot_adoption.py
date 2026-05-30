"""Tests for finops.recommendations.spot_adoption."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from finops.recommendations.spot_adoption import (
    SPOT_DISCOUNT,
    SPOT_INTERRUPTION_FREQ,
    _classify,
    _get_asg_members,
    _get_cpu_variance,
    _get_interruption_freq,
    _get_spot_discount,
    _is_stateless,
    _monthly_ondemand_cost,
    recommend_spot_adoption,
)


# ── SPOT_DISCOUNT and SPOT_INTERRUPTION_FREQ maps ────────────────────────────

def test_spot_discount_has_default() -> None:
    assert "_default" in SPOT_DISCOUNT
    assert 0.0 < SPOT_DISCOUNT["_default"] < 1.0


def test_spot_interruption_freq_has_default() -> None:
    assert "_default" in SPOT_INTERRUPTION_FREQ
    assert 0.0 < SPOT_INTERRUPTION_FREQ["_default"] < 1.0


def test_spot_discount_known_types() -> None:
    assert SPOT_DISCOUNT["m5.large"] == 0.72
    assert SPOT_DISCOUNT["c5.large"] == 0.75
    assert SPOT_DISCOUNT["r5.large"] == 0.65


def test_get_spot_discount_fallback() -> None:
    assert _get_spot_discount("x99.hugemachine") == SPOT_DISCOUNT["_default"]


def test_get_interruption_freq_fallback() -> None:
    assert _get_interruption_freq("x99.hugemachine") == SPOT_INTERRUPTION_FREQ["_default"]


# ── _monthly_ondemand_cost ────────────────────────────────────────────────────

def test_monthly_ondemand_cost_known_type() -> None:
    # m5.large is $0.096/hr * 730 hrs
    cost = _monthly_ondemand_cost("m5.large")
    assert cost == round(0.096 * 730.0, 2)
    assert cost > 0


def test_monthly_ondemand_cost_unknown_type_returns_zero() -> None:
    assert _monthly_ondemand_cost("z99.unknown") == 0.0


# ── _is_stateless ─────────────────────────────────────────────────────────────

def test_is_stateless_dev_env_tag() -> None:
    inst = {"Tags": [{"Key": "environment", "Value": "dev"}]}
    assert _is_stateless(inst) is True


def test_is_stateless_staging_env_tag() -> None:
    inst = {"Tags": [{"Key": "env", "Value": "staging"}]}
    assert _is_stateless(inst) is True


def test_is_stateless_test_env_tag() -> None:
    inst = {"Tags": [{"Key": "stage", "Value": "test"}]}
    assert _is_stateless(inst) is True


def test_is_not_stateless_prod() -> None:
    inst = {"Tags": [{"Key": "env", "Value": "prod"}]}
    assert _is_stateless(inst) is False


def test_is_not_stateless_no_env_tag() -> None:
    inst = {"Tags": [{"Key": "Name", "Value": "web-server"}]}
    assert _is_stateless(inst) is False


# ── _classify ─────────────────────────────────────────────────────────────────

def test_classify_recommended_low_freq_stateless_in_asg() -> None:
    # freq=0.02 (<5%), in_asg=True, stateless=True, low variance
    result = _classify(0.02, in_asg=True, is_stateless=True, cpu_variance=5.0)
    assert result == "RECOMMENDED"


def test_classify_possible_medium_freq() -> None:
    # freq=0.08 (8%, between 5% and 15%), in_asg=True, stateless=True
    result = _classify(0.08, in_asg=True, is_stateless=True, cpu_variance=5.0)
    assert result == "POSSIBLE"


def test_classify_not_recommended_high_freq() -> None:
    # freq=0.20 (>15%)
    result = _classify(0.20, in_asg=True, is_stateless=True, cpu_variance=5.0)
    assert result == "NOT_RECOMMENDED"


def test_classify_not_recommended_stateful() -> None:
    # Stateful (prod) even with low freq
    result = _classify(0.02, in_asg=True, is_stateless=False, cpu_variance=5.0)
    assert result == "NOT_RECOMMENDED"


def test_classify_not_recommended_high_variance() -> None:
    # High CPU variance makes spot risky
    result = _classify(0.02, in_asg=True, is_stateless=True, cpu_variance=35.0)
    assert result == "NOT_RECOMMENDED"


def test_classify_not_recommended_not_in_asg_high_freq() -> None:
    result = _classify(0.20, in_asg=False, is_stateless=True, cpu_variance=5.0)
    assert result == "NOT_RECOMMENDED"


# ── _get_cpu_variance ─────────────────────────────────────────────────────────

def test_get_cpu_variance_returns_stddev() -> None:
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {
        "Datapoints": [
            {"Average": 10.0},
            {"Average": 90.0},
            {"Average": 50.0},
            {"Average": 20.0},
        ]
    }
    variance = _get_cpu_variance(cw, "i-abc123", days=14)
    assert variance > 0.0


def test_get_cpu_variance_returns_zero_on_empty() -> None:
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {"Datapoints": []}
    variance = _get_cpu_variance(cw, "i-noop", days=14)
    assert variance == 0.0


def test_get_cpu_variance_returns_zero_on_exception() -> None:
    cw = MagicMock()
    cw.get_metric_statistics.side_effect = Exception("access denied")
    variance = _get_cpu_variance(cw, "i-fail", days=14)
    assert variance == 0.0


# ── _get_asg_members ──────────────────────────────────────────────────────────

def test_get_asg_members_returns_instance_ids() -> None:
    asg_client = MagicMock()
    asg_client.get_paginator.return_value.paginate.return_value = [
        {
            "AutoScalingGroups": [
                {
                    "AutoScalingGroupName": "my-asg",
                    "Instances": [
                        {"InstanceId": "i-aaa111"},
                        {"InstanceId": "i-bbb222"},
                    ],
                }
            ]
        }
    ]
    members = _get_asg_members(asg_client, ["us-east-1"])
    assert "i-aaa111" in members
    assert "i-bbb222" in members


def test_get_asg_members_handles_exception_gracefully() -> None:
    asg_client = MagicMock()
    asg_client.get_paginator.side_effect = Exception("access denied")
    members = _get_asg_members(asg_client, ["us-east-1"])
    assert isinstance(members, set)
    assert len(members) == 0


# ── recommend_spot_adoption integration ──────────────────────────────────────

def _make_ec2_page(instances: list[dict]) -> list[dict]:
    return [{"Reservations": [{"Instances": instances}]}]


def _make_instance(
    instance_id: str = "i-abc123",
    instance_type: str = "m5.large",
    lifecycle: str | None = None,
    env_tag: str = "staging",
) -> dict:
    tags = [{"Key": "Name", "Value": "test-server"}]
    if env_tag:
        tags.append({"Key": "env", "Value": env_tag})
    inst = {
        "InstanceId":   instance_id,
        "InstanceType": instance_type,
        "Tags":         tags,
    }
    if lifecycle:
        inst["InstanceLifecycle"] = lifecycle
    return inst


def _make_metric_data_response_empty(instance_ids: list[str]) -> dict:
    """Return a get_metric_data response with no data points per instance."""
    return {
        "MetricDataResults": [
            {"Id": f"m{i}", "Timestamps": [], "Values": []}
            for i, _ in enumerate(instance_ids)
        ]
    }


def test_recommend_spot_skips_spot_instances() -> None:
    """Instances already on spot must be excluded."""
    spot_inst = _make_instance("i-spot", "m5.large", lifecycle="spot")

    with patch("finops.recommendations.spot_adoption.boto3") as mock_boto3:
        ec2    = MagicMock()
        cw     = MagicMock()
        asg    = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "ec2": ec2, "cloudwatch": cw, "autoscaling": asg,
        }[svc]

        ec2.describe_regions.return_value = {
            "Regions": [{"RegionName": "us-east-1"}]
        }
        ec2.get_paginator.return_value.paginate.return_value = _make_ec2_page([spot_inst])
        asg.get_paginator.return_value.paginate.return_value = [{"AutoScalingGroups": []}]
        cw.get_metric_data.return_value = {"MetricDataResults": []}

        results = recommend_spot_adoption(regions=["us-east-1"])
    assert results == []


def test_recommend_spot_output_structure() -> None:
    """Each result must contain the required keys."""
    inst = _make_instance("i-od123", "m5.large", env_tag="staging")

    with patch("finops.recommendations.spot_adoption.boto3") as mock_boto3:
        ec2    = MagicMock()
        cw     = MagicMock()
        asg    = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "ec2": ec2, "cloudwatch": cw, "autoscaling": asg,
        }[svc]

        ec2.describe_regions.return_value = {
            "Regions": [{"RegionName": "us-east-1"}]
        }
        ec2.get_paginator.return_value.paginate.return_value = _make_ec2_page([inst])
        asg.get_paginator.return_value.paginate.return_value = [
            {
                "AutoScalingGroups": [
                    {
                        "AutoScalingGroupName": "my-asg",
                        "Instances": [{"InstanceId": "i-od123"}],
                    }
                ]
            }
        ]
        cw.get_metric_data.return_value = _make_metric_data_response_empty(["i-od123"])

        results = recommend_spot_adoption(regions=["us-east-1"])

    assert len(results) == 1
    r = results[0]
    required_keys = {
        "instance_id", "instance_type", "name", "region", "environment",
        "in_asg", "interruption_freq_pct", "recommendation",
        "monthly_ondemand_cost", "monthly_spot_estimate",
        "monthly_savings", "savings_pct",
    }
    assert required_keys.issubset(r.keys())


def test_recommend_spot_sorted_by_savings_desc() -> None:
    """Results must be sorted by monthly_savings descending."""
    instances = [
        _make_instance("i-small", "t3.medium",  env_tag="staging"),
        _make_instance("i-large", "m5.2xlarge", env_tag="staging"),
        _make_instance("i-mid",   "m5.xlarge",  env_tag="staging"),
    ]

    with patch("finops.recommendations.spot_adoption.boto3") as mock_boto3:
        ec2    = MagicMock()
        cw     = MagicMock()
        asg    = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "ec2": ec2, "cloudwatch": cw, "autoscaling": asg,
        }[svc]

        ec2.describe_regions.return_value = {
            "Regions": [{"RegionName": "us-east-1"}]
        }
        ec2.get_paginator.return_value.paginate.return_value = _make_ec2_page(instances)
        asg.get_paginator.return_value.paginate.return_value = [{"AutoScalingGroups": []}]
        cw.get_metric_data.return_value = _make_metric_data_response_empty(
            ["i-small", "i-large", "i-mid"]
        )

        results = recommend_spot_adoption(regions=["us-east-1"])

    assert len(results) == 3
    savings = [r["monthly_savings"] for r in results]
    assert savings == sorted(savings, reverse=True)


def test_recommend_spot_prod_instance_not_recommended() -> None:
    """Production instances without ASG should be NOT_RECOMMENDED."""
    inst = _make_instance("i-prod", "m5.large", env_tag="prod")

    with patch("finops.recommendations.spot_adoption.boto3") as mock_boto3:
        ec2    = MagicMock()
        cw     = MagicMock()
        asg    = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "ec2": ec2, "cloudwatch": cw, "autoscaling": asg,
        }[svc]

        ec2.describe_regions.return_value = {
            "Regions": [{"RegionName": "us-east-1"}]
        }
        ec2.get_paginator.return_value.paginate.return_value = _make_ec2_page([inst])
        asg.get_paginator.return_value.paginate.return_value = [{"AutoScalingGroups": []}]
        cw.get_metric_data.return_value = _make_metric_data_response_empty(["i-prod"])

        results = recommend_spot_adoption(regions=["us-east-1"])

    assert len(results) == 1
    assert results[0]["recommendation"] == "NOT_RECOMMENDED"
    assert results[0]["in_asg"] is False
