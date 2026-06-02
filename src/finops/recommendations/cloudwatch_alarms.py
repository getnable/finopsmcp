"""
CloudWatch orphaned alarm audit.

Standard alarms cost $0.10/alarm/month. Composite alarms cost $0.30/alarm/month.
Alarms on deleted resources (terminated EC2 instances, deleted SQS queues,
deprovisioned endpoints) stay in INSUFFICIENT_DATA state and keep billing.

This module:
  1. Lists all CloudWatch alarms.
  2. Flags alarms in INSUFFICIENT_DATA for >7 days as likely orphaned.
  3. For EC2 metric alarms, verifies the instance still exists.
  4. For SQS metric alarms, verifies the queue still exists.
  5. Returns a safe-to-delete assessment per alarm.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

STANDARD_ALARM_COST = 0.10   # USD per alarm per month
COMPOSITE_ALARM_COST = 0.50  # USD per composite alarm per month

_INSUFFICIENT_DATA_THRESHOLD_DAYS = 7
_EC2_NAMESPACE = "AWS/EC2"
_SQS_NAMESPACE = "AWS/SQS"


def _make_cw(session_or_none: Any, region: str) -> Any:
    import boto3

    if session_or_none is not None:
        return session_or_none.client("cloudwatch", region_name=region)
    return boto3.client("cloudwatch", region_name=region)


def _make_ec2(session_or_none: Any, region: str) -> Any:
    import boto3

    if session_or_none is not None:
        return session_or_none.client("ec2", region_name=region)
    return boto3.client("ec2", region_name=region)


def _make_sqs(session_or_none: Any, region: str) -> Any:
    import boto3

    if session_or_none is not None:
        return session_or_none.client("sqs", region_name=region)
    return boto3.client("sqs", region_name=region)


def _get_opted_in_regions(session_or_none: Any) -> list[str]:
    import boto3

    ec2 = (
        boto3.client("ec2", region_name="us-east-1")
        if session_or_none is None
        else session_or_none.client("ec2", region_name="us-east-1")
    )
    resp = ec2.describe_regions(
        Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
    )
    return [r["RegionName"] for r in resp.get("Regions", [])]


def _list_all_alarms(cw: Any) -> list[dict]:
    """Return all metric alarms (standard) from the region."""
    alarms: list[dict] = []
    paginator = cw.get_paginator("describe_alarms")
    try:
        for page in paginator.paginate(AlarmTypes=["MetricAlarm"]):
            alarms.extend(page.get("MetricAlarms", []))
    except Exception as exc:
        log.debug("describe_alarms failed: %s", exc)
    return alarms


def _list_composite_alarms(cw: Any) -> list[dict]:
    """Return all composite alarms from the region."""
    alarms: list[dict] = []
    paginator = cw.get_paginator("describe_alarms")
    try:
        for page in paginator.paginate(AlarmTypes=["CompositeAlarm"]):
            alarms.extend(page.get("CompositeAlarms", []))
    except Exception as exc:
        log.debug("describe_alarms (composite) failed: %s", exc)
    return alarms


def _days_in_state(alarm: dict, now: datetime) -> int | None:
    """Return how many days the alarm has been in its current state."""
    state_updated = alarm.get("StateUpdatedTimestamp")
    if state_updated is None:
        return None
    if state_updated.tzinfo is None:
        state_updated = state_updated.replace(tzinfo=timezone.utc)
    return (now - state_updated).days


def _get_dimension_value(alarm: dict, name: str) -> str | None:
    for dim in alarm.get("Dimensions", []):
        if dim.get("Name") == name:
            return dim.get("Value")
    return None


def _instance_exists(ec2: Any, instance_id: str) -> bool:
    try:
        resp = ec2.describe_instances(InstanceIds=[instance_id])
        reservations = resp.get("Reservations", [])
        for r in reservations:
            for inst in r.get("Instances", []):
                state = inst.get("State", {}).get("Name", "")
                if state not in ("terminated", ""):
                    return True
        return False
    except Exception:
        return False


def _queue_exists(sqs: Any, queue_name: str, region: str, account_id: str = "") -> bool:
    """Check if an SQS queue exists by trying to get its URL."""
    try:
        sqs.get_queue_url(QueueName=queue_name)
        return True
    except Exception:
        return False


def _audit_region(session_or_none: Any, region: str) -> dict:
    """Audit a single region for orphaned alarms."""
    now = datetime.now(tz=timezone.utc)
    cw = _make_cw(session_or_none, region)
    ec2 = _make_ec2(session_or_none, region)
    sqs = _make_sqs(session_or_none, region)

    metric_alarms = _list_all_alarms(cw)
    composite_alarms = _list_composite_alarms(cw)

    results: list[dict] = []

    for alarm in metric_alarms:
        alarm_name = alarm.get("AlarmName", "")
        namespace = alarm.get("Namespace", "")
        metric_name = alarm.get("MetricName", "")
        state = alarm.get("StateValue", "")
        dimensions = alarm.get("Dimensions", [])
        monthly_cost = STANDARD_ALARM_COST

        days_insufficient = None
        likely_orphaned = False
        resource_exists: bool | None = None

        if state == "INSUFFICIENT_DATA":
            days_insufficient = _days_in_state(alarm, now)
            if days_insufficient is not None and days_insufficient >= _INSUFFICIENT_DATA_THRESHOLD_DAYS:
                likely_orphaned = True

                # Verify resource existence for known namespaces
                if namespace == _EC2_NAMESPACE:
                    instance_id = _get_dimension_value(alarm, "InstanceId")
                    if instance_id:
                        resource_exists = _instance_exists(ec2, instance_id)
                        if not resource_exists:
                            likely_orphaned = True

                elif namespace == _SQS_NAMESPACE:
                    queue_name = _get_dimension_value(alarm, "QueueName")
                    if queue_name:
                        resource_exists = _queue_exists(sqs, queue_name, region)
                        if not resource_exists:
                            likely_orphaned = True

        results.append({
            "alarm_name": alarm_name,
            "namespace": namespace,
            "metric_name": metric_name,
            "dimensions": dimensions,
            "state": state,
            "days_insufficient_data": days_insufficient,
            "monthly_cost": monthly_cost,
            "likely_orphaned": likely_orphaned,
            "resource_exists": resource_exists,
            "alarm_type": "MetricAlarm",
            "region": region,
        })

    for alarm in composite_alarms:
        alarm_name = alarm.get("AlarmName", "")
        state = alarm.get("StateValue", "")
        days_insufficient = None
        likely_orphaned = False

        if state == "INSUFFICIENT_DATA":
            days_insufficient = _days_in_state(alarm, now)
            if days_insufficient is not None and days_insufficient >= _INSUFFICIENT_DATA_THRESHOLD_DAYS:
                likely_orphaned = True

        results.append({
            "alarm_name": alarm_name,
            "namespace": "",
            "metric_name": "",
            "dimensions": [],
            "state": state,
            "days_insufficient_data": days_insufficient,
            "monthly_cost": COMPOSITE_ALARM_COST,
            "likely_orphaned": likely_orphaned,
            "resource_exists": None,
            "alarm_type": "CompositeAlarm",
            "region": region,
        })

    orphaned = [r for r in results if r["likely_orphaned"]]
    return {
        "region": region,
        "total_alarms": len(results),
        "orphaned_alarms": orphaned,
        "all_alarms": results,
    }


async def audit_cloudwatch_orphaned_alarms(
    aws_client: Any,
    regions: list[str] | None = None,
) -> dict:
    """
    Audit CloudWatch alarms for orphaned (likely waste) alarms across regions.

    Flags alarms that have been in INSUFFICIENT_DATA state for >7 days as likely
    orphaned. For EC2 and SQS metric alarms, verifies the backing resource still
    exists. Returns a safe-to-delete assessment per alarm.

    Pricing: $0.10/alarm/month (standard), $0.30/alarm/month (composite).

    Args:
        aws_client: AWSConnector instance (provides boto3 session).
        regions:    AWS regions to scan. Defaults to all opted-in regions.

    Returns:
        {
          total_alarms: int,
          total_orphaned: int,
          total_monthly_waste: float,
          orphaned_alarms: list[{
            alarm_name, namespace, metric_name, dimensions, state,
            days_insufficient_data, monthly_cost, likely_orphaned,
            resource_exists, alarm_type, region
          }],
          by_region: {region: {total_alarms, orphaned_count, monthly_waste}},
        }
    """
    import asyncio

    loop = asyncio.get_event_loop()
    session = getattr(aws_client, "_session", None)

    if not regions:
        try:
            regions = await loop.run_in_executor(None, _get_opted_in_regions, session)
        except Exception as exc:
            log.warning("Could not list regions, falling back to us-east-1: %s", exc)
            regions = ["us-east-1"]

    tasks = [
        loop.run_in_executor(None, _audit_region, session, region)
        for region in regions
    ]
    region_results = await asyncio.gather(*tasks, return_exceptions=True)

    all_orphaned: list[dict] = []
    by_region: dict[str, dict] = {}
    grand_total_alarms = 0

    for result in region_results:
        if isinstance(result, Exception):
            log.warning("Region alarm scan failed: %s", result)
            continue
        region_name = result["region"]
        orphaned = result["orphaned_alarms"]
        grand_total_alarms += result["total_alarms"]
        region_waste = sum(a["monthly_cost"] for a in orphaned)
        by_region[region_name] = {
            "total_alarms": result["total_alarms"],
            "orphaned_count": len(orphaned),
            "monthly_waste": round(region_waste, 2),
        }
        all_orphaned.extend(orphaned)

    total_monthly_waste = round(sum(a["monthly_cost"] for a in all_orphaned), 2)

    return {
        "total_alarms": grand_total_alarms,
        "total_orphaned": len(all_orphaned),
        "total_monthly_waste": total_monthly_waste,
        "orphaned_alarms": all_orphaned,
        "by_region": by_region,
    }
