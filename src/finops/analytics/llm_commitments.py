"""
LLM commitment & contract intelligence — the token side of Reserved Instances.

Enterprises and funded startups rarely pay list price for tokens. They hold
committed capacity in one of a few shapes:

  - prepaid credits        (AWS Activate, Azure/GCP startup credits, vendor credits)
  - Azure OpenAI PTUs      (Provisioned Throughput Units: reserved throughput, billed hourly)
  - Bedrock Provisioned    (AWS reserved "model units", 1 or 6 month commitment)
  - enterprise rate cards  (negotiated $/Mtok, often with a minimum spend commit)

Every one of these reduces to the same question Reserved-Instance / Savings-Plan
analysis answers for cloud: are you covered, are you using the capacity you
committed to, what is your effective rate versus on-demand, and how much runway
is left. This module computes that uniformly across contract types, against the
customer's ACTUAL terms, not list price. That last part is the moat: a generic
dashboard prices you at list; nable prices you at your contract.

Pure and testable on purpose. The analysis functions take an observed-usage dict
and a contract spec and return a normalized verdict. The MCP tool wires real
usage in via connectors.llm_costs.get_all_llm_costs and credit_tracking.

Contract spec (a plain dict; see EXAMPLE_CONTRACTS for templates):

  credits:
    {"type": "credits", "label": str, "provider": str,
     "balance_usd": float|None,        # stated remaining balance, if known
     "monthly_burn_usd": float|None}   # else inferred from credit_tracking

  capacity (azure_ptu | bedrock_provisioned):
    {"type": "azure_ptu", "label": str, "provider": "azure",
     "units": float,                   # PTUs / model units committed
     "unit_throughput_tpm": float,     # tokens-per-minute per unit (capacity)
     "unit_rate_usd_hr": float,        # $/hour per unit (your committed rate)
     "term_months": int,
     "on_demand_rate_per_mtok": float} # what you'd pay PAYG for the same model

  rate_card:
    {"type": "rate_card", "label": str, "provider": str,
     "negotiated_rate_per_mtok": float,   # your blended negotiated $/Mtok
     "list_rate_per_mtok": float,         # public list for the same mix
     "minimum_spend_usd": float|None}     # contractual monthly minimum, if any
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_CAPACITY_TYPES = {"azure_ptu", "bedrock_provisioned"}
# Right-sizing targets a high-but-safe utilization band. Below this you are
# over-committed (on-demand would be cheaper for that volume); above the ceiling
# you are likely spilling into pay-as-you-go and should buy more capacity.
_UTIL_TARGET = 0.85
_UTIL_CEILING = 0.95


# ── token helpers ─────────────────────────────────────────────────────────────

def total_tokens(by_model_tokens: dict[str, dict[str, int]] | None) -> int:
    """Sum input + output + cache tokens across every model.

    Accepts the ``by_model_tokens`` shape from get_all_llm_costs:
    {model: {"input": int, "output": int, "cache_read": int, ...,
             "request_count": int}}. ``request_count`` is not a token count and
    is excluded.
    """
    if not by_model_tokens:
        return 0
    total = 0
    for tok in by_model_tokens.values():
        if not isinstance(tok, dict):
            continue
        for k, v in tok.items():
            if k == "request_count":
                continue
            try:
                total += int(v)
            except (TypeError, ValueError):
                continue
    return total


# ── per-contract analysis ─────────────────────────────────────────────────────

def analyze_commitment(contract: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
    """Analyze a single commitment against observed usage.

    ``usage`` carries the observed window:
      {"tokens": int, "spend_usd": float, "days": int,
       "credit_analysis": <analyze_credits output> | None}

    Returns a normalized verdict (see module docstring). Never raises on bad
    input; returns a ``no_data`` status instead.
    """
    ctype = (contract.get("type") or "").strip().lower()
    label = contract.get("label") or ctype or "commitment"
    provider = contract.get("provider") or ""

    base = {"label": label, "type": ctype, "provider": provider}

    if ctype == "credits":
        return {**base, **_analyze_credits_contract(contract, usage)}
    if ctype in _CAPACITY_TYPES:
        return {**base, **_analyze_capacity_contract(contract, usage)}
    if ctype == "rate_card":
        return {**base, **_analyze_rate_card(contract, usage)}

    return {**base, "status": "no_data",
            "headline": f"Unknown commitment type '{ctype}'.",
            "note": "Supported types: credits, azure_ptu, bedrock_provisioned, rate_card."}


def _analyze_credits_contract(contract: dict, usage: dict) -> dict[str, Any]:
    """Credits are committed capacity measured in dollars. Coverage and runway
    come from the observed RECORD_TYPE series (credit_tracking.analyze_credits)
    when available, or from a stated balance + burn rate."""
    ca = usage.get("credit_analysis") or {}
    balance = contract.get("balance_usd")
    burn = contract.get("monthly_burn_usd")

    # Prefer a stated balance + burn for a precise runway.
    months_to_zero = None
    if balance is not None and burn:
        try:
            months_to_zero = round(max(0.0, float(balance) / float(burn)), 1) if float(burn) > 0 else None
        except (TypeError, ValueError, ZeroDivisionError):
            months_to_zero = None
    elif ca:
        months_to_zero = ca.get("estimated_months_to_zero_credits")

    coverage_pct = ca.get("latest_credit_coverage_pct")
    cash_flip = bool(ca.get("cash_flip_detected"))
    trend = ca.get("credit_trend", "unknown")

    if cash_flip:
        status, headline = "expiring", (
            "Credits have flipped to cash. The committed balance no longer covers "
            "the bill; you are now paying real cash. This is the cliff.")
    elif months_to_zero is not None and months_to_zero <= 3:
        status, headline = "expiring", (
            f"Credits run out in about {months_to_zero} month(s) at the current burn. "
            f"Plan the cash transition now.")
    elif coverage_pct is not None and coverage_pct < 50:
        status, headline = "underutilized", (
            f"Credits cover only {coverage_pct:.0f}% of the bill; cash exposure is climbing.")
    elif balance is not None:
        status, headline = "ok", (
            f"${float(balance):,.0f} in credits, ~{months_to_zero} month(s) of runway "
            f"at the current burn." if months_to_zero else
            f"${float(balance):,.0f} in credits remaining.")
    else:
        status, headline = ("ok", "Credits are active; watch for the flip to cash.") if coverage_pct \
            else ("no_data", "No credit data. Connect AWS or state a balance to track runway.")

    return {
        "status": status,
        "headline": headline,
        "coverage_pct": coverage_pct,
        "utilization_pct": None,
        "effective_rate_per_mtok": None,
        "on_demand_rate_per_mtok": None,
        "savings_vs_on_demand_usd": None,
        "break_even_utilization_pct": None,
        "recommended_units": None,
        "runway": {
            "balance_usd": balance,
            "estimated_months_to_zero": months_to_zero,
            "credit_trend": trend,
        },
        "note": "Credits are dollar-denominated committed capacity. Runway is the "
                "stated balance over burn, or inferred from observed credit consumption.",
    }


def _analyze_capacity_contract(contract: dict, usage: dict) -> dict[str, Any]:
    """PTUs and Bedrock Provisioned Throughput: reserved throughput billed by the
    hour. The question is utilization (are you using the throughput you bought)
    and effective rate versus on-demand for the volume you actually served."""
    try:
        units = float(contract["units"])
        tpm = float(contract["unit_throughput_tpm"])
        rate_hr = float(contract["unit_rate_usd_hr"])
        on_demand = float(contract["on_demand_rate_per_mtok"])
        days = float(usage.get("days") or 30)
        used_tokens = float(usage.get("tokens") or 0)
    except (KeyError, TypeError, ValueError):
        return {"status": "no_data",
                "headline": "Capacity contract is missing units / throughput / rate, "
                            "or no token usage was observed.",
                "note": "Need units, unit_throughput_tpm, unit_rate_usd_hr, "
                        "on_demand_rate_per_mtok, and observed token usage."}

    hours = days * 24.0
    minutes = days * 24.0 * 60.0
    committed_cost = units * rate_hr * hours
    capacity_tokens = units * tpm * minutes
    if capacity_tokens <= 0 or used_tokens <= 0 or on_demand <= 0:
        return {"status": "no_data",
                "headline": "Not enough signal to assess this capacity commitment.",
                "note": "Zero capacity, usage, or on-demand rate."}

    utilization = used_tokens / capacity_tokens
    effective_rate = committed_cost / (used_tokens / 1e6)        # $/Mtok actually paid
    on_demand_cost = (used_tokens / 1e6) * on_demand
    savings = on_demand_cost - committed_cost                     # >0 => commit wins
    savings_pct = (savings / on_demand_cost * 100) if on_demand_cost > 0 else 0.0
    # Break-even: the utilization at which the fixed committed cost equals what the
    # served volume would cost on-demand.
    break_even = committed_cost / ((capacity_tokens / 1e6) * on_demand)
    recommended_units = round(units * (utilization / _UTIL_TARGET), 2)

    if utilization < break_even:
        status = "underutilized"
        headline = (
            f"{label_of(contract)} is {utilization*100:.0f}% utilized, below the "
            f"{break_even*100:.0f}% break-even. You are paying ${effective_rate:,.2f}/Mtok "
            f"versus ${on_demand:,.2f} on-demand. Right-size to ~{recommended_units} unit(s) "
            f"or move this volume to pay-as-you-go.")
    elif utilization > _UTIL_CEILING:
        status = "oversubscribed"
        headline = (
            f"{label_of(contract)} is {utilization*100:.0f}% utilized and likely spilling "
            f"into pay-as-you-go. Effective rate ${effective_rate:,.2f}/Mtok beats on-demand; "
            f"adding capacity could cover the overflow at the committed rate.")
    else:
        status = "ok"
        headline = (
            f"{label_of(contract)} is {utilization*100:.0f}% utilized at ${effective_rate:,.2f}/Mtok, "
            f"saving ${savings:,.0f} ({savings_pct:.0f}%) versus on-demand this window.")

    return {
        "status": status,
        "headline": headline,
        "coverage_pct": None,
        "utilization_pct": round(utilization * 100, 1),
        "effective_rate_per_mtok": round(effective_rate, 2),
        "on_demand_rate_per_mtok": round(on_demand, 2),
        "savings_vs_on_demand_usd": round(savings, 2),
        "savings_vs_on_demand_pct": round(savings_pct, 1),
        "break_even_utilization_pct": round(break_even * 100, 1),
        "recommended_units": recommended_units,
        "runway": None,
        "detail": {
            "committed_cost_usd": round(committed_cost, 2),
            "capacity_tokens": int(capacity_tokens),
            "used_tokens": int(used_tokens),
            "window_days": round(days, 1),
        },
        "note": "Utilization is served tokens over the throughput you reserved. Below "
                "break-even, on-demand is cheaper for that volume; above the ceiling you "
                "are likely paying pay-as-you-go for the overflow.",
    }


def _analyze_rate_card(contract: dict, usage: dict) -> dict[str, Any]:
    """Negotiated enterprise rate card: a discounted $/Mtok, sometimes with a
    monthly minimum spend commit. The questions are the realized discount versus
    list, and whether you are clearing the minimum (or paying for unused commit)."""
    try:
        negotiated = float(contract["negotiated_rate_per_mtok"])
        list_rate = float(contract["list_rate_per_mtok"])
    except (KeyError, TypeError, ValueError):
        return {"status": "no_data",
                "headline": "Rate card is missing negotiated or list rate.",
                "note": "Need negotiated_rate_per_mtok and list_rate_per_mtok."}

    tokens = float(usage.get("tokens") or 0)
    spend = float(usage.get("spend_usd") or 0.0)
    minimum = contract.get("minimum_spend_usd")

    discount_pct = (1.0 - negotiated / list_rate) * 100 if list_rate > 0 else 0.0
    # Savings versus list for the observed volume.
    list_cost = (tokens / 1e6) * list_rate if tokens > 0 else 0.0
    negotiated_cost = (tokens / 1e6) * negotiated if tokens > 0 else spend
    savings = list_cost - negotiated_cost

    status = "ok"
    headline = (
        f"{label_of(contract)}: ${negotiated:,.2f}/Mtok negotiated versus ${list_rate:,.2f} list, "
        f"a {discount_pct:.0f}% discount, ${savings:,.0f} saved this window.")

    shortfall = None
    if minimum is not None:
        try:
            minimum = float(minimum)
            if spend < minimum:
                shortfall = round(minimum - spend, 2)
                status = "minimum_shortfall"
                headline = (
                    f"{label_of(contract)}: spend ${spend:,.0f} is below the ${minimum:,.0f} "
                    f"monthly minimum. You are paying ${shortfall:,.0f} for committed volume "
                    f"you did not use. Increase routing through this contract or renegotiate the floor.")
        except (TypeError, ValueError):
            minimum = None

    return {
        "status": status,
        "headline": headline,
        "coverage_pct": None,
        "utilization_pct": None,
        "effective_rate_per_mtok": round(negotiated, 2),
        "on_demand_rate_per_mtok": round(list_rate, 2),
        "savings_vs_on_demand_usd": round(savings, 2),
        "savings_vs_on_demand_pct": round(discount_pct, 1),
        "break_even_utilization_pct": None,
        "recommended_units": None,
        "runway": None,
        "detail": {
            "observed_spend_usd": round(spend, 2),
            "minimum_spend_usd": minimum,
            "minimum_shortfall_usd": shortfall,
            "discount_vs_list_pct": round(discount_pct, 1),
        },
        "note": "Discount is negotiated versus list rate. A shortfall means you paid for "
                "minimum committed volume you did not consume.",
    }


def label_of(contract: dict) -> str:
    return contract.get("label") or contract.get("type") or "commitment"


# ── portfolio + commit recommendation ─────────────────────────────────────────

def analyze_portfolio(contracts: list[dict], usage: dict[str, Any]) -> dict[str, Any]:
    """Analyze every contract and roll up the portfolio: total realized savings,
    total committed waste (under-utilized capacity + minimum shortfalls), and the
    contracts that need attention."""
    results = [analyze_commitment(c, usage) for c in (contracts or [])]

    total_savings = 0.0
    total_waste = 0.0
    needs_attention: list[dict] = []
    for r in results:
        s = r.get("savings_vs_on_demand_usd")
        if isinstance(s, (int, float)) and s > 0:
            total_savings += s
        # Waste = paying above on-demand on an under-utilized capacity commit, or a
        # rate-card minimum shortfall.
        if r.get("status") == "underutilized" and isinstance(s, (int, float)) and s < 0:
            total_waste += -s
        sf = (r.get("detail") or {}).get("minimum_shortfall_usd")
        if isinstance(sf, (int, float)) and sf > 0:
            total_waste += sf
        if r.get("status") in ("underutilized", "oversubscribed", "expiring", "minimum_shortfall"):
            needs_attention.append({"label": r["label"], "status": r["status"],
                                    "headline": r["headline"]})

    return {
        "contract_count": len(results),
        "total_realized_savings_usd": round(total_savings, 2),
        "total_committed_waste_usd": round(total_waste, 2),
        "needs_attention": needs_attention,
        "contracts": results,
    }


def recommend_commitment(daily_spend: list[float], on_demand_monthly_usd: float) -> dict[str, Any]:
    """For an account with NO commitment yet: should they buy one?

    The signal is the same as cloud RIs: high, stable spend is the case for a
    commitment; spiky or small spend is not. This is the on-ramp, the tool stays
    useful before any contract exists (which is most early customers).
    """
    if on_demand_monthly_usd < 200:
        return {"recommend": False,
                "headline": "Token spend is small. Stay on pay-as-you-go; a commitment would "
                            "lock up cash for little benefit.",
                "monthly_on_demand_usd": round(on_demand_monthly_usd, 2)}

    # Stability: coefficient of variation on the daily series. Low CoV => predictable
    # baseline that committed capacity can absorb at a discount.
    import statistics
    series = [x for x in (daily_spend or []) if x is not None and x >= 0]
    cov = None
    if len(series) >= 7:
        mean = statistics.mean(series)
        if mean > 0:
            cov = statistics.pstdev(series) / mean

    stable = cov is not None and cov < 0.4
    # Provisioned/PTU discounts on committed throughput are typically ~20-40% vs
    # on-demand when well utilized. Quote a conservative, clearly-labeled estimate.
    est_savings_lo = round(on_demand_monthly_usd * 0.15, 0)
    est_savings_hi = round(on_demand_monthly_usd * 0.35, 0)

    if stable:
        headline = (
            f"${on_demand_monthly_usd:,.0f}/mo of token spend with a stable daily baseline. "
            f"Provisioned capacity (Azure PTU / Bedrock Provisioned Throughput) or an enterprise "
            f"rate card would likely cut ${est_savings_lo:,.0f}-${est_savings_hi:,.0f}/mo. "
            f"Right-size to the baseline, keep spikes on pay-as-you-go.")
        recommend = True
    else:
        headline = (
            f"${on_demand_monthly_usd:,.0f}/mo of token spend, but the daily pattern is spiky"
            f"{f' (variation {cov:.0%})' if cov is not None else ''}. Commit only to the stable "
            f"floor of usage; a full commitment risks paying for idle capacity.")
        recommend = "partial"

    return {
        "recommend": recommend,
        "headline": headline,
        "monthly_on_demand_usd": round(on_demand_monthly_usd, 2),
        "daily_variation_cov": round(cov, 3) if cov is not None else None,
        "estimated_savings_range_usd": [est_savings_lo, est_savings_hi],
        "note": "Savings range is a conservative 15-35% estimate for well-utilized committed "
                "capacity; the realized figure depends on your negotiated rate and utilization.",
    }


# ── contract loading ──────────────────────────────────────────────────────────

def load_contracts() -> list[dict]:
    """Load the customer's commitment specs.

    Priority:
      1. FINOPS_AI_CONTRACTS env var (a JSON array of contract dicts)
      2. $FINOPS_HOME/ai_contracts.json (default ~/.finops-mcp/ai_contracts.json)

    Returns [] if none are configured. Token contract terms are not secrets, but
    they live outside the repo so a customer's negotiated rates stay on their
    machine, consistent with nable's local-first model.
    """
    raw = os.environ.get("FINOPS_AI_CONTRACTS")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [c for c in data if isinstance(c, dict)]
        except (json.JSONDecodeError, TypeError):
            log.warning("FINOPS_AI_CONTRACTS is not valid JSON; ignoring.")

    base = Path(os.environ.get("FINOPS_HOME", str(Path.home() / ".finops-mcp")))
    path = base / "ai_contracts.json"
    try:
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, list):
                return [c for c in data if isinstance(c, dict)]
    except (json.JSONDecodeError, OSError):
        log.warning("Could not read %s; ignoring.", path)
    return []


EXAMPLE_CONTRACTS = [
    {"type": "credits", "label": "AWS Activate", "provider": "aws",
     "balance_usd": 100000, "monthly_burn_usd": 8000},
    {"type": "azure_ptu", "label": "Azure OpenAI PTU (gpt-4o)", "provider": "azure",
     "units": 50, "unit_throughput_tpm": 5500, "unit_rate_usd_hr": 1.0,
     "term_months": 1, "on_demand_rate_per_mtok": 5.0},
    {"type": "bedrock_provisioned", "label": "Bedrock PT (Claude Sonnet)", "provider": "aws",
     "units": 2, "unit_throughput_tpm": 130000, "unit_rate_usd_hr": 39.6,
     "term_months": 1, "on_demand_rate_per_mtok": 9.0},
    {"type": "rate_card", "label": "Anthropic Enterprise", "provider": "anthropic",
     "negotiated_rate_per_mtok": 6.0, "list_rate_per_mtok": 9.0,
     "minimum_spend_usd": 20000},
]
