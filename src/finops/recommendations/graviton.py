"""
Graviton migration opportunity scanner.

Identifies x86_64 EC2 instances that have a direct Graviton (arm64)
equivalent. Graviton instances typically cost 20-40% less than x86
equivalents and deliver equal or better performance for most workloads.

Usage:
    from finops.recommendations.graviton import scan_graviton_opportunities
    results = await scan_graviton_opportunities(aws_connector)
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

from .graviton_prices import HOURLY_PRICE, HOURS_PER_MONTH, GRAVITON_SAVINGS_PCT

# Mapping from common x86_64 instance types to their Graviton equivalents.
# Targets m7g (general), c7g (compute), r7g (memory), t4g (burstable).
GRAVITON_MAP: dict[str, str] = {
    # General purpose: m5 family
    "m5.large":      "m7g.large",
    "m5.xlarge":     "m7g.xlarge",
    "m5.2xlarge":    "m7g.2xlarge",
    "m5.4xlarge":    "m7g.4xlarge",
    "m5.8xlarge":    "m7g.8xlarge",
    "m5.12xlarge":   "m7g.12xlarge",
    "m5.16xlarge":   "m7g.16xlarge",
    "m5.24xlarge":   "m7g.16xlarge",
    # General purpose: m5a family
    "m5a.large":     "m7g.large",
    "m5a.xlarge":    "m7g.xlarge",
    "m5a.2xlarge":   "m7g.2xlarge",
    "m5a.4xlarge":   "m7g.4xlarge",
    # General purpose: m6i family
    "m6i.large":     "m7g.large",
    "m6i.xlarge":    "m7g.xlarge",
    "m6i.2xlarge":   "m7g.2xlarge",
    "m6i.4xlarge":   "m7g.4xlarge",
    "m6i.8xlarge":   "m7g.8xlarge",
    # General purpose: m6a family
    "m6a.large":     "m7g.large",
    "m6a.xlarge":    "m7g.xlarge",
    "m6a.2xlarge":   "m7g.2xlarge",
    "m6a.4xlarge":   "m7g.4xlarge",
    # General purpose: m7i family (newer x86, Graviton is still cheaper)
    "m7i.large":     "m7g.large",
    "m7i.xlarge":    "m7g.xlarge",
    "m7i.2xlarge":   "m7g.2xlarge",
    "m7i.4xlarge":   "m7g.4xlarge",
    # Compute optimized: c5 family
    "c5.large":      "c7g.large",
    "c5.xlarge":     "c7g.xlarge",
    "c5.2xlarge":    "c7g.2xlarge",
    "c5.4xlarge":    "c7g.4xlarge",
    "c5.9xlarge":    "c7g.8xlarge",
    "c5.18xlarge":   "c7g.16xlarge",
    # Compute optimized: c6i family
    "c6i.large":     "c7g.large",
    "c6i.xlarge":    "c7g.xlarge",
    "c6i.2xlarge":   "c7g.2xlarge",
    "c6i.4xlarge":   "c7g.4xlarge",
    "c6i.8xlarge":   "c7g.8xlarge",
    # Compute optimized: c6a family
    "c6a.large":     "c7g.large",
    "c6a.xlarge":    "c7g.xlarge",
    "c6a.2xlarge":   "c7g.2xlarge",
    "c6a.4xlarge":   "c7g.4xlarge",
    # Memory optimized: r5 family
    "r5.large":      "r7g.large",
    "r5.xlarge":     "r7g.xlarge",
    "r5.2xlarge":    "r7g.2xlarge",
    "r5.4xlarge":    "r7g.4xlarge",
    "r5.8xlarge":    "r7g.8xlarge",
    "r5.12xlarge":   "r7g.12xlarge",
    # Memory optimized: r6i family
    "r6i.large":     "r7g.large",
    "r6i.xlarge":    "r7g.xlarge",
    "r6i.2xlarge":   "r7g.2xlarge",
    "r6i.4xlarge":   "r7g.4xlarge",
    "r6i.8xlarge":   "r7g.8xlarge",
    # Burstable: t3 family
    "t3.nano":       "t4g.nano",
    "t3.micro":      "t4g.micro",
    "t3.small":      "t4g.small",
    "t3.medium":     "t4g.medium",
    "t3.large":      "t4g.large",
    "t3.xlarge":     "t4g.xlarge",
    "t3.2xlarge":    "t4g.2xlarge",
    # Burstable: t3a family
    "t3a.nano":      "t4g.nano",
    "t3a.micro":     "t4g.micro",
    "t3a.small":     "t4g.small",
    "t3a.medium":    "t4g.medium",
    "t3a.large":     "t4g.large",
    "t3a.xlarge":    "t4g.xlarge",
    "t3a.2xlarge":   "t4g.2xlarge",
}


def _estimate_monthly_cost(instance_type: str) -> float:
    """Return estimated monthly cost in USD for an instance type."""
    hourly = HOURLY_PRICE.get(instance_type)
    if hourly is None:
        return 0.0
    return round(hourly * HOURS_PER_MONTH, 2)


def _compute_savings(
    current_type: str,
    graviton_type: str,
) -> tuple[float, float, float]:
    """
    Return (current_monthly_cost, savings_estimate, savings_pct).

    If both types are in the price table, uses exact prices.
    Falls back to the GRAVITON_SAVINGS_PCT ratio for unknowns.
    """
    current_monthly = _estimate_monthly_cost(current_type)
    graviton_monthly = _estimate_monthly_cost(graviton_type)

    if current_monthly > 0 and graviton_monthly > 0:
        savings = round(current_monthly - graviton_monthly, 2)
        pct = round(savings / current_monthly * 100, 1)
        return current_monthly, max(savings, 0.0), max(pct, 0.0)

    if current_monthly > 0:
        # Graviton type not in price table; apply fallback ratio
        savings = round(current_monthly * GRAVITON_SAVINGS_PCT, 2)
        return current_monthly, savings, round(GRAVITON_SAVINGS_PCT * 100, 1)

    # Neither type is priced; return zeros
    return 0.0, 0.0, round(GRAVITON_SAVINGS_PCT * 100, 1)


async def scan_graviton_opportunities(
    aws_client: Any,
    regions: list[str] | None = None,
) -> list[dict]:
    """
    Scan running EC2 instances for Graviton migration candidates.

    Args:
        aws_client: An AWSConnector instance (used to derive boto3 sessions).
        regions:    AWS regions to scan. Defaults to us-east-1 if not provided.

    Returns:
        List of dicts, one per candidate, sorted by savings_estimate descending.
        Each dict contains:
            instance_id, instance_type, graviton_equivalent,
            current_monthly_cost_estimate, savings_estimate, savings_pct,
            region, name_tag, account_id
    """
    import boto3

    if not regions:
        # Default to the region that's most commonly used; callers can expand
        regions = ["us-east-1"]

    # Build a boto3 session. Use the injected session if available.
    session: Any
    if getattr(aws_client, "_session", None):
        session = aws_client._session
    else:
        session = boto3.session.Session()

    results: list[dict] = []

    for region in regions:
        try:
            ec2 = session.client("ec2", region_name=region)
            account_id = _get_account_id(session)
            paginator = ec2.get_paginator("describe_instances")
            pages = paginator.paginate(
                Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
            )
            for page in pages:
                for reservation in page.get("Reservations", []):
                    for inst in reservation.get("Instances", []):
                        arch = inst.get("Architecture", "")
                        if arch != "x86_64":
                            continue

                        itype = inst.get("InstanceType", "")
                        graviton_type = GRAVITON_MAP.get(itype)
                        if not graviton_type:
                            continue

                        iid = inst.get("InstanceId", "")
                        name_tag = _get_name_tag(inst.get("Tags", []))
                        current_cost, savings, savings_pct = _compute_savings(
                            itype, graviton_type
                        )

                        results.append(
                            {
                                "instance_id": iid,
                                "instance_type": itype,
                                "graviton_equivalent": graviton_type,
                                "current_monthly_cost_estimate": current_cost,
                                "savings_estimate": savings,
                                "savings_pct": savings_pct,
                                "region": region,
                                "name_tag": name_tag,
                                "account_id": account_id,
                            }
                        )
        except Exception as exc:
            log.warning("Graviton scan failed for region %s: %s", region, exc)
            continue

    results.sort(key=lambda r: r["savings_estimate"], reverse=True)
    return results


def _get_name_tag(tags: list[dict]) -> str:
    for tag in tags:
        if tag.get("Key") == "Name":
            return tag.get("Value", "")
    return ""


def _get_account_id(session: Any) -> str:
    try:
        sts = session.client("sts")
        return sts.get_caller_identity()["Account"]
    except Exception:
        return "unknown"
