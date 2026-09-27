# SPDX-License-Identifier: Apache-2.0
"""`nable org`: the org model from the terminal, and the human side of it.

  nable org init [--here]        create the directory, import what nable already
                                 knows, ask the top questions (on a terminal)
  nable org status [--json]      where it lives, counts, coverage, stale, conflicts
  nable org review [--kind K]    proposals waiting for a human, with their keys
  nable org confirm KEY... [--as WHO]
  nable org reject KEY... [--as WHO]
  nable org set owner --subject aws_account:123 --team payments [--channel ...]
  nable org questions [--limit N] [--json]
  nable org export [--format json|yaml] [--out PATH]

Confirming, rejecting and `set` are the human path the whole model rests on.
They record who decided: --as WHO, or on a terminal git's user.email, then
$USER. Without a terminal and without --as they refuse, so a script or an
agent cannot quietly sign a person's name.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

_ACTIONS = ("init", "status", "review", "confirm", "reject", "set", "questions", "export")


def add_parser(sub) -> None:
    p = sub.add_parser(
        "org",
        help="The org model: who owns what, proposed by nable, confirmed by you",
        description="Facts about this org (owners, teams, environments, tag keys, "
                    "accounts, thresholds) as plain YAML you own. nable and your agents "
                    "propose; only a person confirms.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", dest="org_dir", default=None, metavar="PATH",
                        help="Org model directory (default: FINOPS_ORG_DIR, the repo's "
                             "nable.org/ if it exists, else the nable data dir)")
    osub = p.add_subparsers(dest="org_action", metavar="<action>")

    x = osub.add_parser("init", parents=[common], help="Create the model, import what nable "
                        "knows, ask the top questions")
    x.add_argument("--here", dest="org_here", action="store_true",
                   help="Create nable.org/ at the root of this git repo (to commit it)")
    x.add_argument("--limit", dest="org_limit", type=int, default=10, metavar="N")
    x.add_argument("--as", dest="org_as", default=None, metavar="WHO",
                   help="Who is answering (default: git user.email, then $USER)")

    x = osub.add_parser("status", parents=[common], help="Location, counts, coverage, "
                        "stale facts and conflicts")
    x.add_argument("--json", dest="org_json", action="store_true")

    x = osub.add_parser("review", parents=[common], help="Proposed facts with their keys")
    x.add_argument("--kind", dest="org_kind", default=None, metavar="KIND")
    x.add_argument("--json", dest="org_json", action="store_true")

    for name, verb in (("confirm", "Confirm"), ("reject", "Reject")):
        x = osub.add_parser(name, parents=[common], help=f"{verb} facts by key (a human decision)")
        x.add_argument("org_keys", nargs="+", metavar="KEY")
        x.add_argument("--as", dest="org_as", default=None, metavar="WHO",
                       help="Who decided (default on a terminal: git user.email, then $USER)")

    x = osub.add_parser("set", parents=[common], help="State a fact directly (confirmed)")
    x.add_argument("org_set_kind", choices=["owner", "environment", "team"], metavar="KIND",
                   help="owner | environment | team")
    x.add_argument("--subject", dest="org_subject", default=None, metavar="KIND:ID",
                   help="e.g. aws_account:123456789012, repo_path:infra/payments "
                        "(owner, environment)")
    x.add_argument("--team", dest="org_team", default=None,
                   help="owner: the owning team; team: the team's id")
    x.add_argument("--channel", dest="org_channel", default=None,
                   help="#channel or an email address")
    x.add_argument("--people", dest="org_people", default=None, metavar="A,B")
    x.add_argument("--env", dest="org_env", default=None,
                   help="environment: prod | nonprod | dr | sandbox | shared | unknown")
    x.add_argument("--alias", dest="org_aliases", action="append", default=None,
                   metavar="NAME", help="team: another name for it (repeatable)")
    x.add_argument("--review-after", dest="org_review_after", default=None, metavar="YYYY-MM-DD")
    x.add_argument("--as", dest="org_as", default=None, metavar="WHO")

    x = osub.add_parser("questions", parents=[common], help="The top questions, most "
                        "dollars first")
    x.add_argument("--limit", dest="org_limit", type=int, default=10, metavar="N")
    x.add_argument("--json", dest="org_json", action="store_true")

    x = osub.add_parser("export", parents=[common], help="The whole model as YAML or JSON")
    x.add_argument("--format", dest="org_format", choices=["yaml", "json"], default="yaml")
    x.add_argument("--out", dest="org_out", default=None, metavar="PATH")

    p.set_defaults(cmd="org")


# ── the human ─────────────────────────────────────────────────────────────────

def _is_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _git_email() -> str | None:
    import subprocess
    try:
        r = subprocess.run(["git", "config", "user.email"], capture_output=True,
                           text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    email = (r.stdout or "").strip()
    return email or None


def _who(as_: str | None) -> str | None:
    """Who is deciding, or None when nobody can be named honestly: no --as
    and no terminal means no human is known to be there."""
    if as_ and as_.strip():
        return as_.strip()
    if not _is_tty():
        return None
    import getpass
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no name is an answer too
        user = ""
    return _git_email() or os.environ.get("USER") or user or None


def _need_human() -> int:
    print("This is a human decision: run it in a terminal, or pass --as WHO "
          "(your name or email) to record who decided.", file=sys.stderr)
    return 2


# ── actions ───────────────────────────────────────────────────────────────────

def _fmt_usd(x: float | None) -> str:
    return f"${x:,.0f}/mo" if x else ""


def _status(parsed, org) -> int:
    m = org.load(parsed.org_dir)
    cov = org.coverage(m)
    stale = m.stale()
    conflicts = m.conflicts()
    exists = m.dir.is_dir()
    if getattr(parsed, "org_json", False):
        print(json.dumps({"dir": str(m.dir), "dir_source": m.dir_source, "exists": exists,
                          "counts": m.status_counts(), "by_kind": m.kind_counts(),
                          "coverage": cov, "stale": [f.summary() for f in stale],
                          "conflicts": [{"confirmed": w.summary(), "proposed": p.summary()}
                                        for w, p in conflicts],
                          "warnings": m.warnings}, default=str))
        return 0
    where = {"argument": "--dir", "FINOPS_ORG_DIR": "FINOPS_ORG_DIR", "repo": "this repo",
             "data_dir": "nable data dir"}.get(m.dir_source, m.dir_source)
    print(f"Org model: {m.dir} ({where})" + ("" if exists else ", not created yet: "
                                             "run `nable org init`"))
    c = m.status_counts()
    print(f"  facts: {c['confirmed']} confirmed, {c['proposed']} proposed, "
          f"{c['rejected']} rejected, {c['expired']} expired")
    for kind, kc in sorted(m.kind_counts().items()):
        print(f"    {kind}: " + ", ".join(f"{n} {s}" for s, n in kc.items() if n))
    print(f"  coverage: {cov['summary']}")
    if cov["basis"] == "subjects" and cov["subjects"]["total"]:
        s = cov["subjects"]
        print(f"    subjects: {s['confirmed_owner']} of {s['total']} with a confirmed owner, "
              f"{s['proposed_owner']} proposed")
    if stale:
        print(f"  stale (past review_after, still used): {len(stale)}")
        for f in stale[:10]:
            print(f"    {f.key}  {f.subject}  review was due {f.review_after}")
    if conflicts:
        print(f"  conflicts (a proposal disagrees with a confirmed fact): {len(conflicts)}")
        for w, p in conflicts[:10]:
            print(f"    {p.key} proposes {p.value} for {p.subject}; confirmed {w.key} says {w.value}")
    for w in m.warnings:
        print(f"  warning: {w}", file=sys.stderr)
    return 0


def _review(parsed, org) -> int:
    from .questions import describe
    m = org.load(parsed.org_dir)
    props = m.proposals(parsed.org_kind)
    conflicted = {p.key: w for w, p in m.conflicts()}
    if getattr(parsed, "org_json", False):
        rows = []
        for f in props:
            d = f.summary()
            if f.key in conflicted:
                d["conflicts_with"] = conflicted[f.key].key
            rows.append(d)
        print(json.dumps({"dir": str(m.dir), "proposed": rows}, default=str))
        return 0
    if not props:
        print("Nothing waiting for review.")
        return 0
    for f in sorted(props, key=lambda f: (-(f.dollars_monthly or 0), f.fact, str(f.subject))):
        note = f"  [conflicts with confirmed {conflicted[f.key].key}]" if f.key in conflicted else ""
        usd = _fmt_usd(f.dollars_monthly)
        print(f"  {f.key}  {describe(f)}" + (f"  {usd}" if usd else "") +
              f"  ({f.source}, confidence {f.confidence:.2f}){note}")
    print("\n  Confirm: nable org confirm KEY...    Reject: nable org reject KEY...")
    return 0


def _decide(parsed, org, verb: str) -> int:
    who = _who(parsed.org_as)
    if who is None:
        return _need_human()
    fn = org.confirm if verb == "confirm" else org.reject
    code = 0
    for key in parsed.org_keys:
        try:
            f = fn(key, who, parsed.org_dir)
        except org.OrgError as e:
            print(f"  {key}: {e}", file=sys.stderr)
            code = 1
            continue
        print(f"  {f.key}  {f.status} by {f.confirmed_by or who}: {f.fact} {f.subject} {f.value}")
    return code


def _set(parsed, org) -> int:
    who = _who(parsed.org_as)
    if who is None:
        return _need_human()
    kind = parsed.org_set_kind
    people = [p.strip() for p in (parsed.org_people or "").split(",") if p.strip()]
    value: dict[str, Any]
    if kind == "team":
        if not parsed.org_team:
            print("set team needs --team ID", file=sys.stderr)
            return 2
        subject: Any = f"team:{parsed.org_team}"
        value = {"name": parsed.org_team}
        if parsed.org_aliases:
            value["aliases"] = parsed.org_aliases
    else:
        if not parsed.org_subject:
            print(f"set {kind} needs --subject KIND:ID", file=sys.stderr)
            return 2
        subject = parsed.org_subject
        if kind == "owner":
            if not parsed.org_team:
                print("set owner needs --team TEAM", file=sys.stderr)
                return 2
            value = {"team": parsed.org_team}
        else:
            value = {"env": parsed.org_env}
    if kind in ("owner", "team"):
        if parsed.org_channel:
            value["channel"] = parsed.org_channel
        if people:
            value["people"] = people
    try:
        fact = org.Fact.from_dict({"fact": kind, "subject": org.subject_of(subject).to_dict(),
                                   "value": value, "source": "human", "status": "confirmed",
                                   "review_after": parsed.org_review_after})
        f = org.set_fact(fact, who, parsed.org_dir)
    except (org.FactError, org.OrgError) as e:
        print(f"Not set: {e}", file=sys.stderr)
        return 1
    print(f"  {f.key}  confirmed by {who}: {f.fact} {f.subject} {f.value}")
    return 0


def _questions(parsed, org) -> int:
    qs = org.questions(parsed.org_limit, model=org.load(parsed.org_dir))
    if getattr(parsed, "org_json", False):
        print(json.dumps({"questions": [q.to_dict() for q in qs]}, default=str))
        return 0
    if not qs:
        print("No questions: nothing proposed, stale or unowned.")
        return 0
    for i, q in enumerate(qs, 1):
        print(f"  {i}. {q.text}")
        print(f"     yes: {q.command}" + (f"    no: {q.no_command}" if q.no_command else ""))
    return 0


def _export(parsed, org) -> int:
    try:
        org.export(parsed.org_out, parsed.org_format, dir=parsed.org_dir)
    except OSError as e:
        print(f"Export failed: {e}", file=sys.stderr)
        return 1
    if parsed.org_out:
        print(f"Wrote {parsed.org_out}", file=sys.stderr)
    return 0


def _ask(prompt: str) -> str | None:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _interview(qs, org, d, who: str) -> None:
    """Ask each question: y/n/edit with the default shown. 'q' stops."""
    for i, q in enumerate(qs, 1):
        print(f"\n  {i}. {q.text}")
        if q.kind == "unowned":
            team = _ask("     Team (blank to skip, q to stop): ")
            if team is None or team.lower() == "q":
                return
            if team:
                f = org.Fact.from_dict({"fact": "owner", "subject": q.subject,
                                        "value": {"team": team}, "source": "human",
                                        "status": "confirmed"})
                org.set_fact(f, who, d)
                print(f"     Recorded: {q.subject} is owned by {team}.")
            continue
        choices = "[Y/n/e/q]" if q.default == "y" else "[y/N/e/q]"
        ans = _ask(f"     {choices} ")
        if ans is None or ans.lower() == "q":
            return
        ans = (ans or q.default).lower()[:1]
        if ans == "y":
            org.confirm(q.key, who, d)
            print("     Confirmed.")
        elif ans == "n":
            org.reject(q.key, who, d)
            print("     Rejected; it will not be proposed again.")
        elif ans == "e":
            fact = q.fact or {}
            if fact.get("fact") != "owner":
                print(f"     Edit it by hand in {d}, then confirm with {q.command}.")
                continue
            team = _ask("     The owning team: ")
            if not team:
                continue
            f = org.Fact.from_dict({"fact": "owner", "subject": fact["subject"],
                                    "value": {**fact["value"], "team": team},
                                    "source": "human", "status": "confirmed"})
            org.set_fact(f, who, d)
            org.reject(q.key, who, d)
            print(f"     Recorded: {fact['subject']} is owned by {team}.")


def _init(parsed, org) -> int:
    if parsed.org_here:
        root = org.git_root()
        if root is None:
            print("--here needs a git repo: run it inside the repo that should hold "
                  "nable.org/.", file=sys.stderr)
            return 2
        d = root / org.ORG_DIR_NAME
        if os.environ.get("FINOPS_ORG_DIR"):
            print(f"  note: FINOPS_ORG_DIR is set, so nable reads "
                  f"{os.environ['FINOPS_ORG_DIR']}, not {d}, until it is unset.",
                  file=sys.stderr)
    else:
        d, _ = org.resolve_dir(parsed.org_dir)
    from .store import ensure_dir, run_adapters
    made = ensure_dir(d)
    print(f"Org model: {d}" + (f" (created {len(made)} files)" if made else ""))
    added = org.import_legacy(d)
    print(f"  imported {added} fact(s) nable already had (tag_rules.yaml, accounts.yaml, "
          "FINOPS_REQUIRED_TAGS/FINOPS_PROTECTED_TAGS)")
    counts = run_adapters(d)
    if counts:
        print("  adapters: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    m = org.load(d)
    qs = org.questions(parsed.org_limit, model=m)
    if not qs:
        print("  No questions: nothing proposed, stale or unowned yet.")
    elif _is_tty():
        who = _who(parsed.org_as)
        if who is None:
            return _need_human()
        print(f"  {len(qs)} question(s), most dollars first. Answering as {who}.")
        _interview(qs, org, d, who)
    else:
        print(f"  {len(qs)} question(s), most dollars first (answer each with its command):")
        for i, q in enumerate(qs, 1):
            print(f"  {i}. {q.text}")
            print(f"     yes: {q.command}" + (f"    no: {q.no_command}" if q.no_command else ""))
    print(f"  coverage: {org.coverage(org.load(d))['summary']}")
    return 0


def run(parsed) -> int:
    from .. import org
    action = getattr(parsed, "org_action", None) or "status"
    if not hasattr(parsed, "org_dir"):
        parsed.org_dir = None
    try:
        if action == "init":
            return _init(parsed, org)
        if action == "status":
            return _status(parsed, org)
        if action == "review":
            return _review(parsed, org)
        if action in ("confirm", "reject"):
            return _decide(parsed, org, action)
        if action == "set":
            return _set(parsed, org)
        if action == "questions":
            return _questions(parsed, org)
        if action == "export":
            return _export(parsed, org)
    except (org.OrgError, org.FactError) as e:
        print(f"nable org {action}: {e}", file=sys.stderr)
        return 1
    print(f"unknown org action {action!r} (one of {', '.join(_ACTIONS)})", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    """`python -m finops.org.cli ...`, the same as `nable org ...`."""
    parser = argparse.ArgumentParser(prog="nable")
    add_parser(parser.add_subparsers(dest="cmd"))
    return run(parser.parse_args(["org", *(sys.argv[1:] if argv is None else argv)]))


if __name__ == "__main__":
    raise SystemExit(main())
