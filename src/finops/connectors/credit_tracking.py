"""
AWS credit-runway tracking + cash-billing-flip detection + AI-billing blind spots.

Why this exists: for an early AI-native startup, promotional credits (AWS
Activate, $1K-$100K) mask all cost pain for 12-24 months, then the bill snaps
from credits to cash with no warning. The #1 real trigger to care about cost is
that cliff, or a scary surprise invoice. AWS's own tooling has documented blind
spots here: Bedrock/Marketplace spend bypasses Cost Anomaly Detection, and
there is no notification when credits deplete and billing flips to cash.

This module reads Cost Explorer's RECORD_TYPE dimension (the "Charge type" in
the console) to separate gross usage, credits applied, and net cash paid, per
month. No CUR / S3 / Athena pipeline required: it uses the same
GetCostAndUsage permission cost queries already use, so it works on a read-only
key with zero extra setup. AWS exposes no API for the *remaining* Activate
balance, so this detects the trend and the flip from observed monthly data
rather than claiming a precise balance.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

log = logging.getLogger(__name__)

# Record types Cost Explorer reports as negative offsets to the bill.
_CREDIT_TYPES = {"Credit"}
_REFUND_TYPES = {"Refund"}


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    return date(y, m % 12 + 1, 1)


def _ce_client():
    from .aws import AWSConnector
    return AWSConnector()._make_client()


def fetch_record_type_monthly(months: int = 6, today: date | None = None, ce=None) -> list[dict]:
    """
    Pull monthly cost grouped by RECORD_TYPE from Cost Explorer.

    Returns a chronological list of:
      {"month": "YYYY-MM-DD", "gross": float, "credits": float,
       "refunds": float, "net_cash": float, "by_type": {type: usd}}
    where ``credits`` / ``refunds`` are positive magnitudes and ``net_cash`` is
    the bill after credits (sum of all unblended amounts, incl. negative ones).
    """
    today = today or date.today()
    start = _add_months(_month_start(today), -(months - 1))
    end = _add_months(_month_start(today), 1)  # exclusive, includes current month
    ce = ce or _ce_client()

    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
    )

    out: list[dict] = []
    for r in resp.get("ResultsByTime", []):
        month = r.get("TimePeriod", {}).get("Start", "")
        by_type: dict[str, float] = {}
        gross = credits = refunds = net = 0.0
        for g in r.get("Groups", []):
            rtype = (g.get("Keys") or ["Unknown"])[0]
            amt = float(g.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", 0.0))
            by_type[rtype] = round(amt, 4)
            net += amt
            if rtype in _CREDIT_TYPES:
                credits += -amt          # credit rows are negative; store magnitude
            elif rtype in _REFUND_TYPES:
                refunds += -amt
            elif amt > 0:
                gross += amt
        out.append({
            "month": month,
            "gross": round(gross, 2),
            "credits": round(credits, 2),
            "refunds": round(refunds, 2),
            "net_cash": round(net, 2),
            "by_type": by_type,
        })
    return out


def analyze_credits(per_month: list[dict]) -> dict[str, Any]:
    """
    Pure analysis over the monthly RECORD_TYPE series. Detects:
      - whether credits are meaningfully covering the bill,
      - a cash-flip (credits used to cover most of the bill, now they don't and
        net cash is being paid),
      - a declining-credit trend with a best-effort months-to-zero estimate.

    Coverage = credits / gross is used as the primary signal because it is
    scale-invariant to the current partial month (both shrink together).
    """
    if not per_month:
        return {"status": "no_data",
                "note": "No Cost Explorer data returned for the window."}

    grosses = [m["gross"] for m in per_month]
    credits = [m["credits"] for m in per_month]
    nets = [m["net_cash"] for m in per_month]
    coverage = [(c / g if g > 0 else 0.0) for c, g in zip(credits, grosses)]

    prior_cov = coverage[:-1]
    latest_cov = coverage[-1]
    prior_max_cov = max(prior_cov) if prior_cov else 0.0
    credits_active = prior_max_cov >= 0.30 or any(c > 1.0 for c in credits[:-1])

    latest_net = nets[-1]
    # Cash-flip: credits used to carry the bill, now coverage has collapsed and
    # real cash is going out the door.
    cash_flip = bool(credits_active and latest_cov < 0.10 and latest_net > 25.0)

    # Declining-credit trend + best-effort months-to-zero from a linear slope on
    # the credit magnitudes (only meaningful while credits are still flowing).
    months_to_zero = None
    trend = "none"
    nonzero_credits = [c for c in credits if c > 0]
    if len(nonzero_credits) >= 2:
        first_half = credits[: len(credits) // 2]
        second_half = credits[len(credits) // 2:]
        avg1 = sum(first_half) / max(1, len(first_half))
        avg2 = sum(second_half) / max(1, len(second_half))
        if avg2 < avg1 * 0.8:
            trend = "declining"
            slope = (credits[-1] - credits[0]) / max(1, len(credits) - 1)
            if slope < 0 and credits[-1] > 0:
                months_to_zero = max(0, round(credits[-1] / -slope, 1))
        elif avg2 > avg1 * 1.2:
            trend = "rising"
        else:
            trend = "steady"

    if cash_flip:
        status = "critical"
        headline = (
            f"Credits flipped to cash. Your bill was largely credit-covered and is "
            f"now ${latest_net:,.0f}/mo in real cash. This is the cliff."
        )
    elif credits_active and latest_cov < 0.50:
        status = "warning"
        headline = (
            f"Credit coverage is dropping ({latest_cov*100:.0f}% of the latest bill). "
            f"Cash exposure is climbing."
        )
    elif credits_active:
        status = "ok"
        headline = (
            f"Credits are covering {latest_cov*100:.0f}% of the bill. "
            f"Watch for the flip when they run low."
        )
    elif any(n > 25.0 for n in nets):
        status = "ok"
        headline = "Paying cash; no meaningful promotional credits detected."
    else:
        status = "ok"
        headline = "No significant spend or credits detected yet."

    return {
        "status": status,
        "headline": headline,
        "cash_flip_detected": cash_flip,
        "credits_active": credits_active,
        "latest_credit_coverage_pct": round(latest_cov * 100, 1),
        "latest_net_cash_usd": round(latest_net, 2),
        "credit_trend": trend,
        "estimated_months_to_zero_credits": months_to_zero,
        "monthly": per_month,
        "note": (
            "AWS exposes no API for remaining Activate credit balance, so runway "
            "is inferred from observed monthly credit consumption, not a stated "
            "balance. months-to-zero is a linear-trend estimate."
        ),
    }


def get_credit_status(months: int = 6, today: date | None = None, ce=None) -> dict[str, Any]:
    """End-to-end: fetch RECORD_TYPE monthly data and analyze it."""
    try:
        per_month = fetch_record_type_monthly(months=months, today=today, ce=ce)
    except Exception as e:
        log.warning("Credit status fetch failed: %s", e)
        return {"status": "error", "error": str(e),
                "note": "Cost Explorer RECORD_TYPE query failed (permissions or no data)."}
    return analyze_credits(per_month)


# ── AI-billing blind spots ──────────────────────────────────────────────────

# Services whose spend bypasses AWS Cost Anomaly Detection or routes through
# Marketplace (per documented 2026 surprise-bill cases). nable watches these
# explicitly because AWS's own detector does not.
_BLINDSPOT_HINTS = {
    "bedrock":     "Bedrock bills through AWS Marketplace and bypasses AWS Cost Anomaly Detection.",
    "marketplace": "Marketplace charges (third-party AI/SaaS) are not covered by AWS Cost Anomaly Detection.",
    "sagemaker":   "SageMaker inference/training spend can spike without a native anomaly alert.",
}


def detect_billing_blind_spots(by_service: dict[str, float]) -> dict[str, Any]:
    """
    From a service->USD breakdown (e.g. CostSummary.by_service), flag AI/Marketplace
    spend that AWS Cost Anomaly Detection does not watch. Pure + testable.
    """
    findings: list[dict] = []
    total_blind = 0.0
    for service, amount in by_service.items():
        if amount is None or amount <= 0:
            continue
        s = service.lower()
        for hint_key, reason in _BLINDSPOT_HINTS.items():
            if hint_key in s:
                findings.append({
                    "service": service,
                    "monthly_usd": round(float(amount), 2),
                    "reason": reason,
                })
                total_blind += float(amount)
                break

    findings.sort(key=lambda f: f["monthly_usd"], reverse=True)
    return {
        "blind_spot_count": len(findings),
        "total_blind_spot_usd": round(total_blind, 2),
        "findings": findings,
        "note": (
            "These line items are invisible to AWS Cost Anomaly Detection. nable "
            "watches them directly so a Bedrock or Marketplace spike does not go "
            "unnoticed until the invoice lands."
        ) if findings else "No Bedrock/Marketplace AI spend detected in this window.",
    }
