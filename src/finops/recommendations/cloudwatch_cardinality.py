"""
CloudWatch custom metric cardinality audit.

Custom metrics cost $0.30/metric/month above the 10,000 free-tier threshold.
A single microservice emitting one metric per pod per environment can create
thousands of metrics. At 10,000 metrics: $3,000/month.

This module lists all custom namespaces (AWS/* namespaces are free and excluded),
counts metrics per namespace, identifies the dimensions causing cardinality
explosions (e.g. pod_id, request_id, trace_id), and estimates monthly cost.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any

log = logging.getLogger(__name__)

FREE_METRICS = 10_000
METRIC_COST_PER_MONTH = 0.30  # per metric above free tier

# Dimensions commonly causing cardinality explosions
_HIGH_CARDINALITY_DIMENSION_HINTS = {
    "pod_id", "pod", "pod_name",
    "request_id", "requestid",
    "trace_id", "traceid",
    "container_id", "containerid",
    "task_id", "taskid",
    "execution_id",
    "session_id", "sessionid",
    "user_id", "userid",
    "transaction_id",
    "span_id",
    "instance_id",
}

_HIGH_CARDINALITY_THRESHOLD = 100  # namespaces with more metrics than this are flagged
_SAMPLE_SIZE = 20  # number of metrics to sample when identifying bad dimensions


def _make_cw(session_or_none: Any, region: str) -> Any:
    """Return a CloudWatch client for the given region."""
    import boto3

    if session_or_none is not None:
        return session_or_none.client("cloudwatch", region_name=region)
    return boto3.client("cloudwatch", region_name=region)


def _get_opted_in_regions(session_or_none: Any) -> list[str]:
    """Return all regions the account has opted in to."""
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


def _list_custom_namespaces(cw: Any) -> list[str]:
    """
    Return all non-AWS namespaces visible in this region.
    AWS/* namespaces are included in the free tier and not billed per metric.
    """
    namespaces: list[str] = []
    paginator = cw.get_paginator("list_metrics")
    seen: set[str] = set()
    try:
        for page in paginator.paginate():
            for metric in page.get("Metrics", []):
                ns = metric.get("Namespace", "")
                if ns and not ns.startswith("AWS/") and ns not in seen:
                    seen.add(ns)
                    namespaces.append(ns)
    except Exception as exc:
        log.debug("list_metrics paginator failed: %s", exc)
    return namespaces


def _count_and_sample_metrics(cw: Any, namespace: str) -> tuple[int, list[dict]]:
    """
    Count all metrics in namespace and return a sample for dimension analysis.
    Returns (count, sample_metrics).
    """
    all_metrics: list[dict] = []
    paginator = cw.get_paginator("list_metrics")
    try:
        for page in paginator.paginate(Namespace=namespace):
            all_metrics.extend(page.get("Metrics", []))
    except Exception as exc:
        log.debug("list_metrics failed for namespace %s: %s", namespace, exc)
    sample = all_metrics[:_SAMPLE_SIZE]
    return len(all_metrics), sample


def _identify_high_cardinality_dimensions(sample_metrics: list[dict]) -> list[str]:
    """
    Inspect sampled metrics to find dimension names that suggest cardinality explosion.
    Returns dimension names that match known high-cardinality patterns.
    """
    dimension_names: Counter = Counter()
    for metric in sample_metrics:
        for dim in metric.get("Dimensions", []):
            dimension_names[dim["Name"]] += 1

    flagged: list[str] = []
    for dim_name, count in dimension_names.most_common():
        lower = dim_name.lower().replace("-", "_")
        if lower in _HIGH_CARDINALITY_DIMENSION_HINTS:
            flagged.append(dim_name)

    return flagged


def _build_recommendation(namespace: str, count: int, bad_dims: list[str]) -> str:
    if bad_dims:
        dims_str = ", ".join(f'"{d}"' for d in bad_dims)
        return (
            f"Namespace '{namespace}' has {count} metrics. "
            f"The dimension(s) {dims_str} appear to contain unique-per-request or "
            f"unique-per-instance values. Remove those dimensions or aggregate at the "
            f"service level before emitting. Consider using metric math or embedded "
            f"metric format (EMF) with structured logs instead."
        )
    return (
        f"Namespace '{namespace}' has {count} metrics above the HIGH_CARDINALITY "
        f"threshold of {_HIGH_CARDINALITY_THRESHOLD}. Sample the metric dimensions "
        f"to identify which one is causing the explosion and aggregate or remove it."
    )


def _estimate_cost(total_custom_metrics: int) -> float:
    """Estimate monthly cost for metrics above the free tier."""
    billable = max(0, total_custom_metrics - FREE_METRICS)
    return round(billable * METRIC_COST_PER_MONTH, 2)


def _audit_region(session_or_none: Any, region: str) -> dict:
    """Audit one region. Returns raw findings dict."""
    cw = _make_cw(session_or_none, region)

    namespaces = _list_custom_namespaces(cw)
    findings: list[dict] = []
    total_custom_metrics = 0

    for namespace in namespaces:
        count, sample = _count_and_sample_metrics(cw, namespace)
        total_custom_metrics += count

        if count <= _HIGH_CARDINALITY_THRESHOLD:
            continue

        bad_dims = _identify_high_cardinality_dimensions(sample)
        findings.append({
            "namespace": namespace,
            "metric_count": count,
            "high_cardinality_dimensions": bad_dims,
            "region": region,
            "recommendation": _build_recommendation(namespace, count, bad_dims),
        })

    findings.sort(key=lambda f: f["metric_count"], reverse=True)
    return {
        "region": region,
        "total_custom_metrics": total_custom_metrics,
        "findings": findings,
    }


async def audit_cloudwatch_metric_cardinality(
    aws_client: Any,
    regions: list[str] | None = None,
) -> dict:
    """
    Audit CloudWatch custom metric cardinality across regions.

    Excludes AWS/* namespaces (free tier). Flags namespaces with >100 metrics,
    identifies cardinality-exploding dimensions, and estimates monthly cost above
    the 10,000-metric free tier at $0.30/metric/month.

    Args:
        aws_client: AWSConnector instance (provides boto3 session).
        regions:    AWS regions to scan. Defaults to all opted-in regions.

    Returns:
        {
          total_custom_metrics: int,
          estimated_monthly_cost: float,
          high_cardinality_namespaces: list[{
            namespace, metric_count, estimated_monthly_cost,
            high_cardinality_dimensions, recommendation, region
          }],
          by_region: {region: {total_custom_metrics, findings_count}},
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

    all_findings: list[dict] = []
    by_region: dict[str, dict] = {}
    grand_total = 0

    for result in region_results:
        if isinstance(result, Exception):
            log.warning("Region scan failed: %s", result)
            continue
        region_name = result["region"]
        grand_total += result["total_custom_metrics"]
        by_region[region_name] = {
            "total_custom_metrics": result["total_custom_metrics"],
            "findings_count": len(result["findings"]),
        }
        for finding in result["findings"]:
            # Attach per-namespace cost estimate relative to the full account total
            # (cost is account-wide, so we attach it per finding as a proportional note)
            ns_cost = round(finding["metric_count"] * METRIC_COST_PER_MONTH, 2)
            finding["estimated_monthly_cost"] = ns_cost
            all_findings.append(finding)

    all_findings.sort(key=lambda f: f["metric_count"], reverse=True)
    estimated_monthly_cost = _estimate_cost(grand_total)

    return {
        "total_custom_metrics": grand_total,
        "estimated_monthly_cost": estimated_monthly_cost,
        "high_cardinality_namespaces": all_findings,
        "by_region": by_region,
    }
