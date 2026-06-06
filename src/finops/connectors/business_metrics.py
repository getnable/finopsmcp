"""
Business metrics store and unit economics engine.

Connects cloud + SaaS costs to business outcomes so teams can answer
the "so what?" question when spend changes.

Metrics tracked over time:
    arr_usd             Annual Recurring Revenue
    mrr_usd             Monthly Recurring Revenue
    mau                 Monthly Active Users
    dau                 Daily Active Users
    paying_customers    Number of paying customers
    api_calls_monthly   API calls per month (your product's API, not cloud APIs)
    employees           Headcount
    custom_metrics      Any metric your team cares about (dict)

Unit economics computed:
    hosting_pct_arr         Infrastructure cost as % of ARR
    hosting_pct_mrr         Infrastructure cost as % of MRR
    cost_per_customer       Total infra cost / paying customers
    cost_per_mau            Total infra cost / MAU
    cost_per_api_call       Total infra cost / API calls
    cost_per_employee       Total infra cost / headcount
    gross_margin_impact_pct How much infra is eating into gross margin
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)


# ── Read / write helpers ──────────────────────────────────────────────────────

def save_metrics(
    metric_date: str,
    arr_usd: float | None = None,
    mrr_usd: float | None = None,
    mau: int | None = None,
    dau: int | None = None,
    paying_customers: int | None = None,
    api_calls_monthly: int | None = None,
    employees: int | None = None,
    custom_metrics: dict | None = None,
    notes: str | None = None,
    cash_on_hand_usd: float | None = None,
    last_raise_amount_usd: float | None = None,
    last_raise_date: str | None = None,
    monthly_opex_usd: float | None = None,
) -> dict:
    """
    Upsert business metrics for a given date.
    If a row already exists for that date, it is replaced.

    Runway inputs (cash_on_hand_usd, last_raise_amount_usd, last_raise_date,
    monthly_opex_usd) power compute_runway(). nable sees infra spend, not payroll,
    so monthly_opex_usd is needed for true company runway.
    """
    from ..storage.db import business_metrics as bm_table, get_engine
    from sqlalchemy import select, delete

    engine = get_engine()
    now = datetime.now(timezone.utc)

    row = {
        "metric_date":           metric_date,
        "arr_usd":               arr_usd,
        "mrr_usd":               mrr_usd,
        "mau":                   mau,
        "dau":                   dau,
        "paying_customers":      paying_customers,
        "api_calls_monthly":     api_calls_monthly,
        "employees":             employees,
        "custom_metrics":        json.dumps(custom_metrics) if custom_metrics else None,
        "notes":                 notes,
        "cash_on_hand_usd":      cash_on_hand_usd,
        "last_raise_amount_usd": last_raise_amount_usd,
        "last_raise_date":       last_raise_date,
        "monthly_opex_usd":      monthly_opex_usd,
        "captured_at":           now,
    }

    with engine.begin() as conn:
        # Merge-upsert: a user often sets revenue/customers in one call and the
        # runway inputs (cash, opex) in a separate call on the same date. A plain
        # delete-then-insert would clobber the first call. So we overlay only the
        # fields explicitly provided this call (non-None) onto any existing row,
        # preserving everything else. custom_metrics dicts are shallow-merged.
        existing = conn.execute(
            bm_table.select().where(bm_table.c.metric_date == metric_date)
        ).fetchone()

        if existing is not None:
            merged = dict(existing._mapping)
            merged.pop("id", None)
            for key, value in row.items():
                if key == "custom_metrics":
                    if custom_metrics:
                        prior = merged.get("custom_metrics")
                        prior_dict = {}
                        if prior:
                            try:
                                prior_dict = json.loads(prior)
                            except Exception:
                                prior_dict = {}
                        prior_dict.update(custom_metrics)
                        merged["custom_metrics"] = json.dumps(prior_dict)
                    # if no new custom_metrics this call, keep the prior value
                elif value is not None:
                    merged[key] = value
            merged["captured_at"] = now
            conn.execute(delete(bm_table).where(bm_table.c.metric_date == metric_date))
            conn.execute(bm_table.insert().values(**merged))
        else:
            conn.execute(bm_table.insert().values(**row))

    return {"saved": True, "metric_date": metric_date}


def get_latest_metrics(n: int = 1) -> list[dict]:
    """Return the N most recent metric rows."""
    from ..storage.db import business_metrics as bm_table, get_engine
    from sqlalchemy import select

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(bm_table).order_by(bm_table.c.metric_date.desc()).limit(n)
        ).fetchall()

    return [_row_to_dict(r) for r in rows]


def get_metrics_history(days: int = 90) -> list[dict]:
    """Return all metric rows within the last N days, oldest first."""
    from ..storage.db import business_metrics as bm_table, get_engine
    from sqlalchemy import select

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(bm_table)
            .where(bm_table.c.metric_date >= cutoff)
            .order_by(bm_table.c.metric_date.asc())
        ).fetchall()

    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row) -> dict:
    d = dict(row._mapping)
    if d.get("custom_metrics"):
        try:
            d["custom_metrics"] = json.loads(d["custom_metrics"])
        except Exception:
            pass
    if isinstance(d.get("captured_at"), datetime):
        d["captured_at"] = d["captured_at"].isoformat()
    return d


# ── Auto-population from billing connectors ───────────────────────────────────

async def _stripe_snapshot() -> dict:
    """Best-effort Stripe MRR + paying-customer pull. Returns {} on any failure."""
    try:
        from .saas.stripe import StripeConnector

        sc = StripeConnector()
        if not await sc.is_configured():
            return {}
        return await sc.fetch_business_snapshot()
    except Exception as e:  # network, auth, schema drift: never block the caller
        log.debug("Stripe snapshot unavailable: %s", e)
        return {}


async def resolve_business_metrics(allow_stripe: bool = True) -> dict:
    """
    Return the best available business metrics for unit-economics math.

    Precedence:
      1. Stored metrics that already carry a revenue signal (mrr/arr/customers).
         Manual entry always wins, so a founder who set numbers is never
         overwritten by an estimate.
      2. A live Stripe snapshot (active subscriptions -> MRR + paying customers),
         persisted as today's row so it trends over time. This is what makes
         cost-per-customer and AI-as-%-of-MRR work the first time someone asks,
         with zero manual data entry.
      3. Whatever partial stored metrics exist (e.g. headcount only), else {}.

    The returned dict may carry non-metric keys used only for display:
      _source        "stored" | "stripe" | "stored+stripe" | "none"
      _stripe_as_of  ISO timestamp of the Stripe pull, when one happened
      _stripe_caveats list of accuracy notes from the Stripe pull
    """
    latest_rows = get_latest_metrics(n=1)
    base: dict[str, Any] = dict(latest_rows[0]) if latest_rows else {}

    def _has_revenue(d: dict) -> bool:
        return bool(d.get("mrr_usd") or d.get("arr_usd") or d.get("paying_customers"))

    if _has_revenue(base):
        base["_source"] = "stored"
        return base

    if allow_stripe:
        snap = await _stripe_snapshot()
        if snap and (snap.get("mrr_usd") or snap.get("paying_customers")):
            today = date.today().isoformat()
            note = "Auto-populated from Stripe active subscriptions."
            if snap.get("caveats"):
                note += " " + " ".join(snap["caveats"])
            try:
                save_metrics(
                    metric_date=today,
                    mrr_usd=snap.get("mrr_usd") or None,
                    paying_customers=snap.get("paying_customers") or None,
                    notes=note,
                )
            except Exception as e:
                log.debug("failed to persist Stripe snapshot: %s", e)
            if snap.get("mrr_usd"):
                base["mrr_usd"] = snap["mrr_usd"]
            if snap.get("paying_customers"):
                base["paying_customers"] = snap["paying_customers"]
            base["metric_date"] = today
            base["_source"] = "stored+stripe" if latest_rows else "stripe"
            base["_stripe_as_of"] = snap.get("as_of")
            base["_stripe_caveats"] = snap.get("caveats") or []
            return base

    base["_source"] = "stored" if latest_rows else "none"
    return base


# ── Unit economics engine ─────────────────────────────────────────────────────

def compute_unit_economics(total_cost_usd: float, metrics: dict) -> dict:
    """
    Given total infrastructure cost and a dict of business metrics,
    return a full unit economics breakdown.

    All ratios are monthly. If annual metrics are passed (arr_usd),
    they are divided by 12 to get the monthly equivalent.
    """
    result: dict[str, Any] = {}

    arr = metrics.get("arr_usd")
    mrr = metrics.get("mrr_usd") or (arr / 12 if arr else None)
    mau = metrics.get("mau")
    dau = metrics.get("dau")
    customers = metrics.get("paying_customers")
    api_calls = metrics.get("api_calls_monthly")
    employees = metrics.get("employees")
    custom = metrics.get("custom_metrics") or {}

    if mrr:
        pct = (total_cost_usd / mrr) * 100
        result["hosting_pct_mrr"] = round(pct, 2)
        result["hosting_pct_mrr_label"] = f"{pct:.1f}% of MRR"
        # Benchmark context
        if pct < 8:
            result["hosting_pct_mrr_health"] = "healthy"
            result["hosting_pct_mrr_note"] = "Below 8% of MRR. Well within typical SaaS range."
        elif pct < 15:
            result["hosting_pct_mrr_health"] = "watch"
            result["hosting_pct_mrr_note"] = "8-15% of MRR. Normal for growth-stage SaaS but worth optimizing."
        elif pct < 25:
            result["hosting_pct_mrr_health"] = "elevated"
            result["hosting_pct_mrr_note"] = "15-25% of MRR. High. Infrastructure is a meaningful drag on gross margin."
        else:
            result["hosting_pct_mrr_health"] = "critical"
            result["hosting_pct_mrr_note"] = f"Over 25% of MRR. This is a gross margin problem that needs immediate attention."

    if arr:
        pct_arr = (total_cost_usd * 12 / arr) * 100
        result["hosting_pct_arr"] = round(pct_arr, 2)
        result["hosting_pct_arr_label"] = f"{pct_arr:.1f}% of ARR"

    if customers and customers > 0:
        cpp = total_cost_usd / customers
        result["cost_per_customer_usd"] = round(cpp, 4)
        result["cost_per_customer_label"] = f"${cpp:.2f} per customer / month"

    if mau and mau > 0:
        cpu = total_cost_usd / mau
        result["cost_per_mau_usd"] = round(cpu, 4)
        result["cost_per_mau_label"] = f"${cpu:.4f} per MAU / month"

    if dau and dau > 0:
        cpd = total_cost_usd / dau
        result["cost_per_dau_usd"] = round(cpd, 4)
        result["cost_per_dau_label"] = f"${cpd:.4f} per DAU / month"

    if api_calls and api_calls > 0:
        cpa = total_cost_usd / api_calls * 1000
        result["cost_per_1k_api_calls_usd"] = round(cpa, 6)
        result["cost_per_1k_api_calls_label"] = f"${cpa:.4f} per 1k API calls"

    if employees and employees > 0:
        cpe = total_cost_usd / employees
        result["cost_per_employee_usd"] = round(cpe, 2)
        result["cost_per_employee_label"] = f"${cpe:.2f} per employee / month"

    # Custom metric ratios
    for metric_name, metric_value in custom.items():
        try:
            v = float(metric_value)
            if v > 0:
                ratio = total_cost_usd / v
                result[f"cost_per_{metric_name}"] = round(ratio, 6)
        except Exception:
            pass

    return result


# ── Runway engine ─────────────────────────────────────────────────────────────

def compute_runway(
    cash_on_hand_usd: float | None,
    infra_monthly_burn_usd: float,
    monthly_opex_usd: float | None = None,
    mrr_usd: float | None = None,
) -> dict:
    """
    Compute runway from cash and burn.

    nable sees infra + inference spend, not payroll. So:
      - If monthly_opex_usd is supplied, we compute true COMPANY runway:
        net burn = total opex - monthly revenue, runway = cash / net burn.
      - If not, we compute INFRA runway: cash / infra spend. This is a ceiling,
        not real runway (it ignores payroll), and is labelled as such so a
        founder is never shown an infra-only number as if it were company runway.

    Returns {"available": False, "reason": ...} when inputs are missing, and
    guards burn == 0 (no ZeroDivisionError, returns months=None with a note).
    """
    if not cash_on_hand_usd or cash_on_hand_usd <= 0:
        return {
            "available": False,
            "reason": "Set cash_on_hand_usd with set_business_metrics() to see runway.",
        }

    mrr = mrr_usd or 0.0

    if monthly_opex_usd and monthly_opex_usd > 0:
        net_burn = monthly_opex_usd - mrr
        mode = "company"
        burn_label = "total net burn (opex minus revenue)"
        if net_burn <= 0:
            return {
                "available": True,
                "mode": "company",
                "months": None,
                "cash_on_hand_usd": round(cash_on_hand_usd, 2),
                "net_burn_usd": round(net_burn, 2),
                "label": "Cash-flow positive: revenue covers total opex, no runway limit at current burn.",
                "note": "Monthly revenue meets or exceeds total monthly opex.",
            }
        burn = net_burn
    else:
        mode = "infra"
        burn_label = "infra + inference spend (excludes payroll)"
        if infra_monthly_burn_usd <= 0:
            return {
                "available": False,
                "reason": "No infra spend recorded yet, so infra runway cannot be computed.",
            }
        burn = infra_monthly_burn_usd

    months = cash_on_hand_usd / burn

    # Project the runway-out date. Use 30.44 days/month (365.25 / 12).
    runway_end = date.today() + timedelta(days=int(round(months * 30.44)))

    if mode == "company":
        label = f"Company runway: {months:.1f} months at ${burn:,.0f}/mo net burn."
        note = "True runway: cash divided by total opex minus revenue."
    else:
        label = (
            f"Infra runway: {months:.1f} months if cash only had to cover "
            f"infra + inference spend. Real runway is shorter once payroll is "
            f"included. Set monthly_opex_usd for company runway."
        )
        note = "Infra-only ceiling, excludes payroll. Not true company runway."

    return {
        "available": True,
        "mode": mode,
        "months": round(months, 1),
        "cash_on_hand_usd": round(cash_on_hand_usd, 2),
        "monthly_burn_usd": round(burn, 2),
        "burn_basis": burn_label,
        "runway_end_date": runway_end.isoformat(),
        "label": label,
        "note": note,
    }


# ── "So what?" analyzer ───────────────────────────────────────────────────────

def explain_cost_change(
    cost_now: float,
    cost_before: float,
    metrics_now: dict,
    metrics_before: dict,
    period_label: str = "this period vs last period",
) -> dict:
    """
    Given cost and business metrics for two periods, explain what the cost
    change actually means for the business.

    Returns a structured explanation with a plain-English narrative,
    efficiency trend, and recommended action.
    """
    cost_delta = cost_now - cost_before
    cost_pct = ((cost_now - cost_before) / cost_before * 100) if cost_before else None

    econ_now = compute_unit_economics(cost_now, metrics_now)
    econ_before = compute_unit_economics(cost_before, metrics_before)

    findings: list[str] = []
    signals: list[dict] = []

    direction = "up" if cost_delta > 0 else "down"
    abs_delta = abs(cost_delta)
    pct_str = f"{abs(cost_pct):.1f}%" if cost_pct is not None else "unknown %"

    # ── MAU efficiency ────────────────────────────────────────────────────────
    mau_now = metrics_now.get("mau")
    mau_before = metrics_before.get("mau")
    if mau_now and mau_before and mau_before > 0:
        mau_pct = (mau_now - mau_before) / mau_before * 100
        cpu_now = econ_now.get("cost_per_mau_usd")
        cpu_before = econ_before.get("cost_per_mau_usd")
        if cpu_now and cpu_before:
            cpu_delta_pct = (cpu_now - cpu_before) / cpu_before * 100
            if mau_pct > 2 and cpu_delta_pct < 0:
                signals.append({"type": "positive", "metric": "cost_per_mau",
                                 "label": f"Cost per MAU improved {abs(cpu_delta_pct):.1f}% as users grew {mau_pct:.1f}%"})
                findings.append(
                    f"MAU grew {mau_pct:.1f}% while cost per user dropped "
                    f"from ${cpu_before:.4f} to ${cpu_now:.4f}. Your infrastructure is scaling efficiently."
                )
            elif mau_pct < -2:
                signals.append({"type": "negative", "metric": "mau",
                                 "label": f"User base shrinking ({mau_pct:.1f}%) while costs went {direction}"})
                findings.append(
                    f"MAU dropped {abs(mau_pct):.1f}% but costs went {direction} ${abs_delta:,.2f}. "
                    f"You are paying more to serve fewer users. Investigate immediately."
                )
            elif abs(mau_pct) < 2 and cost_delta > 0:
                signals.append({"type": "negative", "metric": "cost_per_mau",
                                 "label": f"Costs up {pct_str} with flat user growth"})
                findings.append(
                    f"User base is flat but costs rose ${abs_delta:,.2f} ({pct_str}). "
                    f"This is pure cost inflation, not growth-driven spending. "
                    f"Cost per MAU went from ${cpu_before:.4f} to ${cpu_now:.4f}."
                )

    # ── MRR efficiency ────────────────────────────────────────────────────────
    mrr_now_val = metrics_now.get("mrr_usd") or (metrics_now.get("arr_usd", 0) / 12 if metrics_now.get("arr_usd") else None)
    mrr_before_val = metrics_before.get("mrr_usd") or (metrics_before.get("arr_usd", 0) / 12 if metrics_before.get("arr_usd") else None)

    if mrr_now_val and mrr_before_val and mrr_before_val > 0:
        mrr_pct = (mrr_now_val - mrr_before_val) / mrr_before_val * 100
        pct_mrr_now = econ_now.get("hosting_pct_mrr")
        pct_mrr_before = econ_before.get("hosting_pct_mrr")
        if pct_mrr_now and pct_mrr_before:
            mrr_delta = pct_mrr_now - pct_mrr_before
            if mrr_pct > cost_pct if cost_pct else False:
                signals.append({"type": "positive", "metric": "hosting_pct_mrr",
                                 "label": f"Revenue growing faster than costs"})
                findings.append(
                    f"MRR grew {mrr_pct:.1f}% while hosting grew {pct_str}. "
                    f"Hosting as % of MRR improved from {pct_mrr_before:.1f}% to {pct_mrr_now:.1f}%. "
                    f"This is healthy scaling."
                )
            elif mrr_delta > 2:
                signals.append({"type": "negative", "metric": "hosting_pct_mrr",
                                 "label": f"Hosting rising faster than revenue"})
                findings.append(
                    f"Hosting as % of MRR worsened from {pct_mrr_before:.1f}% to {pct_mrr_now:.1f}%. "
                    f"Costs are growing faster than revenue. "
                    f"{econ_now.get('hosting_pct_mrr_note', '')}"
                )

    # ── Customer efficiency ───────────────────────────────────────────────────
    cust_now = metrics_now.get("paying_customers")
    cust_before = metrics_before.get("paying_customers")
    if cust_now and cust_before and cust_before > 0:
        cust_pct = (cust_now - cust_before) / cust_before * 100
        cpp_now = econ_now.get("cost_per_customer_usd")
        cpp_before = econ_before.get("cost_per_customer_usd")
        if cpp_now and cpp_before and cust_pct > 2:
            cpp_change = (cpp_now - cpp_before) / cpp_before * 100
            signals.append({
                "type": "positive" if cpp_change <= 0 else "watch",
                "metric": "cost_per_customer",
                "label": f"Cost per customer {'improved' if cpp_change <= 0 else 'increased'} {abs(cpp_change):.1f}%"
            })
            findings.append(
                f"Paying customers grew {cust_pct:.1f}%. "
                f"Cost per customer {'dropped' if cpp_change <= 0 else 'rose'} "
                f"from ${cpp_before:.2f} to ${cpp_now:.2f}."
            )

    # ── API call efficiency ───────────────────────────────────────────────────
    api_now = metrics_now.get("api_calls_monthly")
    api_before = metrics_before.get("api_calls_monthly")
    if api_now and api_before and api_before > 0:
        api_pct = (api_now - api_before) / api_before * 100
        cpa_now = econ_now.get("cost_per_1k_api_calls_usd")
        cpa_before = econ_before.get("cost_per_1k_api_calls_usd")
        if cpa_now and cpa_before:
            cpa_delta = (cpa_now - cpa_before) / cpa_before * 100
            signals.append({
                "type": "positive" if cpa_delta <= 0 else "watch",
                "metric": "cost_per_api_call",
                "label": f"Cost per 1k API calls {'improved' if cpa_delta <= 0 else 'increased'} {abs(cpa_delta):.1f}%"
            })
            findings.append(
                f"API calls {'grew' if api_pct > 0 else 'dropped'} {abs(api_pct):.1f}%. "
                f"Cost per 1k API calls went from ${cpa_before:.4f} to ${cpa_now:.4f}."
            )

    # ── Fallback if no business metrics available ─────────────────────────────
    if not findings:
        if cost_delta > 0:
            findings.append(
                f"Costs went up ${abs_delta:,.2f} ({pct_str}). "
                f"Set business metrics (MRR, MAU, paying customers) with set_business_metrics() "
                f"to understand whether this increase is growth-driven or pure inflation."
            )
        else:
            findings.append(
                f"Costs went down ${abs_delta:,.2f} ({pct_str}). "
                f"Set business metrics to understand the efficiency impact."
            )

    # ── Overall verdict ───────────────────────────────────────────────────────
    positive_count = sum(1 for s in signals if s["type"] == "positive")
    negative_count = sum(1 for s in signals if s["type"] == "negative")

    if positive_count > negative_count:
        verdict = "healthy"
        verdict_note = "Cost change looks growth-driven and efficient."
    elif negative_count > positive_count:
        verdict = "investigate"
        verdict_note = "Cost is rising faster than business metrics. This needs attention."
    elif cost_delta > 0:
        verdict = "watch"
        verdict_note = "Costs are up. Monitor business metrics to confirm this is growth-driven."
    else:
        verdict = "healthy"
        verdict_note = "Costs dropped. Confirm this is intentional and not a missing resource."

    return {
        "period": period_label,
        "cost_change": {
            "before": f"${cost_before:,.2f}",
            "now": f"${cost_now:,.2f}",
            "delta": f"${cost_delta:+,.2f}",
            "pct": f"{cost_pct:+.1f}%" if cost_pct is not None else "n/a",
        },
        "verdict": verdict,
        "verdict_note": verdict_note,
        "signals": signals,
        "findings": findings,
        "unit_economics_now": econ_now,
        "unit_economics_before": econ_before,
    }
