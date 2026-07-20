"""
AWS CUR 2.0 to FOCUS 1.2 translator.

CUR 2.0 column reference:
  https://docs.aws.amazon.com/cur/latest/userguide/data-dictionary.html

AWS CUR 2.0 already aligns many column names with FOCUS. Where they differ,
this module maps native CUR fields to the FOCUS schema.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..schema import FocusRecord

# Maps AWS line_item_line_item_type values to FOCUS ChargeCategory
_CHARGE_CATEGORY_MAP: dict[str, str] = {
    "Usage": "Usage",
    "DiscountedUsage": "Usage",
    "SavingsPlanCoveredUsage": "Usage",
    "RIFee": "Purchase",
    "SavingsPlanRecurringFee": "Purchase",
    "Fee": "Purchase",
    "Tax": "Tax",
    "Credit": "Credit",
    "Refund": "Credit",
    "EdpDiscount": "Credit",
    "BundledDiscount": "Credit",
    "Adjustment": "Adjustment",
}

# Maps AWS product_servicename/product_product_family to FOCUS ServiceCategory
_SERVICE_CATEGORY_MAP: dict[str, str] = {
    "Amazon EC2": "Compute",
    "Amazon ECS": "Compute",
    "Amazon EKS": "Compute",
    "AWS Lambda": "Compute",
    "Amazon Lightsail": "Compute",
    "Amazon S3": "Storage",
    "Amazon EBS": "Storage",
    "Amazon EFS": "Storage",
    "Amazon Glacier": "Storage",
    "Amazon RDS": "Database",
    "Amazon DynamoDB": "Database",
    "Amazon ElastiCache": "Database",
    "Amazon Redshift": "Database",
    "Amazon Aurora": "Database",
    "Amazon DocumentDB": "Database",
    "Amazon Neptune": "Database",
    "Amazon VPC": "Networking",
    "Amazon CloudFront": "Networking",
    "Amazon Route 53": "Networking",
    "AWS Direct Connect": "Networking",
    "Amazon API Gateway": "Networking",
    "Amazon Bedrock": "AI and Machine Learning",
    "Amazon SageMaker": "AI and Machine Learning",
    "Amazon Rekognition": "AI and Machine Learning",
    "Amazon Comprehend": "AI and Machine Learning",
    "Amazon Translate": "AI and Machine Learning",
    "Amazon Polly": "AI and Machine Learning",
    "Amazon Lex": "AI and Machine Learning",
    "Amazon Textract": "AI and Machine Learning",
}

# AWS region code to human-readable name
_REGION_NAMES: dict[str, str] = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "eu-west-1": "Europe (Ireland)",
    "eu-west-2": "Europe (London)",
    "eu-west-3": "Europe (Paris)",
    "eu-central-1": "Europe (Frankfurt)",
    "eu-north-1": "Europe (Stockholm)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "sa-east-1": "South America (Sao Paulo)",
    "ca-central-1": "Canada (Central)",
    "me-south-1": "Middle East (Bahrain)",
    "af-south-1": "Africa (Cape Town)",
}


def _float(v: Any) -> float:
    try:
        return float(v) if v not in (None, "", "NULL") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _str(v: Any) -> str:
    return str(v).strip() if v not in (None, "", "NULL") else ""


def _parse_dt(v: Any) -> datetime:
    """Parse an ISO-style datetime string; fall back to epoch on failure."""
    if not v or v in ("", "NULL"):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(v), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _extract_tags(row: dict[str, Any]) -> dict[str, str]:
    """Pull resource_tags_user_* columns from a CUR row into a plain dict."""
    prefix = "resource_tags_user_"
    tags: dict[str, str] = {}
    for k, v in row.items():
        if k.startswith(prefix) and v and v != "NULL":
            tag_key = k[len(prefix):]
            tags[tag_key] = str(v)
    # Also handle a pre-aggregated "resource_tags" dict if present
    if isinstance(row.get("resource_tags"), dict):
        tags.update({k: str(v) for k, v in row["resource_tags"].items() if v})
    return tags


def _commitment_discount_id(row: dict[str, Any]) -> str | None:
    """Return the first non-empty commitment discount identifier."""
    for col in (
        "savings_plan_savings_plan_a_r_n",
        "reservation_reservation_a_r_n",
        "reservation_arn",
    ):
        val = _str(row.get(col, ""))
        if val:
            return val
    return None


def _commitment_discount_type(row: dict[str, Any]) -> str | None:
    """Derive commitment discount type from the CUR line item type."""
    li_type = _str(row.get("line_item_line_item_type", ""))
    if "SavingsPlan" in li_type:
        return "Savings Plan"
    if "DiscountedUsage" in li_type or "RIFee" in li_type:
        return "Reserved"
    return None


def _service_category(service_name: str) -> str:
    """Map a service name to a FOCUS ServiceCategory."""
    if service_name in _SERVICE_CATEGORY_MAP:
        return _SERVICE_CATEGORY_MAP[service_name]
    service_lower = service_name.lower()
    if any(w in service_lower for w in ("compute", "ec2", "lambda", "ecs", "eks", "fargate")):
        return "Compute"
    if any(w in service_lower for w in ("s3", "storage", "ebs", "efs", "glacier", "backup")):
        return "Storage"
    if any(w in service_lower for w in ("rds", "dynamo", "aurora", "redshift", "elasticache", "database", "db")):
        return "Database"
    if any(w in service_lower for w in ("vpc", "cloudfront", "route", "network", "direct connect", "api gateway", "transfer")):
        return "Networking"
    if any(w in service_lower for w in ("sagemaker", "bedrock", "rekognition", "comprehend", "ai", "ml")):
        return "AI and Machine Learning"
    return "Other"


def translate(row: dict[str, Any]) -> FocusRecord:
    """
    Translate a single AWS CUR 2.0 row dict into a FocusRecord.

    Missing fields are set to safe defaults (None or 0.0) rather than raising.
    """
    service_name = (
        _str(row.get("product_servicename"))
        or _str(row.get("line_item_product_code"))
        or "Unknown"
    )

    # Costs
    billed_cost = _float(row.get("line_item_blended_cost") or row.get("line_item_unblended_cost"))
    list_cost = _float(row.get("pricing_public_on_demand_cost") or row.get("line_item_unblended_cost"))

    li_type = _str(row.get("line_item_line_item_type", ""))
    if "SavingsPlan" in li_type:
        effective_cost = _float(row.get("savingsplan_savings_plan_effective_cost") or billed_cost)
    elif "DiscountedUsage" in li_type:
        effective_cost = _float(row.get("reservation_effective_cost") or billed_cost)
    else:
        effective_cost = billed_cost

    # Dates
    billing_start = _parse_dt(row.get("bill_billing_period_start_date"))
    billing_end = _parse_dt(row.get("bill_billing_period_end_date"))
    charge_start = _parse_dt(row.get("line_item_usage_start_date") or row.get("bill_billing_period_start_date"))
    charge_end = _parse_dt(row.get("line_item_usage_end_date") or row.get("bill_billing_period_end_date"))

    # Location
    region_id = _str(row.get("product_region") or row.get("product_location_region")) or None
    region_name = _REGION_NAMES.get(region_id or "")

    # Sub-account
    sub_account_id = _str(row.get("line_item_usage_account_id")) or None

    return FocusRecord(
        BilledCost=round(billed_cost, 10),
        EffectiveCost=round(effective_cost, 10),
        ListCost=round(list_cost, 10),
        ResourceId=_str(row.get("line_item_resource_id")) or "",
        ResourceName=_str(row.get("product_resourcename")) or None,
        ResourceType=_str(row.get("product_instance_type") or row.get("product_product_family")) or "Unknown",
        ServiceName=service_name,
        ServiceCategory=_service_category(service_name),
        ProviderName="AWS",
        PublisherName="AWS",
        RegionId=region_id,
        RegionName=region_name,
        BillingPeriodStart=billing_start,
        BillingPeriodEnd=billing_end,
        ChargePeriodStart=charge_start,
        ChargePeriodEnd=charge_end,
        ChargeCategory=_CHARGE_CATEGORY_MAP.get(li_type, "Usage"),
        ChargeDescription=_str(row.get("line_item_line_item_description")) or None,
        CommitmentDiscountId=_commitment_discount_id(row),
        CommitmentDiscountType=_commitment_discount_type(row),
        Tags=_extract_tags(row),
        SubAccountId=sub_account_id,
        SubAccountName=sub_account_id,
    )
