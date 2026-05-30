"""
Non-production environment scheduler recommendations.

Dev, staging, test, and sandbox EC2 instances typically run 24/7 but only
need to run during business hours (Monday-Friday 08:00-18:00 UTC). Scheduling
them saves 60-70% of their compute cost.

Logic:
  1. Find instances tagged with common non-prod environment tag values.
  2. Pull hourly CloudWatch CPUUtilization over 7 days.
  3. Flag hours where CPU < 5% as idle.
  4. Calculate savings from scheduling to business hours only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

try:
    import boto3 as boto3
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore[assignment]

_HOURS_PER_MONTH = 730.0
_BUSINESS_HOURS_PER_WEEK = 50.0   # Mon-Fri 08:00-18:00 = 10 hrs * 5 days
_TOTAL_HOURS_PER_WEEK = 168.0
_IDLE_CPU_THRESHOLD = 5.0          # % CPU below which hour is considered idle
_LOOKBACK_DAYS = 7

# Tag keys to inspect for environment labels
_ENV_TAG_KEYS = ["Environment", "Env", "environment", "Stage", "stage"]

# Values that indicate a non-production environment (matched case-insensitively)
_NONPROD_VALUES = {
    "dev", "development", "staging", "stage", "test", "testing",
    "qa", "sandbox", "nonprod", "non-prod",
}

# On-demand hourly prices (us-east-1) for cost estimation when no billing data
_HOURLY_PRICE: dict[str, float] = {
    "t3.nano": 0.0052,    "t3.micro": 0.0104,   "t3.small": 0.0208,
    "t3.medium": 0.0416,  "t3.large": 0.0832,   "t3.xlarge": 0.1664,
    "t3.2xlarge": 0.3328,
    "t3a.nano": 0.0047,   "t3a.micro": 0.0094,  "t3a.small": 0.0188,
    "t3a.medium": 0.0376, "t3a.large": 0.0752,  "t3a.xlarge": 0.1504,
    "t3a.2xlarge": 0.3008,
    "m5.large": 0.096,    "m5.xlarge": 0.192,   "m5.2xlarge": 0.384,
    "m5.4xlarge": 0.768,  "m5.8xlarge": 1.536,
    "m6i.large": 0.096,   "m6i.xlarge": 0.192,  "m6i.2xlarge": 0.384,
    "m6i.4xlarge": 0.768, "m6i.8xlarge": 1.536,
    "c5.large": 0.085,    "c5.xlarge": 0.17,    "c5.2xlarge": 0.34,
    "c5.4xlarge": 0.68,   "c5.9xlarge": 1.53,
    "r5.large": 0.126,    "r5.xlarge": 0.252,   "r5.2xlarge": 0.504,
    "r5.4xlarge": 1.008,  "r5.8xlarge": 2.016,
}


def _get_env_tag(tags: list[dict]) -> str | None:
    """Return the non-prod environment value from a list of EC2 tag dicts, or None."""
    for key in _ENV_TAG_KEYS:
        for tag in tags:
            if tag.get("Key") == key:
                value = tag.get("Value", "")
                if value.lower() in _NONPROD_VALUES:
                    return value
    return None


def _monthly_cost_estimate(instance_type: str) -> float:
    hourly = _HOURLY_PRICE.get(instance_type, 0.0)
    return round(hourly * _HOURS_PER_MONTH, 2)


def _get_hourly_cpu_max(cw_client: Any, instance_id: str) -> list[float]:
    """
    Fetch per-hour Maximum CPUUtilization for the last _LOOKBACK_DAYS days.
    Single-instance fallback used only when batching is not available.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=_LOOKBACK_DAYS)
    try:
        resp = cw_client.get_metric_statistics(
            Namespace="AWS/EC2",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=start,
            EndTime=end,
            Period=3600,
            Statistics=["Maximum"],
        )
        datapoints = resp.get("Datapoints", [])
        return [dp["Maximum"] for dp in sorted(datapoints, key=lambda d: d["Timestamp"])]
    except Exception as e:
        log.debug("CloudWatch CPU fetch failed for %s: %s", instance_id, e)
        return []


