"""`nable budget`: cloud budgets from the terminal.

  nable budget [status]   every active budget against month-to-date spend
  nable budget refresh    the same, said as what it also does: rewrites the
                          spend summary the guard hook reads (budget/summary.py)

Both recompute from the local cost history, so both keep the guard's figure
current; `refresh` is the one to put on a schedule. Budgets themselves are set
with the set_budget tool or a budget.yml (sync_budgets_from_yaml).
"""
from __future__ import annotations

import json
import sys
from typing import Any


def add_parser(sub) -> None:
    p = sub.add_parser(
        "budget",
        help="Cloud budgets: status, refresh the guard's spend figure",
        description="Cloud budgets against month-to-date spend, from the local cost "
                    "history. Every run also rewrites the spend summary the guard "
                    "hook checks priced changes against.",
    )
    p.add_argument("budget_action", nargs="?", default="status",
                   choices=["status", "refresh"],
                   help="status (default) = where each budget stands; refresh = the "
                        "same, run to update the guard's spend figure")
    p.add_argument("--json", dest="budget_json", action="store_true",
                   help="Emit machine-readable JSON")
    p.set_defaults(cmd="budget")


def _line(b: dict[str, Any]) -> str:
    scope = "total" if b.get("scope_type") == "total" else f"{b['scope_type']} {b['scope_value']}"
    return (f"  [{b['status']}] {b['name']} ({scope}): ${b['spent']:,.0f} of "
            f"${b['limit']:,.0f} ({b['pct_used']:.0f}%), {b['period_start']} to {b['period_end']}")


def _status(as_json: bool, *, refresh: bool) -> int:
    from . import enforcer
    from .summary import freshness, read_summary, summary_path
    try:
        results = enforcer.check_all_budgets()
    except Exception as e:  # noqa: BLE001 - reported to the user, exit 1
        if as_json:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"Could not check budgets: {e}", file=sys.stderr)
        return 1
    fresh = freshness(read_summary())
    if as_json:
        print(json.dumps({"ok": True, "budgets": results, "summary_path": str(summary_path()),
                          "as_of": fresh["as_of"], "spend_through": fresh["spend_through"]},
                         default=str))
        return 0
    if not results:
        print("No cloud budgets set. Ask your AI to \"set a monthly budget of $X\", "
              "or sync a budget.yml.")
    else:
        for b in results:
            print(_line(b))
    through = fresh["spend_through"]
    print(f"\n  Cost data through {through}." if through else "\n  No cost data in this period yet.")
    if refresh:
        print(f"  Guard spend figure updated: {summary_path()}")
    return 0


def run(parsed) -> int:
    action = getattr(parsed, "budget_action", "status") or "status"
    as_json = bool(getattr(parsed, "budget_json", False))
    return _status(as_json, refresh=action == "refresh")
