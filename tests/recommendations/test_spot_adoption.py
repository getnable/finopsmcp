"""Tests for finops.recommendations.spot_adoption."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from finops.analyzers.cloudwatch import GET_METRIC_DATA_ENV
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


@pytest.fixture(autouse=True)
def _free_path_unless_asked(monkeypatch):
    """A developer shell with the GetMetricData opt-in set must not move these
    reads onto the billed path, which the fakes here do not answer."""
    monkeypatch.delenv(GET_METRIC_DATA_ENV, raising=False)


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


def test_batched_cpu_variance_is_the_spread_of_the_whole_series() -> None:
    """GetMetricStatistics answers at most 1,440 datapoints a call and 14 days
    of hourly points is 336, so one call per instance returns the whole series.
    CloudWatch hands the datapoints back in no particular order; the spread is
    of all of them."""
    import statistics
    from datetime import datetime, timedelta, timezone

    from finops.recommendations.spot_adoption import _batch_get_cpu_variance

    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    cw = MagicMock()
    cw.get_metric_statistics.return_value = {"Datapoints": [
        {"Timestamp": t0 + timedelta(hours=3), "Average": 90.0},
        {"Timestamp": t0, "Average": 10.0},
        {"Timestamp": t0 + timedelta(hours=2), "Average": 90.0},
        {"Timestamp": t0 + timedelta(hours=1), "Average": 10.0},
    ]}

    got = _batch_get_cpu_variance(cw, ["i-abc123"], days=14)

    assert cw.get_metric_statistics.call_count == 1
    kw = cw.get_metric_statistics.call_args.kwargs
    assert kw["Statistics"] == ["Average"] and kw["Period"] == 3600
    assert (kw["EndTime"] - kw["StartTime"]).total_seconds() / kw["Period"] <= 1440
    assert got == {"i-abc123": pytest.approx(statistics.stdev([10.0, 10.0, 90.0, 90.0]))}


def test_batched_cpu_variance_is_none_for_a_series_it_could_not_read() -> None:
    # Not 0.0: zero variance is the "stable load" half of a RECOMMENDED
    # verdict, and a denied read is no evidence of stable load.
    from finops.recommendations.spot_adoption import _batch_get_cpu_variance

    cw = MagicMock()
    cw.get_metric_statistics.side_effect = Exception("AccessDenied")
    assert _batch_get_cpu_variance(cw, ["i-abc123"], days=14) == {"i-abc123": None}


def test_batched_cpu_variance_is_zero_for_an_empty_read() -> None:
    from finops.recommendations.spot_adoption import _batch_get_cpu_variance

    cw = MagicMock()
    cw.get_metric_statistics.return_value = {"Datapoints": []}
    assert _batch_get_cpu_variance(cw, ["i-abc123"], days=14) == {"i-abc123": 0.0}


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


# A successful GetMetricStatistics read that found no datapoints.
_EMPTY_READ: dict = {"Datapoints": []}


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
        cw.get_metric_statistics.return_value = _EMPTY_READ

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
        cw.get_metric_statistics.return_value = _EMPTY_READ

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
        cw.get_metric_statistics.return_value = _EMPTY_READ

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
        cw.get_metric_statistics.return_value = _EMPTY_READ

        results = recommend_spot_adoption(regions=["us-east-1"])

    assert len(results) == 1
    assert results[0]["recommendation"] == "NOT_RECOMMENDED"
    assert results[0]["in_asg"] is False


def test_recommend_spot_never_calls_get_metric_data() -> None:
    """GetMetricData bills per metric with no free tier. The CPU reads behind a
    spot recommendation are one free GetMetricStatistics call per instance,
    and nothing billed."""
    instances = [
        _make_instance("i-a", "m5.large", env_tag="staging"),
        _make_instance("i-b", "m5.xlarge", env_tag="dev"),
        _make_instance("i-c", "c5.large", env_tag="prod"),
    ]

    with patch("finops.recommendations.spot_adoption.boto3") as mock_boto3:
        ec2    = MagicMock()
        cw     = MagicMock()
        asg    = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "ec2": ec2, "cloudwatch": cw, "autoscaling": asg,
        }[svc]

        ec2.get_paginator.return_value.paginate.return_value = _make_ec2_page(instances)
        asg.get_paginator.return_value.paginate.return_value = [{"AutoScalingGroups": []}]
        cw.get_metric_statistics.return_value = _EMPTY_READ

        results = recommend_spot_adoption(regions=["us-east-1"])

    assert len(results) == 3
    cw.get_metric_data.assert_not_called()
    assert cw.get_metric_statistics.call_count == 3


def test_an_instance_whose_cpu_read_failed_is_skipped_and_counted() -> None:
    """A failed CloudWatch read used to become zero variance, the stable-load
    signal a RECOMMENDED verdict rests on. Now the instance gets no verdict,
    and the result and the finding say how many were not assessed."""
    from datetime import datetime, timezone

    readable = _make_instance("i-read", "m5.large", env_tag="staging")
    denied = _make_instance("i-denied", "m5.large", env_tag="staging")

    def cpu(**kw):
        if kw["Dimensions"][0]["Value"] == "i-denied":
            raise Exception("AccessDenied")
        now = datetime.now(timezone.utc)
        return {"Datapoints": [{"Timestamp": now, "Average": 5.0}] * 3}

    with patch("finops.recommendations.spot_adoption.boto3") as mock_boto3:
        ec2, cw, asg = MagicMock(), MagicMock(), MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: {
            "ec2": ec2, "cloudwatch": cw, "autoscaling": asg,
        }[svc]
        ec2.get_paginator.return_value.paginate.return_value = _make_ec2_page(
            [readable, denied])
        asg.get_paginator.return_value.paginate.return_value = [{"AutoScalingGroups": [{
            "AutoScalingGroupName": "web",
            "Instances": [{"InstanceId": "i-read"}, {"InstanceId": "i-denied"}],
        }]}]
        cw.get_metric_statistics.side_effect = cpu

        results = recommend_spot_adoption(regions=["us-east-1"])

    assert [r["instance_id"] for r in results] == ["i-read"]
    assert results.cpu_unread_instances == ["i-denied"]
    assert results[0]["recommendation"] in ("RECOMMENDED", "POSSIBLE")
    finding = results[0]["finding"]
    assert finding["metadata"]["instances_cpu_unread"] == 1
    assert any("1 on-demand instance(s) were not assessed" in a
               for a in finding["assumptions"])