def _batch_get_hourly_cpu_max(
    cw_client: Any,
    instance_ids: list[str],
) -> dict[str, list[float]]:
    """
    Fetch per-hour Maximum CPUUtilization for multiple instances in one
    get_metric_data call. Returns {instance_id: [cpu_values]}.
    Chunks at 500 queries to stay within AWS API limits.
    """
    if not instance_ids:
        return {}

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=_LOOKBACK_DAYS)

    queries = [
        {
            "Id": f"m{i}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/EC2",
                    "MetricName": "CPUUtilization",
                    "Dimensions": [{"Name": "InstanceId", "Value": iid}],
                },
                "Period": 3600,
                "Stat": "Maximum",
            },
            "ReturnData": True,
        }
        for i, iid in enumerate(instance_ids)
    ]

    results: dict[str, list[float]] = {iid: [] for iid in instance_ids}

    try:
        chunk_size = 500
        for chunk_start in range(0, len(queries), chunk_size):
            chunk = queries[chunk_start : chunk_start + chunk_size]
            resp = cw_client.get_metric_data(
                MetricDataQueries=chunk,
                StartTime=start,
                EndTime=end,
            )
            for r in resp.get("MetricDataResults", []):
                idx = int(r["Id"][1:])
                iid = instance_ids[idx]
                # Values are paired with Timestamps; sort by timestamp
                pairs = sorted(zip(r.get("Timestamps", []), r.get("Values", [])))
                results[iid] = [v for _, v in pairs]
    except Exception as exc:
        log.warning("Batched CloudWatch get_metric_data failed, results may be empty: %s", exc)

    return results


def _idle_hours(cpu_samples: list[float]) -> int:
    """Count samples where CPU was below the idle threshold."""
    return sum(1 for cpu in cpu_samples if cpu < _IDLE_CPU_THRESHOLD)


def _scheduler_command(instance_id: str, region: str) -> str:
    """Return an EventBridge Scheduler CLI command that starts/stops the instance."""
    start_cmd = (
        f"aws scheduler create-schedule "
        f"--name start-{instance_id} "
        f"--schedule-expression 'cron(0 8 ? * MON-FRI *)' "
        f"--flexible-time-window Mode=OFF "
        f"--target '{{"
        f'"Arn":"arn:aws:scheduler:::aws-sdk:ec2:startInstances",'
        f'"RoleArn":"<your-scheduler-role-arn>",'
        f'"Input":"{{\\"InstanceIds\\":[\\"" + instance_id + "\\"]}}"'
        f"}}' "
        f"--region {region}"
    )
    return (
        f"aws ec2 stop-instances --instance-ids {instance_id} --region {region}  "
        f"# then set up EventBridge Scheduler: "
        f"start cron(0 8 ? * MON-FRI *), stop cron(0 18 ? * MON-FRI *)"
    )


