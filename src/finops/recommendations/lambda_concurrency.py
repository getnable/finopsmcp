"""
Lambda provisioned concurrency waste scanner.

Provisioned concurrency keep-warm costs $0.0000041667/GB-second even when idle
(this is the PC allocation rate, not the $0.0000097222/GB-s duration rate that
applies while a PC-enabled function is actually executing).
This scanner flags functions where average utilization over the last 14 days
is below a configurable threshold and calculates wasted monthly spend.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

PROVISIONED_CONCURRENCY_PER_GB_SECOND: float = 0.0000041667
SECONDS_PER_MONTH: int = 30 * 24 * 3600

_LOOKBACK_DAYS = 14
_DEFAULT_UTILIZATION_THRESHOLD = 0.5

_DEFAULT_REGIONS = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-west-1", "eu-west-2", "eu-central-1",
    "ap-southeast-1", "ap-southeast-2", "ap-northeast-1",
]


def _make_boto_session(aws_client: Any):
    """Return a boto3 session from the AWSConnector, or a fresh default session."""
    import boto3

    if hasattr(aws_client, "_session") and aws_client._session is not None:
        return aws_client._session
    return boto3.Session()


def _get_cw_metric_avg(
    cw_client: Any,
    function_name: str,
    qualifier: str,
    start: datetime,
    end: datetime,
) -> float | None:
    """
    Fetch the average ProvisionedConcurrencyUtilization for a function/qualifier
    over the given window.  Returns a value in [0, 1] or None if no data.
    """
    try:
        resp = cw_client.get_metric_statistics(
            Namespace="AWS/Lambda",
            MetricName="ProvisionedConcurrencyUtilization",
            Dimensions=[
                {"Name": "FunctionName", "Value": function_name},
                {"Name": "Resource",     "Value": f"{function_name}:{qualifier}"},
            ],
            StartTime=start,
            EndTime=end,
            Period=86400,  # daily data points
            Statistics=["Average"],
        )
        datapoints = resp.get("Datapoints", [])
        if not datapoints:
            return None
        avg_pct = sum(d["Average"] for d in datapoints) / len(datapoints)
        # CloudWatch returns 0-100; normalise to 0-1
        return avg_pct / 100.0
    except Exception as exc:
        log.debug("CW metric fetch failed for %s:%s: %s", function_name, qualifier, exc)
        return None


def _get_function_memory_mb(lambda_client: Any, function_name: str) -> int:
    """Return the configured memory in MB for a Lambda function."""
    try:
        resp = lambda_client.get_function_configuration(FunctionName=function_name)
        return int(resp.get("MemorySize", 128))
    except Exception:
        return 128


def _classify_recommendation(
    avg_utilization: float,
    datapoints_count: int,
) -> str:
    """Choose a recommendation string based on utilization pattern."""
    if avg_utilization < 0.10:
        return "remove_provisioned_concurrency"
    if avg_utilization < 0.50:
        return "reduce_provisioned_concurrency"
    # High variance check: if we have enough datapoints but low avg, suggest scheduled scaling.
    # We reach here only when avg >= 0.50 and the caller's threshold caused a flag (rare),
    # so default to scheduled scaling as a softer nudge.
    return "consider_scheduled_scaling"


async def scan_lambda_concurrency_waste(
    aws_client: Any,
    regions: list[str] | None = None,
    utilization_threshold: float = _DEFAULT_UTILIZATION_THRESHOLD,
) -> list[dict]:
    """
    Scan Lambda functions with provisioned concurrency for over-provisioning waste.

    For each function/qualifier pair, fetches 14 days of CloudWatch utilization
    data and flags those below utilization_threshold.  Calculates monthly cost
    and wasted monthly cost.

    Args:
        aws_client:             AWSConnector instance (provides boto3 session).
        regions:                AWS regions to scan.  Defaults to common regions.
        utilization_threshold:  Flag if average utilization is below this value.
                                Default 0.5 (50%).

    Returns:
        List of dicts with waste findings, sorted by wasted_monthly_cost descending.
    """
    import boto3

    target_regions = regions or _DEFAULT_REGIONS
    session = _make_boto_session(aws_client)

    end_time = datetime.now(tz=timezone.utc)
    start_time = end_time - timedelta(days=_LOOKBACK_DAYS)

    findings: list[dict] = []

    for region in target_regions:
        try:
            lambda_client = session.client("lambda", region_name=region)
            cw_client = session.client("cloudwatch", region_name=region)
        except Exception as exc:
            log.debug("Could not create clients for region %s: %s", region, exc)
            continue

        # List all Lambda functions in this region
        function_names: list[str] = []
        paginator = lambda_client.get_paginator("list_functions")
        try:
            for page in paginator.paginate():
                for fn in page.get("Functions", []):
                    function_names.append(fn["FunctionName"])
        except Exception as exc:
            log.debug("list_functions failed in %s: %s", region, exc)
            continue

        for function_name in function_names:
            # List provisioned concurrency configs for this function
            try:
                pc_resp = lambda_client.list_provisioned_concurrency_configs(
                    FunctionName=function_name
                )
            except Exception as exc:
                log.debug(
                    "list_provisioned_concurrency_configs failed for %s: %s",
                    function_name, exc
                )
                continue

            configs = pc_resp.get("ProvisionedConcurrencyConfigs", [])
            if not configs:
                continue

            memory_mb = _get_function_memory_mb(lambda_client, function_name)
            memory_gb = memory_mb / 1024.0

            for config in configs:
                qualifier = config.get("FunctionArn", "").split(":")[-1]
                provisioned_count = int(config.get("AllocatedProvisionedConcurrentExecutions", 0))
                if provisioned_count == 0:
                    continue

                avg_utilization = _get_cw_metric_avg(
                    cw_client, function_name, qualifier, start_time, end_time
                )
                # If no data, treat as fully idle (worst case)
                if avg_utilization is None:
                    avg_utilization = 0.0

                if avg_utilization >= utilization_threshold:
                    continue

                monthly_cost = (
                    provisioned_count
                    * memory_gb
                    * SECONDS_PER_MONTH
                    * PROVISIONED_CONCURRENCY_PER_GB_SECOND
                )
                wasted_monthly_cost = (1.0 - avg_utilization) * monthly_cost

                recommendation = _classify_recommendation(
                    avg_utilization=avg_utilization,
                    datapoints_count=_LOOKBACK_DAYS,
                )

                findings.append({
                    "function_name": function_name,
                    "qualifier": qualifier,
                    "provisioned_count": provisioned_count,
                    "avg_utilization_pct": round(avg_utilization * 100, 1),
                    "memory_mb": memory_mb,
                    "monthly_cost": round(monthly_cost, 4),
                    "wasted_monthly_cost": round(wasted_monthly_cost, 4),
                    "region": region,
                    "recommendation": recommendation,
                })

    findings.sort(key=lambda f: f["wasted_monthly_cost"], reverse=True)
    return findings
