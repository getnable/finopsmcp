"""Tests for finops.recommendations.cloudwatch_alarms."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from finops.recommendations.cloudwatch_alarms import (
    COMPOSITE_ALARM_COST,
    STANDARD_ALARM_COST,
    _days_in_state,
    _instance_exists,
    audit_cloudwatch_orphaned_alarms,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def _make_aws_client():
    return SimpleNamespace(_session=None)


def _now():
    return datetime.now(tz=timezone.utc)


def _make_alarm(
    name: str,
    state: str = "OK",
    namespace: str = "AWS/EC2",
    metric_name: str = "CPUUtilization",
    dimensions: list[dict] | None = None,
    state_updated_days_ago: int = 0,
) -> dict:
    state_updated = _now() - timedelta(days=state_updated_days_ago)
    return {
        "AlarmName": name,
        "StateValue": state,
        "Namespace": namespace,
        "MetricName": metric_name,
        "Dimensions": dimensions or [],
        "StateUpdatedTimestamp": state_updated,
    }


def _make_cw_mock(metric_alarms: list[dict], composite_alarms: list[dict] | None = None) -> MagicMock:
    cw = MagicMock()

    def _make_paginator(operation_name):
        paginator = MagicMock()

        def _paginate(**kwargs):
            alarm_types = kwargs.get("AlarmTypes", [])
            if "CompositeAlarm" in alarm_types and "MetricAlarm" not in alarm_types:
                return iter([{"CompositeAlarms": composite_alarms or []}])
            return iter([{"MetricAlarms": metric_alarms}])

        paginator.paginate.side_effect = _paginate
        return paginator

    cw.get_paginator.side_effect = _make_paginator
    return cw


# ── unit tests: pricing constants ─────────────────────────────────────────────

def test_standard_alarm_cost():
    assert STANDARD_ALARM_COST == 0.10


def test_composite_alarm_cost():
    assert COMPOSITE_ALARM_COST == 0.50


# ── unit tests: days_in_state ──────────────────────────────────────────────────

def test_days_in_state_recent():
    alarm = _make_alarm("test", state_updated_days_ago=3)
    assert _days_in_state(alarm, _now()) == 3


def test_days_in_state_long_ago():
    alarm = _make_alarm("test", state_updated_days_ago=30)
    assert _days_in_state(alarm, _now()) == 30


def test_days_in_state_missing_timestamp():
    alarm = {"AlarmName": "test"}
    assert _days_in_state(alarm, _now()) is None


# ── unit tests: instance_exists ────────────────────────────────────────────────

def test_instance_exists_running():
    ec2 = MagicMock()
    ec2.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"InstanceId": "i-abc", "State": {"Name": "running"}}]}]
    }
    assert _instance_exists(ec2, "i-abc") is True


def test_instance_exists_terminated():
    ec2 = MagicMock()
    ec2.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"InstanceId": "i-abc", "State": {"Name": "terminated"}}]}]
    }
    assert _instance_exists(ec2, "i-abc") is False


def test_instance_exists_api_error():
    ec2 = MagicMock()
    ec2.describe_instances.side_effect = Exception("InvalidInstanceID.NotFound")
    assert _instance_exists(ec2, "i-missing") is False


# ── integration tests ──────────────────────────────────────────────────────────

def test_returns_required_structure():
    cw_mock = _make_cw_mock(metric_alarms=[])
    with patch("finops.recommendations.cloudwatch_alarms._make_cw", return_value=cw_mock), \
         patch("finops.recommendations.cloudwatch_alarms._make_ec2", return_value=MagicMock()), \
         patch("finops.recommendations.cloudwatch_alarms._make_sqs", return_value=MagicMock()):
        result = _run(audit_cloudwatch_orphaned_alarms(_make_aws_client(), regions=["us-east-1"]))

    assert "total_alarms" in result
    assert "total_orphaned" in result
    assert "total_monthly_waste" in result
    assert "orphaned_alarms" in result
    assert "by_region" in result


def test_ok_alarm_not_flagged():
    """Alarms in OK state should not be flagged as orphaned."""
    alarms = [_make_alarm("healthy-alarm", state="OK")]
    cw_mock = _make_cw_mock(metric_alarms=alarms)
    with patch("finops.recommendations.cloudwatch_alarms._make_cw", return_value=cw_mock), \
         patch("finops.recommendations.cloudwatch_alarms._make_ec2", return_value=MagicMock()), \
         patch("finops.recommendations.cloudwatch_alarms._make_sqs", return_value=MagicMock()):
        result = _run(audit_cloudwatch_orphaned_alarms(_make_aws_client(), regions=["us-east-1"]))

    assert result["total_orphaned"] == 0
    assert result["total_monthly_waste"] == 0.0


def test_insufficient_data_recent_not_flagged():
    """INSUFFICIENT_DATA alarm less than 7 days old should not be flagged."""
    alarms = [_make_alarm("new-alarm", state="INSUFFICIENT_DATA", state_updated_days_ago=3)]
    cw_mock = _make_cw_mock(metric_alarms=alarms)
    with patch("finops.recommendations.cloudwatch_alarms._make_cw", return_value=cw_mock), \
         patch("finops.recommendations.cloudwatch_alarms._make_ec2", return_value=MagicMock()), \
         patch("finops.recommendations.cloudwatch_alarms._make_sqs", return_value=MagicMock()):
        result = _run(audit_cloudwatch_orphaned_alarms(_make_aws_client(), regions=["us-east-1"]))

    assert result["total_orphaned"] == 0


def test_insufficient_data_old_flagged():
    """INSUFFICIENT_DATA alarm older than 7 days should be flagged."""
    alarms = [_make_alarm("stale-alarm", state="INSUFFICIENT_DATA", state_updated_days_ago=14)]
    cw_mock = _make_cw_mock(metric_alarms=alarms)
    with patch("finops.recommendations.cloudwatch_alarms._make_cw", return_value=cw_mock), \
         patch("finops.recommendations.cloudwatch_alarms._make_ec2", return_value=MagicMock()), \
         patch("finops.recommendations.cloudwatch_alarms._make_sqs", return_value=MagicMock()):
        result = _run(audit_cloudwatch_orphaned_alarms(_make_aws_client(), regions=["us-east-1"]))

    assert result["total_orphaned"] == 1
    assert result["total_monthly_waste"] == STANDARD_ALARM_COST


def test_ec2_alarm_resource_does_not_exist():
    """EC2 alarm where the instance no longer exists: resource_exists=False."""
    alarms = [
        _make_alarm(
            "dead-ec2-alarm",
            state="INSUFFICIENT_DATA",
            state_updated_days_ago=10,
            namespace="AWS/EC2",
            dimensions=[{"Name": "InstanceId", "Value": "i-terminated999"}],
        )
    ]
    ec2_mock = MagicMock()
    ec2_mock.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"InstanceId": "i-terminated999", "State": {"Name": "terminated"}}]}]
    }
    cw_mock = _make_cw_mock(metric_alarms=alarms)
    with patch("finops.recommendations.cloudwatch_alarms._make_cw", return_value=cw_mock), \
         patch("finops.recommendations.cloudwatch_alarms._make_ec2", return_value=ec2_mock), \
         patch("finops.recommendations.cloudwatch_alarms._make_sqs", return_value=MagicMock()):
        result = _run(audit_cloudwatch_orphaned_alarms(_make_aws_client(), regions=["us-east-1"]))

    assert result["total_orphaned"] == 1
    orphan = result["orphaned_alarms"][0]
    assert orphan["resource_exists"] is False
    assert orphan["alarm_name"] == "dead-ec2-alarm"


def test_monthly_waste_calculation():
    """Two orphaned standard alarms = 2 * $0.10 = $0.20/month."""
    alarms = [
        _make_alarm("alarm-1", state="INSUFFICIENT_DATA", state_updated_days_ago=20),
        _make_alarm("alarm-2", state="INSUFFICIENT_DATA", state_updated_days_ago=15),
    ]
    cw_mock = _make_cw_mock(metric_alarms=alarms)
    with patch("finops.recommendations.cloudwatch_alarms._make_cw", return_value=cw_mock), \
         patch("finops.recommendations.cloudwatch_alarms._make_ec2", return_value=MagicMock()), \
         patch("finops.recommendations.cloudwatch_alarms._make_sqs", return_value=MagicMock()):
        result = _run(audit_cloudwatch_orphaned_alarms(_make_aws_client(), regions=["us-east-1"]))

    assert result["total_orphaned"] == 2
    assert abs(result["total_monthly_waste"] - 2 * STANDARD_ALARM_COST) < 0.001


def test_by_region_populated():
    cw_mock = _make_cw_mock(metric_alarms=[])
    with patch("finops.recommendations.cloudwatch_alarms._make_cw", return_value=cw_mock), \
         patch("finops.recommendations.cloudwatch_alarms._make_ec2", return_value=MagicMock()), \
         patch("finops.recommendations.cloudwatch_alarms._make_sqs", return_value=MagicMock()):
        result = _run(audit_cloudwatch_orphaned_alarms(_make_aws_client(), regions=["us-east-1", "eu-west-1"]))

    assert "us-east-1" in result["by_region"]
    assert "eu-west-1" in result["by_region"]
    for data in result["by_region"].values():
        assert "total_alarms" in data
        assert "orphaned_count" in data
        assert "monthly_waste" in data
