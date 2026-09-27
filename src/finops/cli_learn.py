# SPDX-License-Identifier: Apache-2.0
"""`nable learn`: what nable has learned from your decisions, and the undo.

  nable learn list [--all] [--json]      the lessons (learning/ledger.py): each
                                         learned adjustment with its evidence;
                                         --all adds superseded ones
  nable learn show ID [--json]           one lesson in full
  nable learn rollback ID [--note TEXT] [--as WHO]
                                         overrule it: standard behaviour for its
                                         key until restored (a person's decision)
  nable learn restore ID [--as WHO]      lift a rollback; the next sync decides
                                         again from live evidence
  nable learn infer [--dry-run] [--days N] [--dir PATH] [--json]
                                         what the guard's asks support: repeated
                                         approvals become a proposed threshold,
                                         repeated declines a tighter one, each
                                         with its evidence; --dry-run proposes
                                         nothing (`nable org questions` asks
                                         about what is proposed)

Rollback and restore are a person's decision, like confirming an org fact:
they take the HumanDecision only the CLI makes (--as WHO, or a terminal),
and refuse without one, and the guard asks before an agent runs them.
Reading (list, show, infer) is anyone's.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

_ACTIONS = ("list", "show", "rollback", "restore", "infer")


def add_parser(sub) -> None:
    p = sub.add_parser(
        "learn",
        help="What nable learned from your decisions, the evidence, and the undo",
        description="Lessons nable learned from what you accept, dismiss, approve and "
                    "decline. It only proposes; rollback and restore are yours.",
    )
    lsub = p.add_subparsers(dest="learn_action", metavar="<action>")
    x = lsub.add_parser("list", help="The lessons, with their evidence")
    x.add_argument("--all", dest="learn_all", action="store_true",
                   help="Include superseded lessons")
    x.add_argument("--json", dest="learn_json", action="store_true")
    x = lsub.add_parser("show", help="One lesson in full")
    x.add_argument("learn_id", type=int, metavar="ID")
    x.add_argument("--json", dest="learn_json", action="store_true")
    x = lsub.add_parser("rollback", help="Overrule a lesson (a person's decision)")
    x.add_argument("learn_id", type=int, metavar="ID")
    x.add_argument("--note", dest="learn_note", default="", help="Why, kept with the lesson")
    x.add_argument("--as", dest="learn_as", default=None, metavar="WHO",
                   help="Who decided (default on a terminal: git user.email, then $USER)")
    x = lsub.add_parser("restore", help="Lift a rollback (a person's decision)")
    x.add_argument("learn_id", type=int, metavar="ID")
    x.add_argument("--as", dest="learn_as", default=None, metavar="WHO")
    x = lsub.add_parser("infer", help="Thresholds the guard's asks support, with evidence")
    x.add_argument("--dry-run", dest="learn_dry_run", action="store_true",
                   help="Show what would be proposed; write nothing")
    x.add_argument("--days", dest="learn_days", type=float, default=None, metavar="N",
                   help="How many days of the decision ledger to read (default 90)")
    x.add_argument("--dir", dest="learn_dir", default=None, metavar="PATH",
                   help="Org model directory to propose into (as `nable org --dir`)")
    x.add_argument("--json", dest="learn_json", action="store_true")
    p.set_defaults(cmd="learn")


def _lesson_line(x: dict[str, Any]) -> str:
    to = x.get("to_verdict") or ""
    since = (x.get("first_seen") or "")[:10]
    status = "" if x.get("status") == "active" else f"  [{x.get('status')}]"
    return f"  {x['id']:>4}  {x['key']}  {to}  since {since}{status}"


def _list(parsed) -> int:
    from .recommendations.learning.ledger import lessons, sync_lessons
    try:
        sync_lessons()                 # record what the signal says now; never a rollback
    except Exception as e:  # noqa: BLE001 - the recorded lessons are still worth showing
        print(f"  (lessons not refreshed: {type(e).__name__}: {e})", file=sys.stderr)
    rows = lessons(include_history=bool(getattr(parsed, "learn_all", False)))
    if getattr(parsed, "learn_json", False):
        print(json.dumps({"lessons": rows}, default=str))
        return 0
    if not rows:
        print("No lessons yet: nable records one when what you act on and dismiss moves a "
              "recommendation type up or down.")
        return 0
    for x in rows:
        print(_lesson_line(x))
        print(f"        {x['lesson']}")
    print("\n  Details: nable learn show ID    Undo: nable learn rollback ID")
    return 0


def _show(parsed) -> int:
    from .recommendations.learning.ledger import lesson
    x = lesson(parsed.learn_id)
    if x is None:
        print(f"nable learn show: no lesson with id {parsed.learn_id}", file=sys.stderr)
        return 1
    if getattr(parsed, "learn_json", False):
        print(json.dumps(x, default=str))
        return 0
    print(_lesson_line(x))
    print(f"        {x['lesson']}")
    print(f"        was {x.get('from_verdict') or 'neutral'}, now {x.get('to_verdict')}; "
          f"first seen {x.get('first_seen')}, last confirmed {x.get('last_confirmed')}")
    print("        evidence: " + ", ".join(f"{k}={v}" for k, v in (x.get("evidence") or {}).items()
                                           if v is not None))
    if x.get("rollback_note"):
        print(f"        rollback: {x['rollback_note']}")
    if x.get("status") == "rolled_back":
        print(f"\n  Restore: nable learn restore {x['id']}")
    elif x.get("status") == "active":
        print(f"\n  Undo: nable learn rollback {x['id']}")
    return 0


def _decide(parsed, verb: str) -> int:
    from .org.cli import _need_human, _who
    from .recommendations.learning import ledger
    who = _who(getattr(parsed, "learn_as", None))
    if who is None:
        return _need_human()
    if verb == "rollback":
        res = ledger.rollback(parsed.learn_id, getattr(parsed, "learn_note", "") or "", by=who)
    else:
        res = ledger.restore(parsed.learn_id, by=who)
    if res.get("error"):
        print(f"nable learn {verb}: {res['error']}", file=sys.stderr)
        return 1
    done = "already " if res.get("already") else ""
    word = "rolled back" if verb == "rollback" else "restored"
    print(f"  {parsed.learn_id} ({res.get('key')}): {done}{word} by {who}.")
    if res.get("effect"):
        print(f"  {res['effect'][0].upper()}{res['effect'][1:]}.")
    return 0


def _money(x: Any) -> str:
    return f"${x:,.0f}/mo" if isinstance(x, (int, float)) else "?"


def _infer(parsed) -> int:
    from .recommendations.learning.policy_inference import propose_guard_facts
    from .recommendations.learning.signal import GUARD_LOOKBACK_DAYS, guard_signal
    days = getattr(parsed, "learn_days", None) or GUARD_LOOKBACK_DAYS
    dry = bool(getattr(parsed, "learn_dry_run", False))
    got = propose_guard_facts(getattr(parsed, "learn_dir", None), dry_run=dry,
                              signal=guard_signal(days=days))
    if getattr(parsed, "learn_json", False):
        props = [{k: v for k, v in p.items() if k != "fact"} for p in got["proposals"]]
        print(json.dumps({"dry_run": dry, "days": days, "proposals": props,
                          "not_yet": got["not_yet"]}, default=str))
        return 0
    head = "Would propose" if dry else "Proposed"
    if not got["proposals"]:
        print(f"Nothing to propose from the last {days:g} days of the guard's asks.")
    for p in got["proposals"]:
        way = "tighter" if p["direction"] == "tighten" else "higher"
        state = "" if dry else f"  ({p.get('result')})"
        print(f"  {head}: a {way} threshold for {p['subject']}, "
              f"{_money(p['value']['max_auto_monthly_usd'])} "
              f"(now {_money(p['current_usd'])}), confidence {p['confidence']:.2f}{state}")
        print(f"      {p['note']}")
        print(f"      key {p['key']}: a person answers it in nable org questions "
              f"(nable org confirm {p['key']} / nable org reject {p['key']})")
    if got["not_yet"]:
        print("\n  Not yet:")
        for n in got["not_yet"]:
            what = n.get("subject") or " ".join(
                x for x in (n.get("action_type"), n.get("door"),
                            f"team {n['team']}" if n.get("team") else "",
                            f"env {n['env']}" if n.get("env") else "") if x)
            print(f"    {what}: {n['why']}")
    if dry and got["proposals"]:
        print("\n  Nothing was written (--dry-run). Without it, these become proposals.")
    return 0


def run(parsed) -> int:
    from .org import OrgError
    action = getattr(parsed, "learn_action", None) or "list"
    try:
        if action == "list":
            return _list(parsed)
        if action == "show":
            return _show(parsed)
        if action in ("rollback", "restore"):
            return _decide(parsed, action)
        if action == "infer":
            return _infer(parsed)
    except (OrgError, OSError, ValueError) as e:
        print(f"nable learn {action}: {e}", file=sys.stderr)
        return 1
    print(f"unknown learn action {action!r} (one of {', '.join(_ACTIONS)})", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    """`python -m finops.cli_learn ...`, the same as `nable learn ...`."""
    parser = argparse.ArgumentParser(prog="nable")
    add_parser(parser.add_subparsers(dest="cmd"))
    return run(parser.parse_args(["learn", *(sys.argv[1:] if argv is None else argv)]))


if __name__ == "__main__":
    raise SystemExit(main())
