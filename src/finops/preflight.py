"""Cost preflight: turn a proposed change's monthly cost delta into a machine
verdict against an optional budget.

This is the agent-native on-ramp. An agent calls the `estimate_change_cost` MCP
tool BEFORE applying an infra change; this module is the pure verdict logic it
runs once the cost delta and the budget are known. Read-only by construction: it
computes, it never acts. It is also the seed of the policy-bounded guardrail
(verdict + headroom is exactly what a request-path gate returns); that later
layer adds human policy on top of this.
"""
from __future__ import annotations

from typing import Any

# Verdicts an agent can branch on.
OK = "ok"
WARN = "warn"
OVER_BUDGET = "over_budget"
NO_BUDGET = "no_budget"

_DEFAULT_ALERT_PCT = 80.0


def evaluate_preflight(
    monthly_delta_usd: float,
    *,
    budget: dict[str, Any] | None = None,
    alert_pct: float = _DEFAULT_ALERT_PCT,
) -> dict[str, Any]:
    """Verdict for a proposed cost change against an optional budget.

    monthly_delta_usd: positive = the change increases monthly cost, negative = saving.
    budget: None, or {"name", "limit_usd", "run_rate_usd"} where run_rate_usd is the
            current monthly run-rate the change would add to.
    alert_pct: the percent-of-limit at which a change is a WARN rather than OK.

    Returns an agent-friendly dict: verdict, monthly/annual delta, budget headroom,
    and a one-line reason. Never raises on normal numeric input.
    """
    monthly = round(float(monthly_delta_usd), 2)
    annual = round(monthly * 12, 2)
    out: dict[str, Any] = {
        "monthly_delta_usd": monthly,
        "annual_delta_usd": annual,
    }

    limit = float(budget["limit_usd"]) if (budget and budget.get("limit_usd")) else 0.0
    if not budget or limit <= 0:
        out["verdict"] = NO_BUDGET
        out["budget"] = None
        if monthly > 0:
            out["reason"] = (f"Change adds ${monthly:,.0f}/mo (${annual:,.0f}/yr). "
                             "No budget configured to check it against.")
        elif monthly < 0:
            out["reason"] = (f"Change saves ${abs(monthly):,.0f}/mo "
                             f"(${abs(annual):,.0f}/yr). No budget configured.")
        else:
            out["reason"] = "No cost-affecting change. No budget configured."
        return out

    run_rate = float(budget.get("run_rate_usd", 0.0) or 0.0)
    projected = run_rate + monthly
    raw_pct = (projected / limit * 100) if limit else 0.0  # verdict uses the true value
    projected_pct = round(raw_pct, 1)                      # rounded only for display
    headroom = round(limit - projected, 2)
    name = budget.get("name") or "budget"

    if monthly <= 0:
        verdict = OK  # a saving or no-op never breaches a budget
    elif raw_pct >= 100:
        verdict = OVER_BUDGET
    elif raw_pct >= alert_pct:
        verdict = WARN
    else:
        verdict = OK

    out["verdict"] = verdict
    out["budget"] = {
        "name": name,
        "limit_usd": round(limit, 2),
        "current_run_rate_usd": round(run_rate, 2),
        "projected_run_rate_usd": round(projected, 2),
        "projected_pct_of_limit": projected_pct,
        "headroom_usd": headroom,
    }

    verb = "adds" if monthly > 0 else "saves" if monthly < 0 else "is"
    amt = abs(monthly)
    if verdict == OVER_BUDGET:
        out["reason"] = (f"Change {verb} ${amt:,.0f}/mo and pushes '{name}' to "
                         f"{projected_pct:.0f}% of its ${limit:,.0f} limit "
                         f"(${abs(headroom):,.0f} over).")
    elif verdict == WARN:
        out["reason"] = (f"Change {verb} ${amt:,.0f}/mo, bringing '{name}' to "
                         f"{projected_pct:.0f}% of its ${limit:,.0f} limit "
                         f"(${headroom:,.0f} headroom left).")
    elif monthly < 0:
        out["reason"] = (f"Change saves ${amt:,.0f}/mo. '{name}' projected at "
                         f"{projected_pct:.0f}% of its ${limit:,.0f} limit.")
    else:
        out["reason"] = (f"Change {verb} ${amt:,.0f}/mo. '{name}' projected at "
                         f"{projected_pct:.0f}% of its ${limit:,.0f} limit "
                         f"(${headroom:,.0f} headroom).")
    return out
