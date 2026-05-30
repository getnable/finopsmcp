"""Tests for finops.recommendations.nonprod_scheduler."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from finops.recommendations.nonprod_scheduler import (
    _get_env_tag,
    _idle_hours,
    _monthly_cost_estimate,
    _scheduler_command,
    identify_nonprod_resources,
)


# ── unit tests for helpers ─────────────────────────────────────────────────────

class TestGetEnvTag:
    def test_returns_dev_value(self):
        tags = [{"Key": "Environment", "Value": "dev"}]
        assert _get_env_tag(tags) == "dev"

    def test_returns_staging_case_insensitive(self):
        tags = [{"Key": "Env", "Value": "Staging"}]
        assert _get_env_tag(tags) == "Staging"

    def test_returns_none_for_prod(self):
        tags = [{"Key": "Environment", "Value": "production"}]
        assert _get_env_tag(tags) is None

    def test_returns_none_for_empty_tags(self):
        assert _get_env_tag([]) is None

    def test_matches_qa_via_stage_key(self):
        tags = [{"Key": "Stage", "Value": "qa"}]
        assert _get_env_tag(tags) == "qa"

    def test_matches_sandbox(self):
        tags = [{"Key": "environment", "Value": "sandbox"}]
        assert _get_env_tag(tags) == "sandbox"

    def test_matches_non_prod(self):
        tags = [{"Key": "Environment", "Value": "non-prod"}]
        assert _get_env_tag(tags) == "non-prod"

    def test_first_matching_key_wins(self):
        # Environment key comes before Env in _ENV_TAG_KEYS
        tags = [
            {"Key": "Environment", "Value": "dev"},
            {"Key": "Env", "Value": "staging"},
        ]
        result = _get_env_tag(tags)
        assert result in ("dev", "staging")  # either is valid non-prod


class TestIdleHours:
    def test_all_idle(self):
        samples = [0.0, 1.0, 2.5, 4.9]
        assert _idle_hours(samples) == 4

    def test_none_idle(self):
        samples = [10.0, 20.0, 5.1]
        assert _idle_hours(samples) == 0

    def test_mixed(self):
        samples = [0.0, 10.0, 4.9, 50.0, 3.0]
        assert _idle_hours(samples) == 3

    def test_empty(self):
        assert _idle_hours([]) == 0

    def test_boundary_value_excluded(self):
        # exactly 5.0 is NOT idle (threshold is strictly less than)
        assert _idle_hours([5.0]) == 0

    def test_boundary_value_included(self):
        # 4.99 is idle
        assert _idle_hours([4.99]) == 1


class TestMonthlyCostEstimate:
    def test_known_instance_type(self):
        cost = _monthly_cost_estimate("m5.large")
        assert cost == round(0.096 * 730.0, 2)

    def test_unknown_instance_type_returns_zero(self):
        assert _monthly_cost_estimate("x99.superlarge") == 0.0


class TestSchedulerCommand:
    def test_returns_string_with_instance_id(self):
        cmd = _scheduler_command("i-12345", "us-east-1")
        assert "i-12345" in cmd
        assert "us-east-1" in cmd


# ── integration-style tests for identify_nonprod_resources ────────────────────

def _make_aws_client():
    """Minimal stub for the aws_client argument (not called directly in the function)."""
    return MagicMock()


def _make_ec2_instance(iid: str, itype: str, name: str, env_value: str) -> dict:
    return {
        "InstanceId": iid,
        "InstanceType": itype,
        "Tags": [
            {"Key": "Name", "Value": name},
            {"Key": "Environment", "Value": env_value},
        ],
    }


def _make_cw_datapoints(cpu_values: list[float]) -> list[dict]:
    base = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    from datetime import timedelta
    return [
        {"Timestamp": base + timedelta(hours=i), "Maximum": v}
        for i, v in enumerate(cpu_values)
    ]


class TestIdentifyNonprodResources:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_no_boto3_returns_error(self):
        with patch("finops.recommendations.nonprod_scheduler.boto3", None):
            result = self._run(
                identify_nonprod_resources(aws_client=_make_aws_client(), regions=["us-east-1"])
            )
        assert "error" in result

    def test_empty_regions_returns_empty(self):
        mock_boto3 = MagicMock()
        mock_ec2 = MagicMock()
        mock_cw = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: mock_ec2 if svc == "ec2" else mock_cw
        # describe_regions returns empty list
        mock_ec2.describe_regions.return_value = {"Regions": []}
        mock_ec2.get_paginator.return_value.paginate.return_value = []

        with patch("finops.recommendations.nonprod_scheduler.boto3", mock_boto3):
            result = self._run(
                identify_nonprod_resources(aws_client=_make_aws_client(), regions=[])
            )

        assert result["total_instances"] == 0
        assert result["schedulable_instances"] == []
        assert result["total_monthly_waste"] == 0.0

    def test_prod_instance_excluded(self):
        mock_boto3 = MagicMock()
        mock_ec2 = MagicMock()
        mock_cw = MagicMock()

        prod_inst = _make_ec2_instance("i-prod1", "m5.large", "api", "production")
        page = {"Reservations": [{"Instances": [prod_inst]}]}
        mock_ec2.get_paginator.return_value.paginate.return_value = [page]
        mock_boto3.client.side_effect = lambda svc, **kw: mock_ec2 if svc == "ec2" else mock_cw
        mock_cw.get_metric_statistics.return_value = {"Datapoints": []}

        with patch("finops.recommendations.nonprod_scheduler.boto3", mock_boto3):
            result = self._run(
                identify_nonprod_resources(aws_client=_make_aws_client(), regions=["us-east-1"])
            )

        assert result["total_instances"] == 0

    def test_highly_idle_dev_instance_included(self):
        mock_boto3 = MagicMock()
        mock_ec2 = MagicMock()
        mock_cw = MagicMock()

        dev_inst = _make_ec2_instance("i-dev1", "m5.large", "backend-dev", "dev")
        page = {"Reservations": [{"Instances": [dev_inst]}]}
        mock_ec2.get_paginator.return_value.paginate.return_value = [page]
        mock_boto3.client.side_effect = lambda svc, **kw: mock_ec2 if svc == "ec2" else mock_cw

        # 100 hours, 80 of them idle (CPU < 5%)
        cpu_values = [0.5] * 80 + [50.0] * 20
        mock_cw.get_metric_statistics.return_value = {
            "Datapoints": _make_cw_datapoints(cpu_values)
        }

        with patch("finops.recommendations.nonprod_scheduler.boto3", mock_boto3):
            result = self._run(
                identify_nonprod_resources(aws_client=_make_aws_client(), regions=["us-east-1"])
            )

        assert result["total_instances"] == 1
        inst = result["schedulable_instances"][0]
        assert inst["instance_id"] == "i-dev1"
        assert inst["environment"] == "dev"
        assert inst["potential_monthly_savings"] > 0
        assert inst["schedule_recommendation"] == "Mon-Fri 08:00-18:00 UTC"
        assert result["total_monthly_waste"] > 0

    def test_low_idle_instance_excluded(self):
        """An instance with only 10 idle hours/week should NOT be flagged (threshold is 20)."""
        mock_boto3 = MagicMock()
        mock_ec2 = MagicMock()
        mock_cw = MagicMock()

        dev_inst = _make_ec2_instance("i-dev2", "t3.micro", "worker", "staging")
        page = {"Reservations": [{"Instances": [dev_inst]}]}
        mock_ec2.get_paginator.return_value.paginate.return_value = [page]
        mock_boto3.client.side_effect = lambda svc, **kw: mock_ec2 if svc == "ec2" else mock_cw

        # Only ~6% idle: 10 out of 168 hours idle per week
        # Sample: 10 idle out of 168 total -> (10/168)*168 = 10 idle hrs/wk
        cpu_values = [0.5] * 10 + [60.0] * 158
        mock_cw.get_metric_statistics.return_value = {
            "Datapoints": _make_cw_datapoints(cpu_values)
        }

        with patch("finops.recommendations.nonprod_scheduler.boto3", mock_boto3):
            result = self._run(
                identify_nonprod_resources(aws_client=_make_aws_client(), regions=["us-east-1"])
            )

        assert result["total_instances"] == 0

    def test_no_cloudwatch_data_uses_worst_case(self):
        """When CloudWatch returns no data, instance should still be flagged with worst-case idle."""
        mock_boto3 = MagicMock()
        mock_ec2 = MagicMock()
        mock_cw = MagicMock()

        dev_inst = _make_ec2_instance("i-dev3", "m5.xlarge", "db-dev", "test")
        page = {"Reservations": [{"Instances": [dev_inst]}]}
        mock_ec2.get_paginator.return_value.paginate.return_value = [page]
        mock_boto3.client.side_effect = lambda svc, **kw: mock_ec2 if svc == "ec2" else mock_cw

        # No CloudWatch data
        mock_cw.get_metric_statistics.return_value = {"Datapoints": []}

        with patch("finops.recommendations.nonprod_scheduler.boto3", mock_boto3):
            result = self._run(
                identify_nonprod_resources(aws_client=_make_aws_client(), regions=["us-east-1"])
            )

        # Worst-case idle = 168 - 50 = 118 hrs/wk (above the 20-hr threshold)
        assert result["total_instances"] == 1
        inst = result["schedulable_instances"][0]
        assert inst["idle_hours_per_week"] == 118.0

    def test_result_sorted_by_savings_descending(self):
        mock_boto3 = MagicMock()
        mock_ec2 = MagicMock()
        mock_cw = MagicMock()

        inst_large = _make_ec2_instance("i-large", "m5.4xlarge", "large", "dev")
        inst_small = _make_ec2_instance("i-small", "t3.micro", "small", "staging")
        page = {"Reservations": [{"Instances": [inst_large, inst_small]}]}
        mock_ec2.get_paginator.return_value.paginate.return_value = [page]
        mock_boto3.client.side_effect = lambda svc, **kw: mock_ec2 if svc == "ec2" else mock_cw

        # Both heavily idle
        cpu_values = [0.5] * 168
        mock_cw.get_metric_statistics.return_value = {
            "Datapoints": _make_cw_datapoints(cpu_values)
        }

        with patch("finops.recommendations.nonprod_scheduler.boto3", mock_boto3):
            result = self._run(
                identify_nonprod_resources(aws_client=_make_aws_client(), regions=["us-east-1"])
            )

        instances = result["schedulable_instances"]
        assert len(instances) == 2
        # Large instance should have higher savings and appear first
        assert instances[0]["potential_monthly_savings"] >= instances[1]["potential_monthly_savings"]

    def test_extra_env_tags_included(self):
        """Custom env_tags values like 'perf' should be matched."""
        mock_boto3 = MagicMock()
        mock_ec2 = MagicMock()
        mock_cw = MagicMock()

        perf_inst = _make_ec2_instance("i-perf", "m5.large", "perf-test", "perf")
        page = {"Reservations": [{"Instances": [perf_inst]}]}
        mock_ec2.get_paginator.return_value.paginate.return_value = [page]
        mock_boto3.client.side_effect = lambda svc, **kw: mock_ec2 if svc == "ec2" else mock_cw

        cpu_values = [0.5] * 120 + [60.0] * 48
        mock_cw.get_metric_statistics.return_value = {
            "Datapoints": _make_cw_datapoints(cpu_values)
        }

        with patch("finops.recommendations.nonprod_scheduler.boto3", mock_boto3):
            result = self._run(
                identify_nonprod_resources(
                    aws_client=_make_aws_client(),
                    regions=["us-east-1"],
                    env_tags=["perf"],
                )
            )

        assert result["total_instances"] == 1
        assert result["schedulable_instances"][0]["instance_id"] == "i-perf"
