"""
Spot Instance Adoption Recommender.

Identifies on-demand EC2 instances that are good candidates for spot
migration. Spot instances save 60-80% for eligible workloads like
batch jobs, dev/staging environments, and stateless services in ASGs.

Uses CloudWatch CPU variance (14 days) as a staleness signal, instance
tags to detect environment, and ASG membership to assess interruption
tolerance. Spot Advisor interruption frequencies are hardcoded from
public AWS data.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    import boto3
except ImportError:
    boto3 = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

_LOOKBACK_DAYS  = 14
_HOURS_PER_MONTH = 730.0

# On-demand hourly prices (us-east-1). Used to estimate monthly costs.
_HOURLY_PRICE: dict[str, float] = {
    "m5.large":    0.096,  "m5.xlarge":   0.192,  "m5.2xlarge":  0.384,
    "m5.4xlarge":  0.768,
    "m6i.large":   0.096,  "m6i.xlarge":  0.192,  "m6i.2xlarge": 0.384,
    "c5.large":    0.085,  "c5.xlarge":   0.170,  "c5.2xlarge":  0.340,
    "r5.large":    0.126,  "r5.xlarge":   0.252,  "r5.2xlarge":  0.504,
    "t3.medium":   0.0416, "t3.large":    0.0832, "t3.xlarge":   0.1664,
    "t3.2xlarge":  0.3328,
}

# Spot discount relative to on-demand (fraction saved). Source: AWS Spot pricing.
SPOT_DISCOUNT: dict[str, float] = {
    "m5.large":   0.72,  "m5.xlarge":  0.71,  "m5.2xlarge": 0.70,
    "m6i.large":  0.68,  "m6i.xlarge": 0.69,
    "c5.large":   0.75,  "c5.xlarge":  0.74,
    "r5.large":   0.65,  "r5.xlarge":  0.64,
    "_default":   0.65,
}

# Interruption frequency per hour (fraction). Source: AWS Spot Advisor public data.
SPOT_INTERRUPTION_FREQ: dict[str, float] = {
    "m5.large":   0.02,  "m5.xlarge":  0.03,
    "m6i.large":  0.01,  "m6i.xlarge": 0.02,
    "c5.large":   0.05,  "c5.xlarge":  0.04,
    "_default":   0.10,
}

# Recommendation thresholds (interruption frequency).
_THRESHOLD_RECOMMENDED = 0.05   # <5%  -> RECOMMENDED
_THRESHOLD_possible    = 0.15   # <15% -> POSSIBLE, else NOT_RECOMMENDED

# High CPU variance threshold (std-dev in %). Indicates unpredictable load.
_HIGH_VARIANCE_STDDEV = 25.0


def _get_interruption_freq(instance_type: str) -> float:
    return SPOT_INTERRUPTION_FREQ.get(instance_type, SPOT_INTERRUPTION_FREQ["_default"])


def _get_spot_discount(instance_type: str) -> float:
    return SPOT_DISCOUNT.get(instance_type, SPOT_DISCOUNT["_default"])


def _monthly_ondemand_cost(instance_type: str) -> float:
    return round(_HOURLY_PRICE.get(instance_type, 0.0) * _HOURS_PER_MONTH, 2)


def _get_cpu_variance(cw_client: Any, instance_id: str, days: int) -> float:
    """
    Return std-dev of hourly CPU utilization over `days` days.
    Returns 0.0 if no data is available (treated as stable / unknown).
    """
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    try:
        resp = cw_client.get_metric_statistics(
            Namespace="AWS/EC2",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=start,
            EndTime=end,
            Period=3600,
            Statistics=["Average"],
        )
        dps = [d["Average"] for d in resp.get("Datapoints", [])]
        if len(dps) < 2:
            return 0.0
        return statistics.stdev(dps)
    except Exception as exc:
        log.debug("CloudWatch CPU variance unavailable for %s: %s", instance_id, exc)
        return 0.0


def _classify(
    interruption_freq: float,
    in_asg: bool,
    is_stateless: bool,
    cpu_variance: float,
) -> str:
    """
    Return RECOMMENDED, POSSIBLE, or NOT_RECOMMENDED.

    RECOMMENDED requires: low interruption freq, in ASG, stateless signals, stable CPU.
    POSSIBLE: low interruption freq and in ASG, but missing stateless signals or moderate variance.
    NOT_RECOMMENDED: high interruption freq, stateful, or high CPU variance.
    """
    high_variance = cpu_variance > _HIGH_VARIANCE_STDDEV

    # Stateful signals disqualify outright regardless of other factors
    if not is_stateless:
        return "NOT_RECOMMENDED"

    # High variance means unpredictable load -> risky to interrupt
    if high_variance:
        return "NOT_RECOMMENDED"

    # Not in an ASG means interruptions require manual restart -> not viable
    if not in_asg:
        return "NOT_RECOMMENDED"

    if interruption_freq < _THRESHOLD_RECOMMENDED:
        return "RECOMMENDED"
    if interruption_freq < _THRESHOLD_possible:
        return "POSSIBLE"
    return "NOT_RECOMMENDED"


def _is_stateless(instance: dict) -> bool:
    """
    Heuristic: instance is stateless if its env tag indicates dev/staging/test.
    """
    tags = {t["Key"].lower(): t["Value"].lower() for t in instance.get("Tags", [])}
    env = tags.get("env", tags.get("environment", tags.get("stage", "")))
    return env in {"dev", "development", "staging", "stage", "test", "testing", "qa"}


def _get_asg_members(autoscaling_client: Any, regions_hint: list[str]) -> set[str]:
    """Return set of instance IDs that belong to an Auto Scaling Group."""
    members: set[str] = set()
    try:
        paginator = autoscaling_client.get_paginator("describe_auto_scaling_groups")
        for page in paginator.paginate():
            for asg in page.get("AutoScalingGroups", []):
                for inst in asg.get("Instances", []):
                    members.add(inst["InstanceId"])
    except Exception as exc:
        log.warning("Could not list ASG members: %s", exc)
    return members


def _analyze_region(
    ec2_client: Any,
    cw_client: Any,
    asg_members: set[str],
    region: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    try:
        paginator = ec2_client.get_paginator("describe_instances")
        pages = paginator.paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
        )
        for page in pages:
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    # Skip spot instances — they are already on spot
                    if inst.get("InstanceLifecycle") == "spot":
                        continue

                    iid   = inst["InstanceId"]
                    itype = inst["InstanceType"]
                    tags  = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                    name  = tags.get("Name", "")
                    env   = (
                        tags.get("env")
                        or tags.get("environment")
                        or tags.get("stage")
                        or ""
                    )

                    in_asg      = iid in asg_members
                    is_stateless = _is_stateless(inst)
                    cpu_var     = _get_cpu_variance(cw_client, iid, _LOOKBACK_DAYS)
                    freq        = _get_interruption_freq(itype)
                    discount    = _get_spot_discount(itype)

                    ondemand_cost = _monthly_ondemand_cost(itype)
                    spot_cost     = round(ondemand_cost * (1.0 - discount), 2)
                    savings       = round(ondemand_cost - spot_cost, 2)
                    savings_pct   = round(discount * 100, 1)

                    recommendation = _classify(freq, in_asg, is_stateless, cpu_var)

                    results.append({
                        "instance_id":            iid,
                        "instance_type":          itype,
                        "name":                   name,
                        "region":                 region,
                        "environment":            env,
                        "in_asg":                 in_asg,
                        "interruption_freq_pct":  round(freq * 100, 1),
                        "recommendation":         recommendation,
                        "monthly_ondemand_cost":  ondemand_cost,
                        "monthly_spot_estimate":  spot_cost,
                        "monthly_savings":        savings,
                        "savings_pct":            savings_pct,
                        "cpu_variance_stddev":    round(cpu_var, 1),
                    })
    except Exception as exc:
        log.warning("Spot adoption scan failed for region %s: %s", region, exc)
    return results


# ── Public API ────────────────────────────────────────────────────────────────

def recommend_spot_adoption(
    regions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Scan running on-demand EC2 instances and return spot adoption recommendations.

    Sorted by monthly_savings descending. Instances already on spot are excluded.
    Regions defaults to us-east-1, us-west-2, eu-west-1 if AWS region discovery fails.
    """
    if boto3 is None:
        log.error("boto3 not installed")
        return []

    if regions is None:
        try:
            ec2g = boto3.client("ec2", region_name="us-east-1")
            resp = ec2g.describe_regions(
                Filters=[{"Name": "opt-in-status",
                          "Values": ["opt-in-not-required", "opted-in"]}]
            )
            regions = [r["RegionName"] for r in resp.get("Regions", [])]
        except Exception:
            regions = ["us-east-1", "us-west-2", "eu-west-1"]

    all_results: list[dict[str, Any]] = []

    for region in regions:
        try:
            ec2 = boto3.client("ec2",          region_name=region)
            cw  = boto3.client("cloudwatch",   region_name=region)
            asg = boto3.client("autoscaling",  region_name=region)

            asg_members = _get_asg_members(asg, [region])
            region_results = _analyze_region(ec2, cw, asg_members, region)
            all_results.extend(region_results)
        except Exception as exc:
            log.warning("Region %s failed: %s", region, exc)

    all_results.sort(key=lambda r: r["monthly_savings"], reverse=True)
    return all_results
