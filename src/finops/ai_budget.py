"""
Local budget for your AI coding agent.

nable's cost tools point at your cloud. This one points at the agent itself: the
tokens Claude Code / Cursor burn against your Claude or Cursor plan, and the dollars
a metered API key spends. It answers one question before you (or the agent) kick off
a big task: "am I about to blow my budget?"

Two honest data sources, no guessing:

  1. Local usage meter (subscription plans). Claude Code writes every message's real
     token usage to ~/.claude/projects/**/*.jsonl. We tally it over a rolling window
     and month-to-date. This is exact token counts, read locally, nothing leaves the
     machine. What we deliberately do NOT do: claim a percentage of Anthropic's Max
     rate-limit. That number is not exposed by any API, so we report real burn rate
     against YOUR budget instead of a fabricated "% of plan left".

  2. Metered API spend (pay-per-token keys). get_all_llm_costs gives real provider
     dollars month-to-date. Precise budgeting for OpenAI/Anthropic/Bedrock API keys.

The gate, `check(...)`, mirrors policy.py: ok / warn / over, advice only. It never
stops the agent; it tells you where you stand so you decide.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import token_budget

# ── Verdicts (mirror policy.py's vocabulary) ─────────────────────────────────
BUDGET_OK = "ok"        # comfortably under budget
BUDGET_WARN = "warn"    # crossed the warn threshold (default 80%)
BUDGET_OVER = "over"    # at or past the budget

_WARN_AT = float(os.getenv("FINOPS_AI_BUDGET_WARN_PCT", "0.80"))

# A rolling window for "right now" usage. Claude's heaviest plan gate is a ~5h
# window, so 5h is a sensible default to show burn against. Configurable.
_WINDOW_HOURS = float(os.getenv("FINOPS_AI_WINDOW_HOURS", "5"))

# Blended API-equivalent price so a subscription user sees a dollar figure they can
# reason about ("this session would be ~$18 on the API"). Not what the plan charges
# (that is flat); it is the metered-equivalent. Configurable per the model you run.
_USD_PER_MTOK_IN = float(os.getenv("FINOPS_AI_USD_PER_MTOK_IN", "3.0"))
_USD_PER_MTOK_OUT = float(os.getenv("FINOPS_AI_USD_PER_MTOK_OUT", "15.0"))
_USD_PER_MTOK_CACHE_WRITE = float(os.getenv("FINOPS_AI_USD_PER_MTOK_CACHE_WRITE", "3.75"))
_USD_PER_MTOK_CACHE_READ = float(os.getenv("FINOPS_AI_USD_PER_MTOK_CACHE_READ", "0.30"))

# Reference plans. `price` is the flat monthly fee (what you actually pay on a
# subscription); `kind` decides how the budget is framed. Never used to fake a
# remaining-rate-limit number.
_PLANS = {
    "claude-pro":    {"label": "Claude Pro ($20/mo)",      "price": 20.0,  "kind": "subscription"},
    "claude-max-5x": {"label": "Claude Max 5x ($100/mo)",  "price": 100.0, "kind": "subscription"},
    "claude-max-20x":{"label": "Claude Max 20x ($200/mo)", "price": 200.0, "kind": "subscription"},
    "cursor":        {"label": "Cursor",                   "price": 0.0,   "kind": "subscription"},
    "api":           {"label": "Metered API key",          "price": 0.0,   "kind": "api"},
}


def _data_dir() -> Path:
    d = Path(os.getenv("FINOPS_DATA_DIR") or (Path.home() / ".nable"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _budget_path() -> Path:
    return _data_dir() / "ai-budget.json"


# ── Budget config ────────────────────────────────────────────────────────────

def get_budget() -> dict[str, Any]:
    """The user's AI budget. Empty/zero fields mean 'not set'."""
    default = {"monthly_usd": 0.0, "monthly_tokens": 0, "plan": "", "set_at": 0.0}
    try:
        data = json.loads(_budget_path().read_text())
        if isinstance(data, dict):
            default.update({k: data[k] for k in default if k in data})
    except (OSError, ValueError):
        pass
    return default


