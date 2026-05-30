"""
Spot Diversification Auditor.

ASGs using spot instances should use 5+ instance types across multiple
availability zones to minimize correlated interruptions. Using only 1-2
types means a single capacity pool tightening can take down the entire fleet.

Best practices audited here:
- Instance type count: 5+ is OK, 3-4 is MEDIUM_RISK, <3 is HIGH_RISK
- Allocation strategy: capacity-optimized is preferred over lowest-price
- Spot percentage: tracked for context

Data source: AWS Auto Scaling Groups (describe_auto_scaling_groups).
"""
from __future__ import annotations

import logging
from typing import Any

try:
    import boto3
except ImportError:
    boto3 = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

_RISK_OK            = "OK"
_RISK_MEDIUM        = "MEDIUM_RISK"
_RISK_HIGH          = "HIGH_RISK"

_COUNT_OK_THRESHOLD     = 5   # 5+ types is OK
_COUNT_MEDIUM_THRESHOLD = 3   # 3-4 types is medium, <3 is high


def _get_mixed_instances_policy(asg: dict) -> dict | None:
    return asg.get("MixedInstancesPolicy")


def _extract_instance_types(policy: dict | None, launch_template: dict | None) -> list[str]:
    """
    Return the list of instance types configured in a MixedInstancesPolicy.
    Falls back to the base launch template instance type when no overrides exist.
    """
    if policy:
        overrides = (
            policy.get("LaunchTemplate", {}).get("Overrides", [])
        )
        types = [o["InstanceType"] for o in overrides if "InstanceType" in o]
        if types:
            return types

    if launch_template:
        itype = launch_template.get("LaunchTemplateSpecification", {}).get("InstanceType")
        if itype:
            return [itype]

    return []


def _extract_allocation_strategy(policy: dict | None) -> str:
    """Return the spot allocation strategy, or 'unknown' if not set."""
    if not policy:
        return "unknown"
    instances_distrib = policy.get("InstancesDistribution", {})
    return instances_distrib.get("SpotAllocationStrategy", "unknown")


def _extract_spot_pct(policy: dict | None, asg: dict) -> float:
    """
    Estimate what fraction of capacity is spot, 0.0-1.0.
    Uses OnDemandBaseCapacity and OnDemandPercentageAboveBaseCapacity to calculate.
    """
    if not policy:
        # Check for LaunchConfiguration with spot price (old-style)
        if asg.get("LaunchConfigurationName"):
            # We cannot easily determine spot pct without describing the launch config.
            return 0.0
        return 0.0

    distrib = policy.get("InstancesDistribution", {})
    od_base  = int(distrib.get("OnDemandBaseCapacity", 0))
    od_above = int(distrib.get("OnDemandPercentageAboveBaseCapacity", 100))
    desired  = int(asg.get("DesiredCapacity", 0))

    if desired <= 0:
        return 0.0

    od_count = od_base + max(0, desired - od_base) * od_above / 100.0
    spot_count = max(0.0, desired - od_count)
    return round(spot_count / desired, 3)


def _classify_risk(instance_type_count: int) -> str:
    if instance_type_count >= _COUNT_OK_THRESHOLD:
        return _RISK_OK
    if instance_type_count >= _COUNT_MEDIUM_THRESHOLD:
        return _RISK_MEDIUM
    return _RISK_HIGH


def _make_recommendation(risk: str, type_count: int, strategy: str) -> str:
    parts: list[str] = []
    if risk == _RISK_HIGH:
        parts.append(
            f"Only {type_count} instance type(s) configured. "
            "Add at least 5 instance types to reduce correlated interruption risk."
        )
    elif risk == _RISK_MEDIUM:
        parts.append(
            f"{type_count} instance types configured. "
            "Adding 1-2 more types would reduce interruption risk."
        )
    else:
        parts.append(f"{type_count} instance types configured. Diversification looks healthy.")

    if strategy not in {"capacity-optimized", "capacity-optimized-prioritized"}:
        parts.append(
            "Consider switching to capacity-optimized allocation strategy "
            "for better interruption resilience."
        )

    return " ".join(parts)


def _audit_asg(asg: dict, region: str) -> dict[str, Any] | None:
    """
    Return an audit record for `asg` if it uses spot instances, else None.
    """
    name   = asg.get("AutoScalingGroupName", "")
    policy = _get_mixed_instances_policy(asg)

    spot_pct = _extract_spot_pct(policy, asg)

    # Skip ASGs with no meaningful spot usage
    if spot_pct <= 0.0 and policy is None:
        return None
    if spot_pct <= 0.0:
        return None

    instance_types = _extract_instance_types(policy, asg.get("LaunchTemplate"))
    strategy       = _extract_allocation_strategy(policy)
    type_count     = len(instance_types)
    risk           = _classify_risk(type_count)
    recommendation = _make_recommendation(risk, type_count, strategy)

    return {
        "asg_name":             name,
        "region":               region,
        "instance_types_count": type_count,
        "instance_types":       instance_types,
        "allocation_strategy":  strategy,
        "spot_pct":             round(spot_pct * 100, 1),
        "recommendation":       recommendation,
        "risk_level":           risk,
    }


def _scan_region(asg_client: Any, region: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    try:
        paginator = asg_client.get_paginator("describe_auto_scaling_groups")
        for page in paginator.paginate():
            for asg in page.get("AutoScalingGroups", []):
                record = _audit_asg(asg, region)
                if record is not None:
                    results.append(record)
    except Exception as exc:
        log.warning("ASG describe failed for region %s: %s", region, exc)
    return results


# ── Public API ────────────────────────────────────────────────────────────────

def audit_spot_diversification(
    regions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Audit ASGs for spot instance type diversification.

    Returns records for ASGs that use spot instances. HIGH_RISK (<3 types)
    and MEDIUM_RISK (3-4 types) are flagged for action. OK (5+ types) are
    included for completeness.

    Sorted by risk level: HIGH_RISK first, then MEDIUM_RISK, then OK.
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
            asg_client = boto3.client("autoscaling", region_name=region)
            all_results.extend(_scan_region(asg_client, region))
        except Exception as exc:
            log.warning("Region %s failed: %s", region, exc)

    _risk_order = {_RISK_HIGH: 0, _RISK_MEDIUM: 1, _RISK_OK: 2}
    all_results.sort(key=lambda r: _risk_order.get(r["risk_level"], 9))
    return all_results