async def identify_nonprod_resources(
    aws_client: Any,
    regions: list[str] | None = None,
    env_tags: list[str] | None = None,
) -> dict:
    """
    Identify EC2 instances tagged as non-production that run 24/7 but appear idle
    during nights and weekends. Returns scheduling recommendations and estimated savings.

    Args:
        aws_client: Configured AWSConnector (used only to check credentials; boto3 is
                    imported internally so this module can be tested with mocks).
        regions:    AWS regions to scan. Defaults to all opted-in regions.
        env_tags:   Additional non-prod tag values to include beyond the built-in set.

    Returns:
        Dict with schedulable_instances list, total_monthly_waste, and total_instances.
    """
    if boto3 is None:
        return {"error": "boto3 not installed", "schedulable_instances": [], "total_monthly_waste": 0.0, "total_instances": 0}

    # Merge caller-supplied env tag values with defaults
    extra_values: set[str] = set()
    if env_tags:
        extra_values = {v.lower() for v in env_tags}
    nonprod_values = _NONPROD_VALUES | extra_values

    if regions is None:
        try:
            ec2g = boto3.client("ec2", region_name="us-east-1")
            resp = ec2g.describe_regions(
                Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
            )
            regions = [r["RegionName"] for r in resp.get("Regions", [])]
        except Exception:
            regions = ["us-east-1", "us-west-2", "eu-west-1"]

    schedulable: list[dict] = []

    for region in regions:
        try:
            ec2 = boto3.client("ec2", region_name=region)
            cw = boto3.client("cloudwatch", region_name=region)

            # Collect all non-prod instances first, then batch CloudWatch
            region_instances: list[dict] = []
            pag = ec2.get_paginator("describe_instances")
            for page in pag.paginate(
                Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
            ):
                for reservation in page["Reservations"]:
                    for inst in reservation["Instances"]:
                        tags = inst.get("Tags", [])
                        env_value = _get_env_tag(tags)

                        # Override nonprod_values for matching when caller extended the set
                        if env_value is None and extra_values:
                            for tag in tags:
                                if tag.get("Key") in _ENV_TAG_KEYS:
                                    v = tag.get("Value", "").lower()
                                    if v in nonprod_values:
                                        env_value = tag.get("Value", "")
                                        break

                        if env_value is None:
                            continue

                        region_instances.append({
                            "instance_id": inst["InstanceId"],
                            "instance_type": inst.get("InstanceType", ""),
                            "name": next(
                                (t["Value"] for t in tags if t.get("Key") == "Name"), ""
                            ),
                            "environment": env_value,
                        })

            if not region_instances:
                continue

            # Single batched CloudWatch call for all instances in this region
            instance_ids = [r["instance_id"] for r in region_instances]
            cpu_by_instance = _batch_get_hourly_cpu_max(cw, instance_ids)

            for inst_info in region_instances:
                iid = inst_info["instance_id"]
                itype = inst_info["instance_type"]
                cpu_samples = cpu_by_instance.get(iid, [])
                total_samples = len(cpu_samples)
                if total_samples == 0:
                    # No CloudWatch data: assume worst-case 70% idle (nights + weekends)
                    idle_hrs_per_week = round(
                        (_TOTAL_HOURS_PER_WEEK - _BUSINESS_HOURS_PER_WEEK), 1
                    )
                else:
                    idle_count = _idle_hours(cpu_samples)
                    # Scale from 7-day sample to weekly estimate
                    idle_hrs_per_week = round(
                        (idle_count / total_samples) * _TOTAL_HOURS_PER_WEEK, 1
                    )

                # Only flag instances that are meaningfully idle
                if idle_hrs_per_week < 20:
                    continue

                monthly_cost = _monthly_cost_estimate(itype)
                idle_fraction = idle_hrs_per_week / _TOTAL_HOURS_PER_WEEK
                potential_savings = round(monthly_cost * idle_fraction, 2)

                schedulable.append({
                    "instance_id": iid,
                    "instance_type": itype,
                    "name": inst_info["name"],
                    "environment": inst_info["environment"],
                    "region": region,
                    "monthly_cost_estimate": monthly_cost,
                    "idle_hours_per_week": idle_hrs_per_week,
                    "potential_monthly_savings": potential_savings,
                    "schedule_recommendation": "Mon-Fri 08:00-18:00 UTC",
                    "aws_scheduler_command": _scheduler_command(iid, region),
                })

        except Exception as e:
            log.warning("Non-prod scan failed for region %s: %s", region, e)

    schedulable.sort(key=lambda x: x["potential_monthly_savings"], reverse=True)
    total_waste = round(sum(r["potential_monthly_savings"] for r in schedulable), 2)

    return {
        "schedulable_instances": schedulable,
        "total_monthly_waste": total_waste,
        "total_instances": len(schedulable),
    }
