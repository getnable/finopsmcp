"""`finops ai-budget` — set and check a local budget for your AI coding agent.

Bare `finops ai-budget` prints where you stand (this window, month to date, budget,
burn rate). Pass --monthly / --tokens / --plan to set the budget first. The numbers
come from finops.ai_budget: real local token usage from Claude Code's session logs
plus any metered API spend, nothing uploaded.
"""
from __future__ import annotations

import json
import sys
from typing import Any

_ACCENT = "\033[38;5;38m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_WARN = "\033[38;5;208m"
_OVER = "\033[38;5;203m"
_OK = "\033[38;5;71m"
_RST = "\033[0m"


def _c(s: str, color: str) -> str:
    return s if not sys.stdout.isatty() else f"{color}{s}{_RST}"


def _tok(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n/1e9:.1f}B"
    if n >= 1_000_000:
        return f"{n/1e6:.1f}M"
    if n >= 1_000:
        return f"{n/1e3:.0f}K"
    return str(n)


def add_parser(sub) -> None:
    p = sub.add_parser(
        "ai-budget",
        help="Set and check a local budget for your AI coding agent",
        description="A local budget for your coding agent's own token/dollar spend. "
                    "Reads Claude Code usage locally; nothing leaves your machine.",
    )
    p.add_argument("--monthly", type=float, metavar="USD",
                   help="Set a monthly dollar budget, e.g. --monthly 200")
    p.add_argument("--tokens", type=int, metavar="N",
                   help="Set a monthly billable-token budget")
    p.add_argument("--plan", metavar="PLAN",
                   help="Label your plan: claude-pro, claude-max-5x, claude-max-20x, cursor, api")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p.set_defaults(cmd="ai-budget")


def run(args) -> int:
    from . import ai_budget as ab

    if args.monthly is not None or args.tokens is not None or args.plan is not None:
        ab.set_budget(monthly_usd=args.monthly, monthly_tokens=args.tokens, plan=args.plan)

    st = ab.status()
    if getattr(args, "json", False):
        print(json.dumps(st, indent=2, default=str))
        return 0

    out = sys.stdout
    b = st["budget"]
    w = st["window"]
    verdict = st["verdict"]
    vcolor = {"ok": _OK, "warn": _WARN, "over": _OVER}[verdict]

    plan = st["plan_label"] or "no plan set"
    print(_c("nable ai-budget", _BOLD) + _c(f"  ·  {plan}", _DIM), file=out)
    if not w["source_present"]:
        print(_c("  no Claude Code usage found yet (looked in ~/.claude/projects).", _DIM), file=out)

    def row(label: str, value: str) -> None:
        print(f"  {_c(label.ljust(16), _DIM)}{value}", file=out)

    print(file=out)
    row("this 5h window", f"{_tok(w['billable_tokens'])} tokens · {w['messages']} msgs · "
                          f"~{_c('$'+format(w['usd_equivalent'], ',.0f'), _ACCENT)} at list price (est.)")
    row("month to date", f"{_tok(st['billable_tokens_mtd'])} tokens · "
                         f"~${st['est_usd_mtd_list_price']:,.0f} at list price (est.)")

    sub = st["subsidy"]
    if b["monthly_tokens"] > 0:
        pct = (st["pct_of_budget"] or 0) * 100
        row("budget", f"{_tok(st['billable_tokens_mtd'])} of {_tok(b['monthly_tokens'])} tokens  ·  "
                      f"{_c(verdict.upper(), vcolor)} ({pct:.0f}%)")
    elif st["plan_kind"] == "api" and b["monthly_usd"] > 0:
        pct = (st["pct_of_budget"] or 0) * 100
        row("budget", f"~${st['est_usd_mtd_list_price']:,.0f} est of ${b['monthly_usd']:,.0f} metered  ·  "
                      f"{_c(verdict.upper(), vcolor)} ({pct:.0f}%)")
    elif sub and sub["multiple"]:
        row("your plan", f"${sub['plan_price_usd']:,.0f}/mo flat  ·  "
                         f"~{sub['multiple']:g}x value pulled {_c('(subsidized)', _OK)}")
    else:
        row("budget", _c("not set — `finops ai-budget --tokens 500000000` or `--monthly 200`", _DIM))
    row("burn rate", f"~{_tok(st['burn_tokens_per_hour'])} tokens/hour")

    print(file=out)
    print("  " + _c(st["summary"], vcolor), file=out)
    print(_c("  local · exact token counts · dollars are list-price estimates, not your bill", _DIM), file=out)
    return 0
