# SPDX-License-Identifier: Apache-2.0
"""connector csv-costs: a CSV of daily costs as FOCUS rows.

The CSV (path in the EXAMPLE_COSTS_CSV secret) has a header row with:

    date,service,account,region,cost[,category][,provider]

Rows outside [start, end) are skipped. A row whose cost is not a number is
passed through as it is: nable's broker checks every row against its FOCUS
schema and drops (and reports) the ones that do not fit, so a connector does
not need to be the last line of defence.
"""
from __future__ import annotations

import csv
from datetime import date, timedelta


def _month(d: date) -> tuple[str, str]:
    first = d.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first.isoformat(), nxt.isoformat()


def _cost(raw: str):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return raw  # left for the broker to refuse


def fetch(ctx, start: str, end: str):
    path = ctx.secret("EXAMPLE_COSTS_CSV")
    if not path:
        raise RuntimeError("EXAMPLE_COSTS_CSV is not set: store the CSV's path with `nable "
                           "pack secret set com.example/example-csv-connector "
                           "EXAMPLE_COSTS_CSV`")
    lo, hi = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
    rows = []
    with open(path, encoding="utf-8", newline="") as f:
        for n, rec in enumerate(csv.DictReader(f), start=2):
            try:
                day = date.fromisoformat((rec.get("date") or "").strip())
            except ValueError:
                ctx.log(f"line {n}: no usable date, skipped")
                continue
            if not lo <= day < hi:
                continue
            cost = _cost((rec.get("cost") or "").strip())
            provider = (rec.get("provider") or "").strip() or "CSV export"
            bp_start, bp_end = _month(day)
            rows.append({
                "BilledCost": cost, "EffectiveCost": cost, "ListCost": cost,
                "ResourceId": "", "ResourceName": None, "ResourceType": "Service",
                "ServiceName": (rec.get("service") or "").strip() or "Unknown",
                "ServiceCategory": (rec.get("category") or "").strip() or "Other",
                "ProviderName": provider, "PublisherName": provider,
                "RegionId": (rec.get("region") or "").strip() or None, "RegionName": None,
                "BillingPeriodStart": bp_start, "BillingPeriodEnd": bp_end,
                "ChargePeriodStart": day.isoformat(),
                "ChargePeriodEnd": (day + timedelta(days=1)).isoformat(),
                "ChargeCategory": "Usage", "ChargeDescription": None,
                "CommitmentDiscountId": None, "CommitmentDiscountType": None,
                "SubAccountId": (rec.get("account") or "").strip() or None,
                "SubAccountName": None, "Tags": {},
            })
    ctx.log(f"read {len(rows)} rows for {start} to {end}")
    return rows
