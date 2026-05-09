"""
EC2 rightsizing recommendations via CloudWatch.

Identifies instances running at low CPU/network utilization over the past 14
days and calculates projected monthly savings from downsizing one tier.

Approach:
  - Pull all running EC2 instances per region
  - For each: fetch CloudWatch avg/max CPU over 14 days
  - Flag instances where avg CPU < threshold AND max CPU < spike_threshold
  - Look up on-demand pricing for current + next-smaller instance type
  - Return recommendations sorted by monthly savings (descending)

This is pure read-only — we never modify resources.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)

# Tunable thresholds
_AVG_CPU_THRESHOLD = 20.0   # flag if avg CPU < 20%
_MAX_CPU_THRESHOLD = 50.0   # AND max CPU never exceeded 50% (true idle, not just off-peak)
_LOOKBACK_DAYS = 14

# Simplified instance family downsize map (current → next smaller)
# In production this would be loaded from a pricing API / embedded JSON
_DOWNSIZE_MAP: dict[str, str] = {
    "t3.medium": "t3.small",   "t3.large": "t3.medium",
    "t3.xlarge": "t3.large",   "t3.2xlarge": "t3.xlarge",
    "t3a.medium": "t3a.small", "t3a.large": "t3a.medium",
    "m5.large": "m5.large",    "m5.xlarge": "m5.large",
    "m5.2xlarge": "m5.xlarge", "m5.4xlarge": "m5.2xlarge",
    "m6i.large": "m6i.large",  "m6i.xlarge": "m6i.large",
    "m6i.2xlarge": "m6i.xlarge","m6i.4xlarge": "m6i.2xlarge",
    "c5.large": "c5.large",    "c5.xlarge": "c5.large",
    "c5.2xlarge": "c5.xlarge", "c5.4xlarge": "c5.2xlarge",
    "r5.large": "r5.large",    "r5.xlarge": "r5.large",
    "r5.2xlarge": "r5.xlarge", "r5.4xlarge": "r5.2xlarge",
}

# Approximate on-demand hourly prices (us-east-1) — snapshot used for estimation.
# Real implementation would call the AWS Pricing API.
_HOURLY_PRICE: dict[str, float] = {
    "t3.nano": 0.0052,   "t3.micro": 0.0104,  "t3.small": 0.0208,
    "t3.medium": 0.0416, "t3.large": 0.0832,  "t3.xlarge": 0.1664, "t3.2xlarge": 0.3328,
    "t3a.nano": 0.0047,  "t3a.micro": 0.0094, "t3a.small": 0.0188,
    "t3a.medium": 0.0376,"t3a.large": 0.0752, "t3a.xlarge": 0.1504,"t3a.2xlarge": 0.3008,
    "m5.large": 0.096,   "m5.xlarge": 0.192,  "m5.2xlarge": 0.384,
    "m5.4xlarge": 0.768, "m5.8xlarge": 1.536,
    "m6i.large": 0.096,  "m6i.xlarge": 0.192, "m6i.2xlarge": 0.384,
    "m6i.4xlarge": 0.768,"m6i.8xlarge": 1.536,
    "c5.large": 0.085,   "c5.xlarge": 0.17,   "c5.2xlarge": 0.34,
    "c5.4xlarge": 0.68,  "c5.9xlarge": 1.53,
    "r5.large": 0.126,   "r5.xlarge": 0.252,  "r5.2xlarge": 0.504,
    "r5.4xlarge": 1.008, "r5.8xlarge": 2.016,
}

_HOURS_PER_MONTH = 730.0


@dataclass
class RightsizingRecommendation:
    instance_id: str
    instance_type: str
    name: str
    region: str
    account_id: str
    avg_cpu_pct: float
    max_cpu_pct: float
    recommended_type: str
    current_monthly_cost: float
    recommended_monthly_cost: float
    monthly_savings: float
    confidence: str  # "high" | "medium"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return (
            f"Downsize {self.name or self.instance_id} "
            f"({self.instance_type} → {self.recommended_type})"
        )

    @property
    def description(self) -> str:
        return (
            f"Avg CPU {self.avg_cpu_pct:.1f}%, max {self.max_cpu_pct:.1f}% "
            f"over {_LOOKBACK_DAYS} days. "
            f"Downsizing saves ~${self.monthly_savings:,.0f}/mo."
        )


def _get_cloudwatch_cpu(cw_client: Any, instance_id: str, days: int) -> tuple[float, float]:
    """Return (avg_cpu, max_cpu) for an instance over the past `days` days."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    resp = cw_client.get_metric_statistics(
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=start,
        EndTime=end,
        Period=3600,  # hourly points
        Statistics=["Average", "Maximum"],
    )
    datapoints = resp.get("Datapoints", [])
    if not datapoints:
        return 0.0, 0.0

    avgs = [d["Average"] for d in datapoints]
    maxs = [d["Maximum"] for d in datapoints]
    return sum(avgs) / len(avgs), max(maxs)


