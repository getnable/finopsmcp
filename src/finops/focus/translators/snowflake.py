"""FOCUS 2.0 translator for Snowflake.

The first usage-based (non-cloud) provider in nable's FOCUS layer, and the reference
for the rest of the long tail. Snowflake bills warehouse compute in credits (and
storage in TB). nable's connector reports credits per warehouse and, when the user
supplies their contract credit price, the dollar amount. This maps one such row to a
FocusRecord: warehouse compute is Database service usage, and the credits consumed are
recorded in the description so nothing is lost when there is no dollar figure.

Raw row shape (nable-native, produced by SnowflakeConnector.get_costs_as_focus):
  billed_cost, service_name, resource_id, resource_name, credits, account, region,
  charge_category, service_category, billing_period_start/end, charge_period_start/end, tags
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..schema import FocusRecord

_CHARGE = {"usage": "Usage", "purchase": "Purchase", "tax": "Tax",
           "adjustment": "Adjustment", "credit": "Credit"}


def _f(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _s(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _dt(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def translate(row: dict[str, Any]) -> FocusRecord:
    """Translate one Snowflake cost row (nable-native shape) into a FocusRecord."""
    amount = round(_f(row.get("billed_cost")), 10)
    service = _s(row.get("service_name")) or "Snowflake"
    credits = _f(row.get("credits"))
    cs = _dt(row.get("charge_period_start"))
    ce = _dt(row.get("charge_period_end"))
    bs = _dt(row.get("billing_period_start")) or cs
    be = _dt(row.get("billing_period_end")) or ce
    return FocusRecord(
        BilledCost=amount,
        EffectiveCost=amount,
        ListCost=amount,
        ResourceId=_s(row.get("resource_id")) or service,
        ResourceName=_s(row.get("resource_name")) or service,
        ResourceType="Warehouse",
        ServiceName=service,
        ServiceCategory=_s(row.get("service_category")) or "Database",
        ProviderName="Snowflake",
        PublisherName="Snowflake",
        RegionId=_s(row.get("region")) or None,
        RegionName=None,
        BillingPeriodStart=bs,
        BillingPeriodEnd=be,
        ChargePeriodStart=cs,
        ChargePeriodEnd=ce,
        ChargeCategory=_CHARGE.get(_s(row.get("charge_category")).lower(), "Usage"),
        ChargeDescription=(f"{credits:g} credits consumed" if credits else None),
        CommitmentDiscountId=None,
        CommitmentDiscountType=None,
        Tags=dict(row.get("tags") or {}),
        SubAccountId=_s(row.get("account")) or None,
        SubAccountName=_s(row.get("account")) or None,
    )
