# SPDX-License-Identifier: Apache-2.0
"""What the bill looks like right now, from an AWS source that is free to read.

THE GAP THIS FILLS

The Cost and Usage Report is the right source for history: complete, itemised,
and cheap to read out of the customer's own bucket (connectors/cur_s3.py). It
has one weakness, and it is structural rather than fixable. AWS delivers CUR
files up to 24 hours behind, and the current day is always partial. So a
dashboard built only on the CUR is correct and slightly behind, and the first
question anyone asks is "what about today".

Cost Explorer answers that for $0.01 a question. CloudWatch answers it for
nothing. AWS publishes AWS/Billing EstimatedCharges into CloudWatch metrics,
and the read here is standard requests only: list_metrics to find the
services, then one GetMetricStatistics call for the total and one per
service. Those sit in CloudWatch's free request tier, 1,000,000 a month (AWS
Price List, CW:Requests). GetMetricData would batch the series into one call,
but it bills $0.01 per 1,000 metrics with no free tier (CW:GMD-Metrics), so
it is never called here. That is the reason this module exists: it closes the
freshness gap without putting a meter on asking.

WHAT THIS IS NOT

An estimate, and labelled one everywhere it surfaces. Specifically:

  - Month to date and CUMULATIVE, not daily. Today's spend is a difference
    between two datapoints, so it inherits both of their errors.
  - Refreshed roughly every six hours, not continuously.
  - Coarse. Service and linked account, no region, no resource, no tags.
  - Published only in us-east-1, whatever regions the spend is actually in.
  - Excludes some credits and refunds that land on the final invoice.

So it never writes to cost_snapshots. Measured history and a provisional
estimate are different kinds of fact, and the moment they share a table
somebody sums them. The CUR is what the bill WAS; this is what AWS currently
THINKS the bill will be. Callers get both, and get told which is which.

REQUIRES ONE ACCOUNT SETTING

AWS only publishes these metrics once billing alerts are switched on
(Billing console, Billing Preferences, "Receive CloudWatch billing alerts").
It is a checkbox and it is free, but until it is ticked the namespace is empty.
unavailable() says exactly that rather than reporting no spend, because a
missing setting that reads as $0.00 is the defect this codebase keeps finding.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..analyzers.cloudwatch import MetricPoints, MetricQuery, fetch_metric_points

log = logging.getLogger(__name__)

# AWS publishes billing metrics to us-east-1 only, regardless of where the
# spend happens. Pointing this at the caller's default region is the reason
# most hand-rolled versions of this read find nothing.
BILLING_METRIC_REGION = "us-east-1"
NAMESPACE = "AWS/Billing"
METRIC = "EstimatedCharges"

# EstimatedCharges refreshes about every six hours. Looking back 36 leaves room
# for a couple of missed publications without reporting a gap as a drop to zero.
_LOOKBACK_HOURS = 36
_PERIOD_SECONDS = 21_600


@dataclass
class ServiceCharge:
    service: str
    amount_usd: float
    as_of: datetime


@dataclass
class EstimatedCharges:
    """Month-to-date estimate. Provisional by construction, said so in the type."""
    total_usd: float | None = None
    by_service: list[ServiceCharge] = field(default_factory=list)
    as_of: datetime | None = None
    currency: str = "USD"

    @property
    def cost_usd(self) -> float:
        """What asking cost: nothing. Every call is a standard CloudWatch request
        (ListMetrics, GetMetricStatistics) inside the free tier of 1,000,000 a
        month (aws_prices.CLOUDWATCH_FREE_REQUESTS_PER_MONTH), and GetMetricData,
        which bills per metric with no free tier, is never called."""
        return 0.0

    @property
    def stale_hours(self) -> float | None:
        if self.as_of is None:
            return None
        return (datetime.now(timezone.utc) - self.as_of).total_seconds() / 3600.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_usd": None if self.total_usd is None else round(self.total_usd, 2),
            "by_service": [
                {"service": s.service, "amount_usd": round(s.amount_usd, 2)}
                for s in sorted(self.by_service, key=lambda s: -s.amount_usd)
            ],
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "stale_hours": None if self.stale_hours is None else round(self.stale_hours, 1),
            "currency": self.currency,
            "source": "cloudwatch_billing_metrics",
            # Repeated in the payload, not just the docstring, because this
            # number travels into dashboards and LLM answers that never read
            # either. An estimate that loses its label becomes an actual.
            "basis": "estimate",
            "note": ("AWS's own month-to-date estimate, refreshed roughly every "
                     "six hours. Not the invoice, and not itemised by region or "
                     "resource. Reading it is free."),
            "cost_to_read_usd": round(self.cost_usd, 6),
        }


def unavailable(reason: str) -> dict[str, Any]:
    """Absent, never zero.

    Billing alerts being off is a setting somebody has not ticked. Reporting it
    as $0.00 would tell a customer their spend stopped.
    """
    return {
        "total_usd": None,
        "by_service": [],
        "as_of": None,
        "source": "cloudwatch_billing_metrics",
        "basis": "unavailable",
        "error": "billing_metrics_unavailable",
        "message": reason,
        "setup": [
            "Open the AWS Billing console, then Billing Preferences.",
            "Tick 'Receive CloudWatch billing alerts' and save.",
            "AWS begins publishing within a few hours. There is no charge for it.",
        ],
        "note": ("This is a missing account setting, not a finding of zero "
                 "spend."),
    }


def _client(session: Any = None) -> Any:
    import boto3

    if session is not None:
        return session.client("cloudwatch", region_name=BILLING_METRIC_REGION)
    return boto3.client("cloudwatch", region_name=BILLING_METRIC_REGION)


def _discover_service_dimensions(cw: Any) -> list[str]:
    """Which services AWS is currently publishing charges for.

    Paginated because an account with many services exceeds one page, and a
    truncated read here would silently drop the tail of the bill.
    """
    services: list[str] = []
    paginator = cw.get_paginator("list_metrics")
    for page in paginator.paginate(Namespace=NAMESPACE, MetricName=METRIC):
        for m in page.get("Metrics", []) or []:
            dims = {d["Name"]: d["Value"] for d in m.get("Dimensions", [])}
            name = dims.get("ServiceName")
            if name and name not in services:
                services.append(name)
    return services


def latest_estimated_charges(session: Any = None, *,
                             include_services: bool = True) -> dict[str, Any]:
    """Month-to-date estimated charges, total and per service.

    list_metrics finds the services, then each series (the total, and one per
    service) is one GetMetricStatistics call through the shared CloudWatch
    reader, eight at a time. All of them are standard requests inside
    CloudWatch's free tier (see cost_usd). The read never takes the billed
    GetMetricData path, even on a host that opted in to it for scans, because
    the payload tells the reader that reading this is free.
    """
    if (os.getenv("NABLE_NO_CLOUDWATCH_BILLING") or "").strip().lower() in ("1", "true", "yes"):
        return unavailable("CloudWatch billing metrics are disabled here "
                           "(NABLE_NO_CLOUDWATCH_BILLING=1).")
    try:
        cw = _client(session)
    except Exception as exc:                    # pragma: no cover - import guard
        return unavailable(f"Could not create a CloudWatch client: {exc}")

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=_LOOKBACK_HOURS)

    try:
        services = _discover_service_dimensions(cw) if include_services else []
    except Exception as exc:
        log.debug("list_metrics for AWS/Billing failed: %s", exc)
        services = []

    # Maximum, because the metric is cumulative month-to-date: the largest
    # value in a period is the most recent one in it. Average would report
    # roughly half the month's spend and look plausible.
    usd = ("Currency", "USD")
    queries = [MetricQuery("total", NAMESPACE, METRIC, (usd,), "Maximum", _PERIOD_SECONDS)]
    queries += [
        MetricQuery(("service", svc), NAMESPACE, METRIC, (usd, ("ServiceName", svc)),
                    "Maximum", _PERIOD_SECONDS)
        for svc in services
    ]
    series = fetch_metric_points(cw, queries, start, end, use_get_metric_data=False)

    def latest(points: MetricPoints | None) -> tuple[float, datetime] | None:
        # Oldest first, so the last stamped point is the newest. Never
        # max(values): a restatement downward would then be ignored forever.
        stamped = [(ts, v) for ts, v in points or [] if ts is not None]
        if not stamped:
            return None
        ts, value = stamped[-1]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return float(value), ts

    if series.get("total") is None:
        return unavailable(
            "CloudWatch returned no billing metrics: the read failed. This usually "
            "means billing alerts have never been switched on for this account, or "
            "that this identity may not call cloudwatch:GetMetricStatistics.")
    total = latest(series["total"])
    if total is None:
        return unavailable(
            "AWS is not publishing AWS/Billing EstimatedCharges for this "
            "account. Billing alerts are almost certainly switched off.")

    out = EstimatedCharges(total_usd=total[0], as_of=total[1])
    for svc in services:
        got = latest(series.get(("service", svc)))
        if got and got[0] > 0:
            out.by_service.append(ServiceCharge(service=svc, amount_usd=got[0],
                                                as_of=got[1]))
    return out.as_dict()


def is_available(session: Any = None) -> bool:
    """Whether this account publishes billing metrics at all.

    Used to decide whether the CloudWatch path can cover the freshness gap, so it has
    to be a real read rather than a guess at configuration.
    """
    return latest_estimated_charges(session, include_services=False).get(
        "total_usd") is not None
