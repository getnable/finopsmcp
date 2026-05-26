"""
AWS Marketplace cost analyzer.

Breaks Marketplace spend out by product/vendor using Cost Explorer
USAGE_TYPE grouping, surfaces MoM trends, and flags high-spend products.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any


def _make_ce(role_arn: str | None = None):
    import boto3

    if role_arn:
        sts = boto3.client("sts")
        creds = sts.assume_role(RoleArn=role_arn, RoleSessionName="finops-mktplace")["Credentials"]
        return boto3.client(
            "ce",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name="us-east-1",
        )
    return boto3.client("ce", region_name="us-east-1")


def _extract_vendor(usage_type: str) -> str:
    """
    Attempt to extract a vendor/product name from a Marketplace usage type string.
    Usage types look like: "AWS-Marketplace:VendorProduct-HourlyUsage" or similar.
    """
    # Strip region prefix
    parts = usage_type.split("-", 1)
    rest = parts[1] if len(parts) > 1 and parts[0].upper() in ("USE1", "USW2", "EUW1", "APS1", "APN1") else usage_type

    # Strip "AWS-Marketplace:" or "AWSMarketplace:" prefix
    for prefix in ("AWS-Marketplace:", "AWSMarketplace:", "Marketplace:"):
        if rest.startswith(prefix):
            rest = rest[len(prefix):]
            break

    # Clean up trailing usage type keywords
    for suffix in ("-HourlyUsage", "-Monthly", "-Annual", ":Usage", ":monthly", ":hourly"):
        if rest.endswith(suffix):
            rest = rest[: -len(suffix)]

    return rest.strip() or usage_type


def _ce_query(ce, start: str, end: str) -> list[dict]:
    results: list[dict] = []
    kwargs: dict[str, Any] = dict(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        Filter={
            "Dimensions": {
                "Key": "SERVICE",
                "Values": ["AWS Marketplace"],
            }
        },
        GroupBy=[
            {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
        ],
    )
    while True:
        resp = ce.get_cost_and_usage(**kwargs)
        results.extend(resp.get("ResultsByTime", []))
        token = resp.get("NextPageToken")
        if not token:
            break
        kwargs["NextPageToken"] = token
    return results


class MarketplaceAnalyzer:
    def __init__(self, role_arn: str | None = None) -> None:
        self.role_arn = role_arn

    def get_costs(self, days: int = 30) -> str:
        end = date.today()
        start = end - timedelta(days=days)
        prior_start = start - timedelta(days=days)

        ce = _make_ce(self.role_arn)

        current_rows = _ce_query(ce, start.isoformat(), end.isoformat())
        prior_rows = _ce_query(ce, prior_start.isoformat(), start.isoformat())

        # Aggregate current and prior by usage type
        current_by_usage: dict[str, float] = {}
        for period in current_rows:
            for group in period.get("Groups", []):
                usage_type = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                current_by_usage[usage_type] = current_by_usage.get(usage_type, 0.0) + amount

        prior_by_usage: dict[str, float] = {}
        for period in prior_rows:
            for group in period.get("Groups", []):
                usage_type = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                prior_by_usage[usage_type] = prior_by_usage.get(usage_type, 0.0) + amount

        total = sum(current_by_usage.values())
        if total == 0.0:
            return "No AWS Marketplace spend found in the selected period."

        # Group by vendor name (usage type can repeat for same vendor)
        vendor_current: dict[str, float] = {}
        vendor_prior: dict[str, float] = {}
        vendor_raw: dict[str, str] = {}  # vendor_name -> representative usage_type

        for usage_type, amount in current_by_usage.items():
            vendor = _extract_vendor(usage_type)
            vendor_current[vendor] = vendor_current.get(vendor, 0.0) + amount
            vendor_raw[vendor] = usage_type

        for usage_type, amount in prior_by_usage.items():
            vendor = _extract_vendor(usage_type)
            vendor_prior[vendor] = vendor_prior.get(vendor, 0.0) + amount

        sorted_vendors = sorted(vendor_current.items(), key=lambda x: -x[1])

        lines: list[str] = [
            f"AWS Marketplace costs (last {days} days): ${total:,.2f}",
            "",
            "By product:",
        ]

        high_spend: list[str] = []

        for vendor, amount in sorted_vendors:
            if amount < 0.01:
                continue
            pct = amount / total * 100
            prior = vendor_prior.get(vendor, 0.0)

            trend_str = ""
            if prior > 0:
                change = (amount - prior) / prior * 100
                direction = "+" if change >= 0 else ""
                trend_str = f"  {direction}{change:.0f}% MoM"

            flag = "  [HIGH SPEND]" if amount > 1000.0 else ""
            lines.append(f"  {vendor:<40}  ${amount:>9,.2f}  ({pct:.0f}%){trend_str}{flag}")

            if amount > 1000.0:
                high_spend.append(vendor)

        if high_spend:
            lines += [
                "",
                f"Products with >$1,000 spend: {', '.join(high_spend)}",
                "Review these subscriptions to confirm they are actively used.",
            ]

        return "\n".join(lines)