def _monthly_cost(instance_type: str) -> float:
    return _HOURLY_PRICE.get(instance_type, 0.0) * _HOURS_PER_MONTH


def analyze_rightsizing(
    regions: list[str] | None = None,
    avg_cpu_threshold: float = _AVG_CPU_THRESHOLD,
    max_cpu_threshold: float = _MAX_CPU_THRESHOLD,
) -> list[RightsizingRecommendation]:
    """
    Scan all running EC2 instances across given regions and return rightsizing
    recommendations sorted by monthly savings descending.
    """
    try:
        import boto3
    except ImportError:
        log.error("boto3 not installed")
        return []

    if regions is None:
        ec2_global = boto3.client("ec2", region_name="us-east-1")
        try:
            resp = ec2_global.describe_regions(Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}])
            regions = [r["RegionName"] for r in resp.get("Regions", [])]
        except Exception:
            regions = ["us-east-1", "us-west-2", "eu-west-1"]

    recommendations: list[RightsizingRecommendation] = []
    sts = boto3.client("sts")
    try:
        account_id = sts.get_caller_identity()["Account"]
    except Exception:
        account_id = "unknown"

    for region in regions:
        try:
            ec2 = boto3.client("ec2", region_name=region)
            cw = boto3.client("cloudwatch", region_name=region)

            paginator = ec2.get_paginator("describe_instances")
            for page in paginator.paginate(
                Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
            ):
                for reservation in page["Reservations"]:
                    for inst in reservation["Instances"]:
                        iid = inst["InstanceId"]
                        itype = inst["InstanceType"]
                        name = next(
                            (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), ""
                        )

                        avg_cpu, max_cpu = _get_cloudwatch_cpu(cw, iid, _LOOKBACK_DAYS)

                        if avg_cpu >= avg_cpu_threshold or max_cpu >= max_cpu_threshold:
                            continue  # instance is actively used

                        recommended = _DOWNSIZE_MAP.get(itype)
                        if not recommended or recommended == itype:
                            continue  # no smaller option mapped

                        current_cost = _monthly_cost(itype)
                        rec_cost = _monthly_cost(recommended)
                        savings = current_cost - rec_cost

                        if savings <= 0:
                            continue

                        confidence = "high" if avg_cpu < 10 and max_cpu < 30 else "medium"

                        recommendations.append(
                            RightsizingRecommendation(
                                instance_id=iid,
                                instance_type=itype,
                                name=name,
                                region=region,
                                account_id=account_id,
                                avg_cpu_pct=round(avg_cpu, 1),
                                max_cpu_pct=round(max_cpu, 1),
                                recommended_type=recommended,
                                current_monthly_cost=round(current_cost, 2),
                                recommended_monthly_cost=round(rec_cost, 2),
                                monthly_savings=round(savings, 2),
                                confidence=confidence,
                            )
                        )
        except Exception as e:
            log.warning("Rightsizing scan failed for region %s: %s", region, e)

    recommendations.sort(key=lambda r: r.monthly_savings, reverse=True)
    return recommendations


def rightsizing_summary(recommendations: list[RightsizingRecommendation]) -> dict[str, Any]:
    total_savings = sum(r.monthly_savings for r in recommendations)
    return {
        "total_instances_flagged": len(recommendations),
        "total_monthly_savings": round(total_savings, 2),
        "total_annual_savings": round(total_savings * 12, 2),
        "recommendations": [
            {
                "instance_id": r.instance_id,
                "name": r.name,
                "region": r.region,
                "current_type": r.instance_type,
                "recommended_type": r.recommended_type,
                "avg_cpu_pct": r.avg_cpu_pct,
                "max_cpu_pct": r.max_cpu_pct,
                "monthly_savings": r.monthly_savings,
                "confidence": r.confidence,
                "title": r.title,
                "description": r.description,
            }
            for r in recommendations
        ],
    }
