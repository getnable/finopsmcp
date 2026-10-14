"""
CloudWatch custom metric cardinality audit.

Custom metrics are billed per metric-month on a tiered rate: $0.30 for the
first 10,000, $0.10 for the next 240,000, $0.05 for the next 750,000 and $0.02
beyond. Only 10 metrics a month are free. A single microservice emitting one
metric per pod per environment can create thousands of metrics. At 10,000
metrics: about $3,000/month.

This module lists all custom namespaces (AWS/* namespaces are excluded),
counts metrics per namespace, identifies the dimensions causing cardinality
explosions (e.g. pod_id, request_id, trace_id), and estimates monthly cost.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from .envelope import INFERRED, Finding

log = logging.getLogger(__name__)

# AWS Price List, AmazonCloudWatch offer, us-east-1, version 20261007115540.
# Global-CW:MetricMonitorUsage is free for 0-10 metrics, account-wide. This
# used to be 10,000, a free tier that does not exist: 5,000 custom metrics
# read $0 against $1,497/mo at list.
FREE_METRICS = 10
# CW:MetricMonitorUsage tiers as (cumulative ceiling, USD per metric-month).
# Every metric used to be priced at the first tier's $0.30, so 50,000 metrics
# read $12,000/mo against $6,997 at list.
METRIC_PRICE_TIERS: tuple[tuple[float, float], ...] = (
    (10_000, 0.30), (250_000, 0.10), (1_000_000, 0.05), (float("inf"), 0.02),
)
METRIC_COST_PER_MONTH = METRIC_PRICE_TIERS[0][1]  # first-tier rate

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


def _tiered_cost(metric_count: int) -> float:
    """List cost of one region's custom metrics on the tiered rate, before the
    free tier."""
    cost, floor = 0.0, 0.0
    for ceiling, rate in METRIC_PRICE_TIERS:
        if metric_count <= floor:
            break
        cost += (min(metric_count, ceiling) - floor) * rate
        floor = ceiling
    return cost


def _estimate_cost(custom_metrics: int | dict[str, int]) -> float:
    """Monthly list cost of custom metrics, from one count or a count per region.

    Each region's usage type is tiered on its own in the AWS Price List; the
    10 free metrics are account-wide and come off once. An upper bound: a
    metric is billed only for the hours it receives data, and the count is
    every metric with a datapoint in the past two weeks.
    """
    by_region = custom_metrics if isinstance(custom_metrics, dict) else {"": custom_metrics}
    gross = sum(_tiered_cost(n) for n in by_region.values())
    free = min(FREE_METRICS, sum(by_region.values())) * METRIC_PRICE_TIERS[0][1]
    return round(max(0.0, gross - free), 2)


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

    Excludes AWS/* namespaces. Flags namespaces with >100 metrics, identifies
    cardinality-exploding dimensions, and estimates monthly list cost on the
    tiered metric rate ($0.30 for the first 10,000, then $0.10, $0.05, $0.02),
    less the 10 free metrics.

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
    count_by_region: dict[str, int] = {}
    grand_total = 0

    for result in region_results:
        if isinstance(result, Exception):
            log.warning("Region scan failed: %s", result)
            continue
        region_name = result["region"]
        region_count = result["total_custom_metrics"]
        grand_total += region_count
        count_by_region[region_name] = region_count
        by_region[region_name] = {
            "total_custom_metrics": region_count,
            "findings_count": len(result["findings"]),
        }
        # A namespace's share of its region's tiered cost, at the region's
        # average rate, before the account's 10 free metrics.
        avg_rate = _tiered_cost(region_count) / region_count if region_count else 0.0
        for finding in result["findings"]:
            finding["estimated_monthly_cost"] = round(finding["metric_count"] * avg_rate, 2)
            all_findings.append(finding)

    all_findings.sort(key=lambda f: f["metric_count"], reverse=True)
    estimated_monthly_cost = _estimate_cost(count_by_region)

    # Classify the finding by the STRENGTH OF EVIDENCE behind it. The metric
    # counts come straight from list_metrics and the tier rates are AWS's
    # published list price, so the cost is grounded. But it is not a saving:
    # this used to ship the whole custom-metric bill as a measured, precise
    # est_monthly_savings, as if every custom metric could go. What a
    # cardinality cut saves depends on which dimensions are dropped, which is
    # the user's call. So this is an investigation sized by the bill at stake,
    # and it is raised only when a namespace is over the cardinality threshold.
    finding = None
    if estimated_monthly_cost > 0 and all_findings:
        top = all_findings[0]
        worst_dims = top.get("high_cardinality_dimensions", [])
        rates = (f"billed per metric-month at $0.30 for the first 10,000, $0.10 to "
                 f"250,000, $0.05 to 1,000,000 and $0.02 beyond (us-east-1 list), with "
                 f"{FREE_METRICS} free")
        if worst_dims:
            dims_str = ", ".join(f'"{d}"' for d in worst_dims)
            why = (
                f"You are emitting {grand_total:,} custom CloudWatch metrics, {rates}. "
                f"The biggest namespace '{top['namespace']}' carries dimension(s) "
                f"{dims_str} that look like unique-per-request or unique-per-instance "
                "values, which is what blows up the metric count."
            )
        else:
            why = (
                f"You are emitting {grand_total:,} custom CloudWatch metrics, {rates}. "
                "Custom metric cost scales with cardinality, so a high count usually "
                "means a dimension is carrying unique-per-request or "
                "unique-per-instance values."
            )
        remediation = [
            "Confirm which dimension is exploding the count by sampling the top "
            "namespace, then drop or aggregate that dimension before emitting.",
            "Move unique identifiers (request_id, pod_id, trace_id) out of metric "
            "dimensions and into Embedded Metric Format logs or metric math.",
            "Removing a high-cardinality dimension drops the metric count and the bill "
            "with it, but verify your dashboards and alarms do not depend on that "
            "dimension first.",
        ]
        finding = Finding(
            source="cloudwatch_cardinality",
            title="Custom CloudWatch metric cardinality is driving billable spend",
            why=why,
            evidence=INFERRED,
            confidence="medium",
            why_unsure=(
                "The cost is the custom-metric count times AWS's tiered list rate, and "
                "it is an upper bound: list_metrics returns every metric with a "
                "datapoint in the past two weeks, while a metric is billed only for "
                "the hours it receives data. How much of it a cardinality fix removes "
                "depends on which dimensions you drop, which I have not measured."),
            assumptions=[
                "us-east-1 list rates, tiered per region; negotiated rates differ.",
                "Every metric list_metrics returned published data all month.",
            ],
            confirm_steps=[
                ("Read the CloudWatch metrics line (MetricMonitorUsage) in Cost Explorer "
                 "for the billed figure."),
                "Count the metrics a dimension contributes before and after dropping it.",
            ],
            rough_monthly=estimated_monthly_cost,
            remediation=remediation,
            resource_id=top["namespace"],
            metadata={
                "total_custom_metrics": grand_total,
                "free_tier": FREE_METRICS,
                "price_tiers": [list(t) for t in METRIC_PRICE_TIERS[:-1]]
                + [["over", METRIC_PRICE_TIERS[-1][1]]],
                "estimated_monthly_cost": estimated_monthly_cost,
                "top_namespaces": [
                    {"namespace": f["namespace"], "metric_count": f["metric_count"],
                     "region": f["region"]}
                    for f in all_findings[:5]
                ],
            },
        )

    return {
        "total_custom_metrics": grand_total,
        "estimated_monthly_cost": estimated_monthly_cost,
        "high_cardinality_namespaces": all_findings,
        "by_region": by_region,
        "finding": finding.to_dict() if finding else None,
    }
