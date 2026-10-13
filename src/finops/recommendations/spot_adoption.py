"""
Spot Instance Adoption Recommender.

Identifies on-demand EC2 instances that are good candidates for spot
migration: batch jobs, dev/staging environments, and stateless services in
ASGs.

Uses CloudWatch CPU variance (14 days) as a staleness signal, instance
tags to detect environment, and ASG membership to assess interruption
tolerance. Savings and interruption bands are a dated snapshot of the public
Spot Instance Advisor data for us-east-1 Linux, used only for us-east-1
instances of the types it covers. Anywhere else the interruption band is
unknown and the saving is an assumed default, and each row says which.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from ..analyzers.cloudwatch import MetricQuery, fetch_metric_values
from ..aws_prices import EC2_HOURLY
from ..aws_prices import HOURS_PER_MONTH as _HOURS_PER_MONTH
from .envelope import INFERRED, Finding

try:
    import boto3
except ImportError:
    boto3 = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

_LOOKBACK_DAYS  = 14

# On-demand hourly prices (us-east-1). Used to estimate monthly costs.
# Shared with every other module that prices an EC2 instance; see aws_prices.
_HOURLY_PRICE: dict[str, float] = EC2_HOURLY

# AWS Spot Instance Advisor public data (the file behind its console,
# spot-bid-advisor.s3.amazonaws.com/spot-advisor-data.json, object
# Last-Modified 2026-03-20), us-east-1, Linux. Savings are its "s" figure, the
# saving over on-demand. The interruption band is its "r" range: the share of
# capacity Spot reclaimed over the trailing month, published as a band, not a
# point. These tables used to hold point values the source does not support:
# m5.large at 72% saving and 2% interruption, where it reads 59% and >20%.
# The bands differ by region more than by type (c5.large is <5% in eu-west-1
# and >20% in us-east-2), so they are read for us-east-1 instances only.
SPOT_ADVISOR_REGION = "us-east-1"
SPOT_ADVISOR_AS_OF = "2026-03-20"

SPOT_DISCOUNT: dict[str, float] = {
    "m5.large":   0.59,  "m5.xlarge":  0.68,  "m5.2xlarge": 0.64,
    "m6i.large":  0.61,  "m6i.xlarge": 0.59,
    "c5.large":   0.61,  "c5.xlarge":  0.58,
    "r5.large":   0.62,  "r5.xlarge":  0.70,
    # Not from Spot Advisor: an assumed figure for every type and region the
    # snapshot does not cover. Rows priced on it say savings_basis "assumed".
    "_default":   0.65,
}

SPOT_INTERRUPTION_BAND: dict[str, str] = {
    "m5.large":   ">20%",   "m5.xlarge":  ">20%",   "m5.2xlarge": ">20%",
    "m6i.large":  ">20%",   "m6i.xlarge": ">20%",
    "c5.large":   "10-15%", "c5.xlarge":  "10-15%",
    "r5.large":   ">20%",   "r5.xlarge":  ">20%",
}

# The lower edge of each Spot Advisor band, the figure the thresholds below
# compare, so a band classifies exactly as its range does.
_BAND_FLOOR: dict[str, float] = {
    "<5%": 0.0, "5-10%": 0.05, "10-15%": 0.10, "15-20%": 0.15, ">20%": 0.20,
}

# Interruption frequency (fraction) per type: the lower edge of its band. The
# default is not a measurement; it keeps an unknown rate at POSSIBLE at best.
SPOT_INTERRUPTION_FREQ: dict[str, float] = {
    **{t: _BAND_FLOOR[b] for t, b in SPOT_INTERRUPTION_BAND.items()},
    "_default":   0.10,
}

# Recommendation thresholds (interruption frequency).
_THRESHOLD_RECOMMENDED = 0.05   # <5%  -> RECOMMENDED
_THRESHOLD_possible    = 0.15   # <15% -> POSSIBLE, else NOT_RECOMMENDED

# High CPU variance threshold (std-dev in %). Indicates unpredictable load.
_HIGH_VARIANCE_STDDEV = 25.0


def _from_advisor(instance_type: str, region: str | None) -> bool:
    """Whether the Spot Advisor snapshot speaks for this instance. A region of
    None (a caller that does not know it) keeps the old type-only lookup."""
    return instance_type in SPOT_INTERRUPTION_BAND and region in (None, SPOT_ADVISOR_REGION)


def _get_interruption_freq(instance_type: str, region: str | None = None) -> float:
    if _from_advisor(instance_type, region):
        return SPOT_INTERRUPTION_FREQ[instance_type]
    return SPOT_INTERRUPTION_FREQ["_default"]


def _get_interruption_band(instance_type: str, region: str | None = None) -> str:
    if _from_advisor(instance_type, region):
        return SPOT_INTERRUPTION_BAND[instance_type]
    return "unknown"


def _get_spot_discount(instance_type: str, region: str | None = None) -> float:
    if _from_advisor(instance_type, region):
        return SPOT_DISCOUNT[instance_type]
    return SPOT_DISCOUNT["_default"]


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
) -> dict[str, float | None]:
    """
    Fetch hourly Average CPUUtilization for many instances, one
    GetMetricStatistics call per instance run concurrently, inside CloudWatch's
    free request tier (GetMetricData only where the host opted in, see
    analyzers.cloudwatch). Returns {instance_id: stddev}. The spread is of the
    whole series, never of part of one. A series that read empty counts as no
    variance. A series that could not be read comes back None: zero variance
    is the "stable load" half of a RECOMMENDED verdict, so a failed read must
    not supply it.
    """
    if not instance_ids:
        return {}

    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    # GetMetricStatistics answers at most 1,440 datapoints a call, 60 days of
    # hourly points. The scan passes _LOOKBACK_DAYS (14), which is 336, so one
    # call holds the whole series.
    series = fetch_metric_values(cw_client, [
        MetricQuery(iid, "AWS/EC2", "CPUUtilization", (("InstanceId", iid),), "Average", 3600)
        for iid in instance_ids
    ], start, end)

    out: dict[str, float | None] = {}
    for iid in instance_ids:
        vals = series.get(iid)
        if vals is None:
            out[iid] = None
            continue
        out[iid] = statistics.stdev(vals) if len(vals) >= 2 else 0.0
    return out


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


class SpotResults(list):
    """recommend_spot_adoption's list of per-instance results, plus the ids of
    the instances skipped because their CPU read failed. A list subclass so
    every caller that treats the result as a plain list keeps working."""

    def __init__(self, *args):
        super().__init__(*args)
        self.cpu_unread_instances: list[str] = []


def _analyze_region(
    ec2_client: Any,
    cw_client: Any,
    asg_members: set[str],
    region: str,
    unread: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Per-instance results for one region. Instances whose CPU series could
    not be read are left out and their ids appended to `unread`."""
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

    # One concurrent CloudWatch read for every instance
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

        cpu_var = cpu_variance_by_id.get(iid)
        if cpu_var is None:
            # The read failed: no evidence the load is stable, so no verdict.
            if unread is not None:
                unread.append(iid)
            continue

        in_asg       = iid in asg_members
        is_stateless = _is_stateless(inst)
        freq         = _get_interruption_freq(itype, region)
        band         = _get_interruption_band(itype, region)
        discount     = _get_spot_discount(itype, region)

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
            # The Spot Advisor band the figure above is the floor of, or
            # "unknown" when the snapshot does not cover this type and region.
            "interruption_band":     band,
            "recommendation":        recommendation,
            "monthly_ondemand_cost": ondemand_cost,
            "monthly_spot_estimate": spot_cost,
            "monthly_savings":       savings,
            "savings_pct":           savings_pct,
            "savings_basis":         "spot_advisor" if band != "unknown" else "assumed",
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

    all_results = SpotResults()
    unread: list[str] = []

    def _one_region(region: str) -> list[dict[str, Any]]:
        try:
            ec2 = boto3.client("ec2",          region_name=region)
            cw  = boto3.client("cloudwatch",   region_name=region)
            asg = boto3.client("autoscaling",  region_name=region)

            asg_members = _get_asg_members(asg, [region])
            return _analyze_region(ec2, cw, asg_members, region, unread)
        except Exception as exc:
            log.warning("Region %s failed: %s", region, exc)
            return []

    # Regions are independent; scan them concurrently (blocking boto3 I/O
    # releases the GIL) instead of paying the sum of every region's latency.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(regions)))) as pool:
        for region_results in pool.map(_one_region, regions):
            all_results.extend(region_results)

    all_results.sort(key=lambda r: r["monthly_savings"], reverse=True)
    all_results.cpu_unread_instances = sorted(unread)
    if unread:
        log.warning("Spot adoption: %d instance(s) not assessed, CPU read failed", len(unread))

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
                "observed behavior. The discount and interruption band come from a "
                f"{SPOT_ADVISOR_AS_OF} Spot Instance Advisor snapshot for us-east-1 where "
                "it covers the type, and are an assumed default elsewhere; real spot prices "
                "vary by AZ and capacity pool, so I cannot put a precise saving on it until "
                "spot is actually running."
            ),
            assumptions=[
                "The workload tolerates a 2-minute interruption notice and can be "
                "rescheduled by its ASG.",
                "Env tags correctly indicate a stateless or non-prod instance.",
                f"Spot discount of about {top['savings_pct']:.0f}% holds ("
                + ("Spot Advisor us-east-1 figure" if top.get("savings_basis") == "spot_advisor"
                   else "an assumed default, not a Spot Advisor figure")
                + "; actual price moves with capacity).",
                "On-demand cost is the us-east-1 Linux list rate; a Reserved Instance or "
                "Savings Plan already covering the instance makes the real saving smaller.",
            ] + ([
                f"{len(unread)} on-demand instance(s) were not assessed: their CloudWatch "
                "CPU read failed, so they are not in this list or its total."
            ] if unread else []),
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
                "instances_cpu_unread": len(unread),
            },
        )
        top["finding"] = finding.to_dict()

    return all_results