def set_budget(monthly_usd: float | None = None, monthly_tokens: int | None = None,
               plan: str | None = None) -> dict[str, Any]:
    """Set the monthly AI budget. Pass a dollar cap, a token cap, a plan, or any mix."""
    b = get_budget()
    if monthly_usd is not None:
        b["monthly_usd"] = max(0.0, float(monthly_usd))
    if monthly_tokens is not None:
        b["monthly_tokens"] = max(0, int(monthly_tokens))
    if plan is not None:
        b["plan"] = plan if plan in _PLANS else ""
    b["set_at"] = time.time()
    fd = os.open(_budget_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(b, fh)
    return b


# ── Local usage meter (Claude Code session logs) ─────────────────────────────

def _claude_projects_dir() -> Path:
    base = os.getenv("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def _usd_equivalent(tin: int, tout: int, cwrite: int, cread: int) -> float:
    return round(
        tin / 1e6 * _USD_PER_MTOK_IN
        + tout / 1e6 * _USD_PER_MTOK_OUT
        + cwrite / 1e6 * _USD_PER_MTOK_CACHE_WRITE
        + cread / 1e6 * _USD_PER_MTOK_CACHE_READ,
        2,
    )


def read_agent_usage(since_epoch: float) -> dict[str, Any]:
    """Tally Claude Code token usage across all local sessions since `since_epoch`.

    Exact counts, read locally. Skips log files whose mtime predates the window so a
    long history stays cheap. Returns totals + a per-model split + first/last activity.
    """
    proj = _claude_projects_dir()
    tin = tout = cwrite = cread = msgs = 0
    by_model: dict[str, int] = {}
    first_ts: float | None = None
    last_ts: float | None = None

    if not proj.is_dir():
        return _usage_payload(tin, tout, cwrite, cread, msgs, by_model, first_ts, last_ts,
                              source_present=False)

    for path in proj.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < since_epoch - 1:
                continue  # whole file is older than the window
        except OSError:
            continue
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    ts = _rec_epoch(rec.get("timestamp"))
                    if ts is None or ts < since_epoch:
                        continue
                    msg = rec.get("message") or {}
                    usage = msg.get("usage") or {}
                    if not usage:
                        continue
                    ti = int(usage.get("input_tokens", 0) or 0)
                    to = int(usage.get("output_tokens", 0) or 0)
                    cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
                    cr = int(usage.get("cache_read_input_tokens", 0) or 0)
                    if ti == to == cw == cr == 0:
                        continue
                    tin += ti; tout += to; cwrite += cw; cread += cr; msgs += 1
                    model = str(msg.get("model", "") or "unknown")
                    by_model[model] = by_model.get(model, 0) + ti + to + cw + cr
                    first_ts = ts if first_ts is None else min(first_ts, ts)
                    last_ts = ts if last_ts is None else max(last_ts, ts)
        except OSError:
            continue

    return _usage_payload(tin, tout, cwrite, cread, msgs, by_model, first_ts, last_ts,
                          source_present=True)


def _usage_payload(tin, tout, cwrite, cread, msgs, by_model, first_ts, last_ts,
                   source_present):
    # "Billable" = the tokens that represent real new work and cost: input, output,
    # and cache creation. cache_read is Claude Code re-reading its own cached context
    # every turn; it is cheap and would otherwise dwarf every other number, so it is
    # reported separately and NOT the headline the budget measures against.
    billable = tin + tout + cwrite
    return {
        "input_tokens": tin, "output_tokens": tout,
        "cache_creation_tokens": cwrite, "cache_read_tokens": cread,
        "billable_tokens": billable, "total_tokens": billable + cread,
        "messages": msgs,
        "usd_equivalent": _usd_equivalent(tin, tout, cwrite, cread),
        "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1])),
        "first_activity": first_ts, "last_activity": last_ts,
        "source_present": source_present,
    }


def _rec_epoch(ts: Any) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _month_start_epoch() -> float:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


# ── Status + gate ────────────────────────────────────────────────────────────

def status() -> dict[str, Any]:
    """Where you stand. Honest by construction: tokens and burn rate are exact
    (read from local logs). Dollars are ONLY ever an estimate at list price, never
    presented as your actual bill (nable's provider $ can be far off the real
    invoice for subscription/credit usage). A flat subscription's fee is what you
    pay, so its verdict is driven by usage, not by an estimated-dollar overage."""
    now = time.time()
    window = read_agent_usage(now - _WINDOW_HOURS * 3600)
    mtd = read_agent_usage(_month_start_epoch())
    budget = get_budget()
    plan = _PLANS.get(budget["plan"], {})
    plan_kind = plan.get("kind", "unknown")

    tokens_mtd = mtd["billable_tokens"]        # exact
    est_usd_mtd = mtd["usd_equivalent"]        # ESTIMATE at list price, not a bill

    # Verdict only off things we measure honestly: a token budget (exact), or a
    # dollar budget on a metered API plan (labeled estimate). Never off a list-price
    # estimate for a flat subscription, where the fee is what you actually pay.
    verdict, pct, basis = BUDGET_OK, None, "none"
    if budget["monthly_tokens"] > 0:
        pct, basis = tokens_mtd / budget["monthly_tokens"], "tokens"
    elif plan_kind == "api" and budget["monthly_usd"] > 0:
        pct, basis = est_usd_mtd / budget["monthly_usd"], "usd_estimate"
    if pct is not None:
        verdict = BUDGET_OVER if pct >= 1.0 else (BUDGET_WARN if pct >= _WARN_AT else BUDGET_OK)

    burn = round(window["billable_tokens"] / max(_WINDOW_HOURS, 0.1))

    subsidy = None
    if plan_kind == "subscription" and plan.get("price", 0) > 0:
        subsidy = {
            "plan_price_usd": plan["price"],
            "compute_value_est_usd": est_usd_mtd,
            "multiple": round(est_usd_mtd / plan["price"], 1) if plan["price"] else None,
        }

    return {
        "verdict": verdict,
        "verdict_basis": basis,
        "window_hours": _WINDOW_HOURS,
        "window": window,
        "month_to_date": mtd,
        "billable_tokens_mtd": tokens_mtd,
        "est_usd_mtd_list_price": est_usd_mtd,
        "budget": budget,
        "plan_kind": plan_kind,
        "plan_label": plan.get("label", ""),
        "pct_of_budget": round(pct, 3) if pct is not None else None,
        "burn_tokens_per_hour": burn,
        "subsidy": subsidy,
        "summary": _summary_line(verdict, basis, tokens_mtd, est_usd_mtd, budget,
                                 plan_kind, subsidy, window),
    }


def _summary_line(verdict, basis, tokens_mtd, est_usd, budget, plan_kind, subsidy, window) -> str:
    tag = {BUDGET_OK: "on track", BUDGET_WARN: "approaching your budget",
           BUDGET_OVER: "over budget"}[verdict]
    if basis == "tokens":
        return f"{tokens_mtd:,} of {budget['monthly_tokens']:,} tokens this month, {tag}."
    if basis == "usd_estimate":
        return (f"~${est_usd:,.0f} estimated at list price of your "
                f"${budget['monthly_usd']:,.0f} metered budget, {tag}.")
    if subsidy and subsidy["multiple"]:
        return (f"You pay ${subsidy['plan_price_usd']:,.0f}/mo and have pulled "
                f"~${est_usd:,.0f} of compute this month (estimated at list price), "
                f"~{subsidy['multiple']:g}x your plan. The provider covers the rest.")
    return (f"{window['billable_tokens']:,} tokens in the last {_WINDOW_HOURS:g}h "
            f"(~${window['usd_equivalent']:,.0f} at list price). Set a budget with "
            f"`finops ai-budget --tokens N` or `--monthly N`.")


def check(estimated_next_tokens: int = 0) -> dict[str, Any]:
    """The gate the agent calls before a big task. Advice only, never blocks.

    On a metered API plan it is about dollars (estimated). On a flat subscription it
    is about usage and not getting rate-limited, so it speaks in tokens and burn
    rate, never a fake dollar overage. Mirrors policy.py: a verdict and a reason."""
    st = status()
    verdict, reason = st["verdict"], st["summary"]

    # A token budget is exact, so a next-task estimate can honestly tip it.
    if estimated_next_tokens and st["budget"]["monthly_tokens"] > 0:
        after = st["billable_tokens_mtd"] + estimated_next_tokens
        pct_after = after / st["budget"]["monthly_tokens"]
        if pct_after >= 1.0 and verdict != BUDGET_OVER:
            verdict = BUDGET_WARN
            reason = (f"this task (~{estimated_next_tokens:,} tokens) would push you to "
                      f"{pct_after * 100:.0f}% of your monthly token budget.")

    if st["plan_kind"] == "subscription":
        rec = {
            BUDGET_OK: f"Proceed. Flat plan, so this is about pace, not a bill: ~{st['burn_tokens_per_hour']:,} tok/hr.",
            BUDGET_WARN: "Proceed, but you are near the usage budget you set for the month.",
            BUDGET_OVER: "You are past the usage budget you set. Confirm with the human first.",
        }[verdict]
    else:
        rec = {
            BUDGET_OK: "Proceed.",
            BUDGET_WARN: "Proceed, but you are close to your budget. Consider a tighter scope.",
            BUDGET_OVER: "You are over your AI budget. Confirm with the human before continuing.",
        }[verdict]

    return {
        "verdict": verdict,
        "reason": reason,
        "recommendation": rec,
        "advice_only": True,
        "plan_kind": st["plan_kind"],
        "billable_tokens_mtd": st["billable_tokens_mtd"],
        "est_usd_mtd_list_price": st["est_usd_mtd_list_price"],
        "burn_tokens_per_hour": st["burn_tokens_per_hour"],
    }
