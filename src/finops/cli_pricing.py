# SPDX-License-Identifier: Apache-2.0
"""`nable pricing margins`: the modeled gross margin of every paid plan.

    nable pricing margins [--option A|B|all] [--lines PLAN] [--json]

An internal command for the founder, not in `nable --help`. It prints, for
each paid plan and billing period, the monthly price, the modeled cost to
serve one tenant and the gross margin in the low, expected and high case, and
marks any row under the 80% floor. The numbers come from finops.margin_guard,
the same model tests/test_margin_guard.py holds to that floor; nothing here
reads an account or a bill.

Exit codes: 0 printed, 2 usage.
"""
from __future__ import annotations

import json
import sys


def add_parser(sub) -> None:
    p = sub.add_parser(
        "pricing",
        help="Internal: modeled gross margin per plan (nable pricing margins)",
    )
    p.add_argument("pricing_action", nargs="?", default="margins", choices=["margins"],
                   help="margins: the margin table for every paid plan")
    p.add_argument("--option", default="all", choices=["A", "B", "all"],
                   help="which proposed price option to show (default: both)")
    p.add_argument("--lines", default=None, metavar="PLAN",
                   help="also print the cost lines for one plan (cloud, growth, team, ...)")
    p.add_argument("--json", action="store_true", help="machine-readable output on stdout")


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _render(option: str, plans: dict, proposed: bool) -> list[str]:
    from . import margin_guard as mg
    growth, cloud = plans["growth"], plans["cloud"]
    head = (f"Option {option}: Cloud ${cloud.monthly_usd:,.0f}, Growth ${growth.monthly_usd:,.0f}"
            + ("  (proposed)" if proposed else ""))
    out = [head,
           f"  {'plan':<26}{'billing':<9}{'price/mo':>10}  {'COGS low / expected / high':>30}"
           + f"  {'margin low / expected / high':>30}"]
    for r in mg.table(plans):
        cogs = (f"${r['cogs_low_usd']:,.2f} / ${r['cogs_expected_usd']:,.2f} / "
                f"${r['cogs_high_usd']:,.2f}")
        gm = (f"{_pct(r['margin_low'])} / {_pct(r['margin_expected'])} / "
              f"{_pct(r['margin_high'])}")
        flag = "" if r["clears_floor"] else "   UNDER 80%"
        out.append(f"  {r['name']:<26}{r['billing']:<9}{'$' + format(r['price_month_usd'], ',.2f'):>10}"
                   f"  {cogs:>30}  {gm:>30}{flag}")
    return out


def _lines(plan_id: str, plans: dict) -> list[str]:
    from . import margin_guard as mg
    p = plans[plan_id]
    rows = {case: mg.plan_cogs(p, case) for case in mg.CASES}
    keys = list(rows["high"])
    out = [f"  {p.name} (${p.monthly_usd:,.0f}/mo), cost lines per tenant-month, monthly billing",
           f"    {'line':<18}{'low':>10}{'expected':>10}{'high':>10}"]
    for k in keys:
        out.append(f"    {k:<18}" + "".join(f"{'$' + format(rows[c].get(k, 0.0), ',.2f'):>10}"
                                            for c in mg.CASES))
    return out


def run(args) -> int:
    from . import margin_guard as mg
    options = ["A", "B"] if args.option == "all" else [args.option]
    ladders = {o: mg.ladder(o) for o in options}
    if args.lines and any(args.lines not in lad for lad in ladders.values()):
        print(f"nable pricing: no plan named {args.lines!r}; plans are "
              f"{', '.join(mg.PROPOSED_PLANS)}", file=sys.stderr)
        return 2

    if args.json:
        doc = {
            "floor": mg.MARGIN_FLOOR,
            "proposed_option": mg.PROPOSED_OPTION,
            "options": {o: mg.table(lad) for o, lad in ladders.items()},
        }
        if args.lines:
            doc["lines"] = {o: {c: mg.plan_cogs(lad[args.lines], c) for c in mg.CASES}
                            for o, lad in ladders.items()}
        print(json.dumps(doc, indent=2))
        return 0

    out = ["nable pricing margins: modeled gross margin per paid plan",
           f"floor {_pct(mg.MARGIN_FLOOR)} in the expected and the high case, monthly and annual",
           "high = every capped cost at its cap in the same month (see docs/PRICING-MODEL.md)",
           ""]
    for o, lad in ladders.items():
        out += _render(o, lad, o == mg.PROPOSED_OPTION)
        if args.lines:
            out += _lines(args.lines, lad)
        out.append("")
    out.append("Pro and Team prices are read from finops.license.PLANS; Cloud, Growth and "
               "Enterprise are proposals, not on sale.")
    print("\n".join(out))
    return 0
