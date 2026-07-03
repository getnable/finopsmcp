"""Generic FOCUS 2.0 translator for usage-based SaaS providers.

The clouds (AWS, Azure, GCP) have bespoke translators for their rich billing
exports. Every usage-based SaaS connector, by contrast, emits the same uniform
CostEntry shape (service, amount, account, region, metadata), so one translator
maps them all. A connector turns its CostSummary into FOCUS records by calling
saas_focus_records with its provider name, publisher, and FOCUS ServiceCategory.

This is the long-tail engine: adding a new provider to the normalized dataset is
one get_costs_as_focus method, not a new translator.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any

from ..schema import FocusRecord, SERVICE_CATEGORIES

_CHARGE = {"usage": "Usage", "purchase": "Purchase", "tax": "Tax",
           "adjustment": "Adjustment", "credit": "Credit"}


def _amount(v: Any) -> float:
    """Coerce a cost to a finite float. NaN/inf from a provider API would poison
    sum() totals and break strict JSON parsing downstream, so clamp to 0.0."""
    try:
        f = float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return round(f, 10) if math.isfinite(f) else 0.0


def _period(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _tags(meta: Any) -> dict[str, str]:
    """Preserve simple metadata (credits, GB ingested, etc.) as FOCUS Tags so no
    provider-specific usage signal is lost when normalizing to the common schema."""
    if not isinstance(meta, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in meta.items():
        if isinstance(v, bool):
            out[str(k)] = "true" if v else "false"
        elif isinstance(v, (str, int, float)) and str(v) != "":
            out[str(k)] = str(v)
    return out


def entry_to_focus(
    entry: Any,
    *,
    provider: str,
    publisher: str,
    category: str,
    period_start: datetime,
    period_end: datetime,
    resource_type: str = "Service",
    charge_category: str = "Usage",
) -> FocusRecord:
    """Map one CostEntry to a FocusRecord. Provider/publisher/category are fixed
    by the connector; everything else comes from the entry."""
    if category not in SERVICE_CATEGORIES:
        category = "Other"
    amount = _amount(getattr(entry, "amount", 0.0))
    service = (getattr(entry, "service", "") or provider).strip() or provider
    region = (getattr(entry, "region", "") or "").strip() or None
    account = (getattr(entry, "account_id", "") or "").strip() or None
    account_name = (getattr(entry, "account_name", "") or "").strip() or account
    return FocusRecord(
        BilledCost=amount,
        EffectiveCost=amount,
        ListCost=amount,
        ResourceId=service,
        ResourceName=service,
        ResourceType=resource_type,
        ServiceName=service,
        ServiceCategory=category,
        ProviderName=provider,
        PublisherName=publisher,
        RegionId=region,
        RegionName=None,
        BillingPeriodStart=period_start,
        BillingPeriodEnd=period_end,
        ChargePeriodStart=period_start,
        ChargePeriodEnd=period_end,
        ChargeCategory=_CHARGE.get(charge_category.lower(), "Usage"),
        ChargeDescription=None,
        CommitmentDiscountId=None,
        CommitmentDiscountType=None,
        Tags=_tags(getattr(entry, "metadata", None)),
        SubAccountId=account,
        SubAccountName=account_name,
    )


def saas_focus_records(
    summary: Any,
    *,
    provider: str,
    publisher: str,
    category: str,
    start_date: date,
    end_date: date,
    resource_type: str = "Service",
) -> list[FocusRecord]:
    """Translate a connector's CostSummary into a list of FOCUS 2.0 records.

    Args:
        summary:       CostSummary returned by a connector's get_costs.
        provider:      FOCUS ProviderName (e.g. "Datadog").
        publisher:     FOCUS PublisherName (usually the same as provider).
        category:      FOCUS ServiceCategory; clamped to "Other" if unrecognized.
        start_date/end_date: charge/billing period bounds.
        resource_type: FOCUS ResourceType label for these line items.
    """
    ps = _period(start_date)
    pe = _period(end_date)
    return [
        entry_to_focus(
            e,
            provider=provider,
            publisher=publisher,
            category=category,
            period_start=ps,
            period_end=pe,
            resource_type=resource_type,
        )
        for e in (getattr(summary, "entries", None) or [])
    ]
