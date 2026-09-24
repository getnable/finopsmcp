"""
Cost estimation for infrastructure resource changes.

Uses AWS Pricing API where possible. Falls back to embedded snapshot
prices (refreshed monthly in the package) for common instance types.

Context-awareness: pulls your actual account data to add factual notes
(e.g. "4 similar instances exist in us-east-1 running at 12% avg CPU")
without editorializing about whether the new resource is needed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .parser import ResourceChange
from ..aws_prices import (
    ALB_PER_MONTH, EC2_MONTHLY, HOURS_PER_MONTH, NAT_GATEWAY_PER_MONTH, NLB_PER_MONTH,
    RDS_MONTHLY, lb_hourly,
)
from ..recommendations.rate_detector import detect_effective_rates

log = logging.getLogger(__name__)

# Monthly on-demand prices (us-east-1), used as fallback. Hourly list rate x 730
# from aws_prices, never typed in: the literals this replaced had GPU rows at
# hourly x 1000 (g4dn.xlarge $526/mo against $383.98), c6i at $0.084/hr against
# the $0.085 list rate, and RDS rows 7% to 24% over the RDS rate every other
# module used (db.m5.xlarge $280.32/mo against $249.66).
_EC2_MONTHLY = EC2_MONTHLY
_RDS_MONTHLY = RDS_MONTHLY

_FIXED_MONTHLY: dict[str, float] = {
    "aws_nat_gateway": NAT_GATEWAY_PER_MONTH,   # hourly base only; data processed is per GB
    "aws_lb": ALB_PER_MONTH,    # load_balancer_type overrides this below
    "aws_alb": ALB_PER_MONTH,
    "aws_nlb": NLB_PER_MONTH,
    "aws_eks_cluster": round(0.10 * HOURS_PER_MONTH, 2),   # $0.10/hr control plane
    "aws_redshift_cluster": 180.00,
    "aws_msk_cluster": 200.00,
    "aws_elasticache_cluster": 52.00,
    "aws_cloudfront_distribution": 0.0,  # pay-per-use
    "aws_lambda_function": 0.0,          # pay-per-use, negligible base
}

_EBS_MONTHLY_PER_GB: dict[str, float] = {
    "gp3": 0.08, "gp2": 0.10, "io1": 0.125, "io2": 0.125, "st1": 0.045, "sc1": 0.025,
}


@dataclass
class CostEstimate:
    resource_type: str
    resource_name: str
    action: str
    monthly_usd: float
    confidence: str  # "high" | "medium" | "low"
    notes: list[str] = field(default_factory=list)   # factual context from account
    breakdown: dict[str, float] = field(default_factory=dict)


def _ec2_monthly(instance_type: str, rate_multiplier: float = 1.0) -> float:
    base = _EC2_MONTHLY.get(instance_type)
    if base is None:
        # Try AWS Pricing API
        try:
            import boto3
            pricing = boto3.client("pricing", region_name="us-east-1")
            resp = pricing.get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
                    {"Type": "TERM_MATCH", "Field": "location", "Value": "US East (N. Virginia)"},
                    {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
                    {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
                    {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
                    {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
                ],
                MaxResults=1,
            )
            import json as _json
            for price_str in resp.get("PriceList", []):
                price_data = _json.loads(price_str)
                for term in price_data.get("terms", {}).get("OnDemand", {}).values():
                    for dim in term.get("priceDimensions", {}).values():
                        hourly = float(dim.get("pricePerUnit", {}).get("USD", 0))
                        if hourly > 0:
                            return round(hourly * HOURS_PER_MONTH * rate_multiplier, 2)
        except Exception:
            pass
        return 0.0
    return round(base * rate_multiplier, 2)


def _get_account_context(resource_type: str, instance_type: str | None, region: str = "us-east-1") -> list[str]:
    """
    Pull factual context from the account. No editorial — just facts.
    e.g. "4 similar instances exist in us-east-1 (avg CPU: 12%)"
    """
    notes = []
    if resource_type != "aws_instance" or not instance_type:
        return notes
    try:
        import boto3
        ec2 = boto3.client("ec2", region_name=region)
        resp = ec2.describe_instances(
            Filters=[
                {"Name": "instance-type", "Values": [instance_type]},
                {"Name": "instance-state-name", "Values": ["running"]},
            ]
        )
        count = sum(len(r["Instances"]) for r in resp.get("Reservations", []))
        if count > 0:
            notes.append(f"{count} existing {instance_type} instance(s) running in {region}")
    except Exception:
        pass
    return notes


def estimate_changes(
    changes: list[ResourceChange],
    region: str = "us-east-1",
) -> list[CostEstimate]:
    """
    Estimate monthly cost impact for a list of resource changes.
    Applies customer's effective rate automatically via rate_detector.
    """
    rates = detect_effective_rates()
    multiplier = rates.effective_multiplier()
    estimates = []

    for change in changes:
        rtype = change.resource_type
        props = change.properties
        action = change.action

        monthly = 0.0
        confidence = "medium"
        breakdown: dict[str, float] = {}
        notes: list[str] = []

        # EC2
        if rtype == "aws_instance":
            itype = props.get("instance_type", props.get("ami", ""))
            if itype:
                monthly = _ec2_monthly(itype, multiplier)
                confidence = "high" if itype in _EC2_MONTHLY else "medium"
                breakdown["compute"] = monthly
                notes.extend(_get_account_context(rtype, itype, region))

        # RDS
        elif rtype in ("aws_db_instance", "aws_rds_cluster_instance"):
            itype = props.get("instance_class", "db.t3.medium")
            base = _RDS_MONTHLY.get(itype, 140.0) * multiplier
            storage_gb = float(props.get("allocated_storage", 20))
            storage_cost = storage_gb * 0.115 * multiplier  # gp2 RDS storage
            monthly = round(base + storage_cost, 2)
            breakdown["compute"] = round(base, 2)
            breakdown["storage"] = round(storage_cost, 2)
            confidence = "high" if itype in _RDS_MONTHLY else "medium"

        # EBS
        elif rtype == "aws_ebs_volume":
            vol_type = props.get("type", "gp3")
            size_gb = float(props.get("size", props.get("volume_size", 20)))
            price_per_gb = _EBS_MONTHLY_PER_GB.get(vol_type, 0.08)
            monthly = round(size_gb * price_per_gb * multiplier, 2)
            breakdown["storage"] = monthly
            confidence = "high"

        # Fixed-cost resources
        elif rtype in _FIXED_MONTHLY:
            base = _FIXED_MONTHLY[rtype]
            if rtype == "aws_lb" and props.get("load_balancer_type"):
                base = round(lb_hourly(props["load_balancer_type"]) * HOURS_PER_MONTH, 2)
            monthly = round(base * multiplier, 2)
            breakdown["base"] = monthly
            confidence = "high" if monthly > 0 else "low"

        # EKS node group
        elif rtype == "aws_eks_node_group":
            itype = props.get("instance_types", "m5.large").split(",")[0].strip()
            desired = int(props.get("desired_size", props.get("scaling_config", "1").split()[0] if props.get("scaling_config") else 1))
            per_node = _ec2_monthly(itype, multiplier)
            monthly = round(per_node * desired, 2)
            breakdown["nodes"] = monthly
            confidence = "medium"
            notes.append(f"{desired}x {itype}")

        else:
            confidence = "low"

        if monthly == 0 and confidence == "low":
            continue  # skip unknowns with no estimate

        # For removals, cost is negative (savings)
        if action == "remove":
            monthly = -monthly

        if rates.has_private_pricing and rates.confidence != "low":
            notes.append(
                f"Estimate uses your effective rate ({rates.overall_discount_pct*100:.0f}% off list, "
                f"detected from {rates.source})"
            )

        estimates.append(CostEstimate(
            resource_type=rtype,
            resource_name=change.resource_name,
            action=action,
            monthly_usd=monthly,
            confidence=confidence,
            notes=notes,
            breakdown=breakdown,
        ))

    return estimates


def format_pr_comment(estimates: list[CostEstimate], threshold_usd: float = 10.0) -> str | None:
    """
    Format a GitHub PR comment from cost estimates.
    Returns None if total impact is below the threshold.
    """
    if not estimates:
        return None

    total = sum(e.monthly_usd for e in estimates)
    if abs(total) < threshold_usd:
        return None

    lines = ["**💰 nable cost estimate**\n"]
    lines.append("| Resource | Change | Est. / month |")
    lines.append("|---|---|---|")

    for e in estimates:
        sign = "+" if e.monthly_usd > 0 else ""
        action_label = {"add": "➕ add", "remove": "➖ remove", "modify": "✏️ modify"}.get(e.action, e.action)
        name = f"`{e.resource_type}` · {e.resource_name}"
        confidence_note = "" if e.confidence == "high" else " ⁽ᵉˢᵗ⁾"
        lines.append(f"| {name} | {action_label} | {sign}${e.monthly_usd:,.0f}{confidence_note} |")

    sign = "+" if total >= 0 else ""
    lines.append(f"|  | **Total** | **{sign}${total:,.0f} / month** |")

    # Factual context notes (no editorial)
    all_notes = [n for e in estimates for n in e.notes]
    if all_notes:
        lines.append("")
        for note in all_notes:
            lines.append(f"- {note}")

    lines.append("")
    lines.append("*⁽ᵉˢᵗ⁾ estimated · [nable](https://getnable.com)*")

    return "\n".join(lines)
