"""
Token-spend forecasting and runway-to-exhaustion.

Wraps the per-account Forecaster (ml/forecasting.py) on the daily token-cost
series from connectors.llm_costs.get_all_llm_costs. The Forecaster degrades
gracefully by series length (naive < 7 days, linear < 14, Holt-Winters >= 14),
so this works on day one against Cost Explorer / usage history with no snapshot
setup.

LLM spend tends to grow, not just cycle weekly, so the headline outputs are the
projected next-30-day spend, the implied month-over-month growth, and, when a
credit balance or commitment is supplied, the date it runs out. That exhaustion
date is the thing finance actually wants and that no provider dashboard gives.

Pure given its inputs: callers pass the observed daily series (and optional
balance); the MCP tool fetches them from the connectors.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def _to_series(daily: list[Any]) -> list[float]:
    """Accept either get_all_llm_costs' daily shape [{date, total_usd}] or a raw
    list of floats, and return an oldest-first float series."""
    series: list[float] = []
    for d in (daily or []):
        if isinstance(d, dict):
            v = d.get("total_usd", d.get("amount", 0.0))
        else:
            v = d
        try:
            series.append(max(0.0, float(v)))
        except (TypeError, ValueError):
            continue
    return series


def forecast_token_spend(
    daily: list[Any],
    horizon_days: int = 90,
    balance_usd: float | None = None,
) -> dict[str, Any]:
    """Forecast token spend and, if a balance is given, the exhaustion date.

    Args:
      daily: daily token-cost history, oldest first (get_all_llm_costs["daily"]
             or a list of floats).
      horizon_days: how far forward to project.
      balance_usd: remaining credit / commitment balance to burn down. When set,
             the forecast walks cumulative projected spend to find the day it is
             exhausted.
    """
    series = _to_series(daily)
    if len(series) < 3:
        return {
            "status": "insufficient_history",
            "headline": "Not enough token-spend history to forecast. Need at least a "
                        "few days; accuracy improves past 14 days.",
            "days_of_history": len(series),
        }

    from ..ml.forecasting import Forecaster

    f = Forecaster(account_id="llm", service=None)
    f.fit(series)
    result = f.predict(horizon_days)

    point = result.point
    projected_30 = round(sum(point[:30]), 2)
    projected_window = round(sum(point[:horizon_days]), 2)

    # Implied month-over-month growth: projected next 30 days vs the trailing 30
    # actual. This is the number that tells a founder whether the AI bill is
    # accelerating, which the seasonal model alone does not surface.
    trailing_30 = sum(series[-30:]) if len(series) >= 30 else sum(series) / len(series) * 30
    growth_pct = None
    if trailing_30 > 0:
        growth_pct = round((projected_30 - trailing_30) / trailing_30 * 100, 1)

    out: dict[str, Any] = {
        "status": "ok",
        "method": result.method,
        "mape_pct": result.mape,
        "days_of_history": len(series),
        "projected_next_30d_usd": projected_30,
        f"projected_next_{horizon_days}d_usd": projected_window,
        "trailing_30d_usd": round(trailing_30, 2),
        "implied_mom_growth_pct": growth_pct,
        "daily_forecast": [
            {"date": d, "point": p, "lower": lo, "upper": hi}
            for d, p, lo, hi in zip(result.dates, point, result.lower, result.upper)
        ][:horizon_days],
    }

    headline = (
        f"Token spend is projected at ${projected_30:,.0f} next month"
        + (f", {growth_pct:+.0f}% versus the trailing month" if growth_pct is not None else "")
        + f" ({result.method} model, {result.mape:.0f}% in-sample error)."
    )

    if balance_usd is not None:
        runway = _exhaustion(point, result.dates, float(balance_usd))
        out["runway"] = runway
        if runway.get("exhausts_on"):
            headline += (
                f" At this rate, ${float(balance_usd):,.0f} of credits/commitment runs out "
                f"around {runway['exhausts_on']} ({runway['days_remaining']} days).")
        elif runway.get("status") == "beyond_horizon":
            headline += (
                f" ${float(balance_usd):,.0f} of credits/commitment lasts beyond the "
                f"{horizon_days}-day horizon at this rate.")

    out["headline"] = headline
    return out


def _exhaustion(point: list[float], dates: list[str], balance: float) -> dict[str, Any]:
    """Walk cumulative projected spend to find the day a balance is exhausted."""
    if balance <= 0:
        return {"status": "exhausted", "exhausts_on": None, "days_remaining": 0,
                "note": "Balance is already zero or negative."}
    cum = 0.0
    for i, (p, d) in enumerate(zip(point, dates)):
        cum += max(0.0, p)
        if cum >= balance:
            return {
                "status": "exhausts_within_horizon",
                "exhausts_on": d,
                "days_remaining": i + 1,
                "projected_spend_to_date_usd": round(cum, 2),
            }
    return {
        "status": "beyond_horizon",
        "exhausts_on": None,
        "days_remaining": None,
        "projected_spend_over_horizon_usd": round(sum(point), 2),
        "note": "Projected spend does not exhaust the balance within the forecast horizon.",
    }
