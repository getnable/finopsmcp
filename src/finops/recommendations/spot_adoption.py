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

from .envelope import INFERRED, Finding

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
    Single-instance fallback; use _batch_get_cpu_variance when scanning many instances.
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


def _batch_get_cpu_variance(
    cw_client: Any,
    instance_ids: list[str],
    days: int,
) -> dict[str, float]:
    """
    Fetch hourly Average CPUUtilization for multiple instances in a single
    get_metric_data call. Returns {instance_id: stddev}. Chunks at 500 queries.
    """
    if not instance_ids:
        return {}

    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

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
                "Stat": "Average",
            },
            "ReturnData": True,
        }
        for i, iid in enumerate(instance_ids)
    ]

    raw: dict[str, list[float]] = {iid: [] for iid in instance_ids}

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
                raw[iid] = r.get("Values", [])
    except Exception as exc:
        log.warning("Batched CPU variance fetch failed: %s", exc)

    return {
        iid: (statistics.stdev(vals) if len(vals) >= 2 else 0.0)
        for iid, vals in raw.items()
    }


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
    # Collect all on-demand instances first
    on_demand_instances: list[dict[str, Any]] = []
    try:
        paginator = ec2_client.get_paginator("describe_instances")
        pages = paginator.paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
        )
        for page in pages:
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    if inst.get("InstanceLifecycle") == "spot":
                        continue
                    on_demand_instances.append(inst)
    except Exception as exc:
        log.warning("Spot adoption scan failed for region %s: %s", region, exc)
        return []

    if not on_demand_instances:
        return []

    # Single batched CloudWatch call for all instances
    instance_ids = [inst["InstanceId"] for inst in on_demand_instances]
    cpu_variance_by_id = _batch_get_cpu_variance(cw_client, instance_ids, _LOOKBACK_DAYS)

    results: list[dict[str, Any]] = []
    for inst in on_demand_instances:
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

        in_asg       = iid in asg_members
        is_stateless = _is_stateless(inst)
        cpu_var      = cpu_variance_by_id.get(iid, 0.0)
        freq         = _get_interruption_freq(itype)
        discount     = _get_spot_discount(itype)

        ondemand_cost = _monthly_ondemand_cost(itype)
        spot_cost     = round(ondemand_cost * (1.0 - discount), 2)
        savings       = round(ondemand_cost - spot_cost, 2)
        savings_pct   = round(discount * 100, 1)

        recommendation = _classify(freq, in_asg, is_stateless, cpu_var)

        results.append({
            "instance_id":           iid,
            "instance_type":         itype,
            "name":                  name,
            "region":                region,
            "environment":           env,
            "in_asg":                in_asg,
            "interruption_freq_pct": round(freq * 100, 1),
            "recommendation":        recommendation,
            "monthly_ondemand_cost": ondemand_cost,
            "monthly_spot_estimate": spot_cost,
            "monthly_savings":       savings,
            "savings_pct":           savings_pct,
            "cpu_variance_stddev":   round(cpu_var, 1),
        })
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

    # Classify the finding by the STRENGTH OF EVIDENCE behind it. We can MEASURE the
    # on-demand instance and its type, but the saving rests on the customer adopting
    # spot, which carries interruption risk. "Stateless" is inferred from env tags,
    # the discount and interruption frequency are hardcoded public averages, and the
    # real spot price moves by AZ and capacity pool. None of that is confirmed for
    # this workload, so this is an INFERRED investigation with a magnitude band, never
    # a precise dollar claim. Attached to the top result so the list shape is intact.
    actionable = [r for r in all_results
                  if r["recommendation"] in ("RECOMMENDED", "POSSIBLE")]
    if actionable:
        top = actionable[0]
        rough = sum(r["monthly_savings"] for r in actionable)
        n = len(actionable)
        finding = Finding(
            source="spot_adoption",
            title="Let's check which on-demand instances can safely move to spot",
            why=(
                f"Instance {top['instance_id']} ({top['instance_type']}) runs on demand "
                "and looks like a spot candidate: it sits in an Auto Scaling Group and "
                "carries non-prod or stateless signals. Spot trades a steep discount for "
                "the chance of interruption, so for tolerant workloads it is often a large "
                f"saving. I see {n} candidate instance(s) like this."
            ),
            evidence=INFERRED,
            confidence="medium" if top["recommendation"] == "RECOMMENDED" else "low",
            why_unsure=(
                "Whether spot is safe here depends on how the workload tolerates "
                "interruption, which I infer from tags and ASG membership, not from "
                "observed behavior. The discount and interruption frequency are public "
                "AWS averages, and real spot prices vary by AZ and capacity pool, so I "
                "cannot put a precise saving on it until spot is actually running."
            ),
            assumptions=[
                "The workload tolerates a 2-minute interruption notice and can be "
                "rescheduled by its ASG.",
                "Env tags correctly indicate a stateless or non-prod instance.",
                f"Spot discount of about {top['savings_pct']:.0f}% holds (public average; "
                "actual price moves with capacity).",
            ],
            rough_monthly=round(rough, 2),
            confirm_steps=[
                "Confirm the workload is interruption-tolerant: stateless, checkpointed, "
                "or behind a queue, and not holding a session or local state.",
                "Run a small spot fraction first via a MixedInstancesPolicy "
                "(OnDemandBaseCapacity for a safety floor) and watch interruption rates.",
                "Check the live spot price and interruption frequency for these types in "
                "your AZs in the Spot Advisor before sizing the saving.",
            ],
            pro_can_confirm=True,
            pro_unlock=(
                "On Pro, give nable read-only CloudTrail and CUR access and it confirms "
                "the actual spot price you would pay, measures real interruption rates for "
                "these instance types in your AZs, and reports the saving you can stand on "
                "instead of a public-average estimate."
            ),
            remediation=[
                "Confirm interruption tolerance first, then migrate via a Launch Template "
                "mixed-instances policy with capacity-optimized allocation and 5+ instance "
                "types. Keep an on-demand base capacity as a floor. Test in staging before "
                "moving any production fleet: a capacity-pool tightening can reclaim spot "
                "with two minutes notice.",
            ],
            resource_id=top["instance_id"],
            metadata={
                "region": top["region"],
                "instance_type": top["instance_type"],
                "in_asg": top["in_asg"],
                "interruption_freq_pct": top["interruption_freq_pct"],
                "monthly_ondemand_cost": top["monthly_ondemand_cost"],
                "monthly_spot_estimate": top["monthly_spot_estimate"],
                "candidate_count": n,
                "candidates_sampled": [r["instance_id"] for r in actionable[:8]],
            },
        )
        top["finding"] = finding.to_dict()

    return all_results
