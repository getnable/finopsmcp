# SPDX-License-Identifier: Apache-2.0
"""`nable org`: the org model from the terminal, and the human side of it.

  nable org init [--here] [--repo PATH]... [--no-adapters]
                                 create the directory, import what nable already
                                 knows, run the adapters (CODEOWNERS, Terraform,
                                 AWS Organizations, tags, workload), ask the top
                                 questions (on a terminal)
  nable org status [--json]      where it lives, counts, coverage, stale, conflicts
  nable org review [--kind K]    proposals waiting for a human, with their keys
  nable org confirm KEY... [--as WHO]
  nable org confirm --owner-bulk TEAM@DIGEST | --env-bulk ENV@DIGEST
                    | --kind-bulk KIND@DIGEST [--as WHO]
                                 (the exact command `nable org questions` prints)
  nable org reject KEY... [--as WHO]    (and the same bulk flags)
  nable org set owner --subject aws_account:123 --team payments [--channel ...]
  nable org set threshold --subject team:payments --max-auto-usd 200
  nable org set freeze --scope environment:prod --start 2026-11-27T00:00-05:00
                       --end 2026-12-01T00:00-05:00 --reason "Black Friday" [--mode deny]
                                 a change freeze: priced changes and one-way doors
                                 in that scope ask (or are denied) until it ends
  nable org set approval --scope team:payments --action-class rightsizing
                         --approver github:alice --approver team:platform [--min 1]
                                 who reviews that class of change: nable's pull
                                 requests request them, its tickets add them
  nable org trust [--here] [--revoke]   trust this repo's nable.org/ (a person's call)
  nable org questions [--limit N] [--json]
                                 (init and questions first read the guard's
                                 decision ledger and propose the thresholds
                                 what people keep deciding supports:
                                 nable learn infer --dry-run shows why)
  nable org export [--format json|yaml] [--out PATH]

Confirming, rejecting and `set` are the human path the whole model rests on.
They record who decided: --as WHO, or on a terminal git's user.email, then
$USER. Without a terminal and without --as they refuse, so a script or an
agent cannot quietly sign a person's name. _who() is also the only maker of
the store's HumanDecision, which confirm/reject/set_fact require.

Where init writes: `nable org init` writes to the nable data dir (or --dir,
or FINOPS_ORG_DIR), never into a repo's tracked files; `init --here` creates
the repo's nable.org/ and records that this person trusts it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

_ACTIONS = ("init", "status", "review", "confirm", "reject", "set", "questions", "export",
            "trust")


def add_parser(sub) -> None:
    p = sub.add_parser(
        "org",
        help="The org model: who owns what, proposed by nable, confirmed by you",
        description="Facts about this org (owners, teams, environments, tag keys, "
                    "accounts, thresholds, freezes, approval chains) as plain YAML you own. nable and your agents "
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
    x.add_argument("--repo", dest="org_repos", action="append", default=None,
                   metavar="PATH", help="Another repo for the adapters to read (repeatable); "
                   "the repo you are in is always read")
    x.add_argument("--no-adapters", dest="org_no_adapters", action="store_true",
                   help="Do not run the adapters: import and ask only")

    x = osub.add_parser("status", parents=[common], help="Location, counts, coverage, "
                        "stale facts and conflicts")
    x.add_argument("--json", dest="org_json", action="store_true")

    x = osub.add_parser("review", parents=[common], help="Proposed facts with their keys")
    x.add_argument("--kind", dest="org_kind", default=None, metavar="KIND")
    x.add_argument("--json", dest="org_json", action="store_true")

    for name, verb in (("confirm", "Confirm"), ("reject", "Reject")):
        x = osub.add_parser(name, parents=[common], help=f"{verb} facts by key (a human decision)")
        x.add_argument("org_keys", nargs="*", metavar="KEY")
        x.add_argument("--as", dest="org_as", default=None, metavar="WHO",
                       help="Who decided (default on a terminal: git user.email, then $USER)")
        x.add_argument("--owner-bulk", dest="org_owner_bulk", default=None,
                       metavar="TEAM@DIGEST",
                       help=f"{verb} the proposals a bulk question listed for TEAM (owner, "
                            "team and team alias facts); DIGEST is the one `nable org "
                            "questions` printed, and a set that changed since is refused")
        x.add_argument("--env-bulk", dest="org_env_bulk", default=None, metavar="ENV@DIGEST",
                       help=f"{verb} the proposals a bulk question listed as ENV")
        x.add_argument("--kind-bulk", dest="org_kind_bulk", default=None,
                       metavar="KIND@DIGEST",
                       help=f"{verb} the proposals a bulk question listed of one kind "
                            "(account, tag_key, ...)")

    x = osub.add_parser("set", parents=[common], help="State a fact directly (confirmed)")
    x.add_argument("org_set_kind", choices=["owner", "environment", "team", "threshold",
                                             "freeze", "approval"],
                   metavar="KIND", help="owner | environment | team | threshold | freeze | "
                                        "approval")
    x.add_argument("--subject", "--scope", dest="org_subject", default=None, metavar="KIND:ID",
                   help="e.g. aws_account:123456789012, repo_path:infra/payments "
                        "(owner, environment); team:payments, environment:prod or org:org "
                        "(threshold, freeze; an account too for a freeze); team:payments or "
                        "environment:prod (approval)")
    x.add_argument("--start", dest="org_start", default=None, metavar="WHEN",
                   help="freeze: when it starts, ISO 8601 (2026-11-27T00:00-05:00); without "
                        "an offset, in --tz or this machine's time zone")
    x.add_argument("--end", dest="org_end", default=None, metavar="WHEN",
                   help="freeze: when it ends, the same way")
    x.add_argument("--tz", dest="org_tz", default=None, metavar="ZONE",
                   help="freeze: the time zone of a --start or --end written without an "
                        "offset (America/New_York)")
    x.add_argument("--reason", dest="org_reason", default=None,
                   help="freeze: why, shown in every ask or deny it causes")
    x.add_argument("--mode", dest="org_mode", choices=["ask", "deny"], default=None,
                   help="freeze: ask (default) or deny")
    x.add_argument("--action-class", dest="org_action_classes", action="append", default=None,
                   metavar="CLASS", help="approval: a class of change (rightsizing, "
                                         "delete_resource, ..., or '*'); repeatable")
    x.add_argument("--approver", dest="org_approvers", action="append", default=None,
                   metavar="KIND:ID", help="approval: github:LOGIN, team:SLUG (a GitHub "
                                           "team), jira:ACCOUNT_ID, linear:USER_ID or "
                                           "email:ADDRESS; repeatable")
    x.add_argument("--min", dest="org_min", type=int, default=None, metavar="N",
                   help="approval: how many of them must approve (default 1)")
    x.add_argument("--change-ticket", dest="org_change_ticket", action="store_true",
                   help="approval: a change ticket is required; nable's pull requests "
                        "carry a place for its link")
    x.add_argument("--max-auto-usd", dest="org_max_auto", type=float, default=None,
                   metavar="USD", help="threshold: the most a change may add per month "
                                       "and run without asking")
    x.add_argument("--velocity-cap-usd", dest="org_velocity_cap", type=float, default=None,
                   metavar="USD", help="threshold: the velocity cap, $/mo per window")
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

    x = osub.add_parser("trust", parents=[common],
                        help="Trust this repo's nable.org/ (its owners pick the guard's "
                             "team, its thresholds may raise limits)")
    x.add_argument("--here", dest="org_here", action="store_true",
                   help="The repo holding the working directory (the default)")
    x.add_argument("--revoke", dest="org_revoke", action="store_true",
                   help="Stop trusting it")
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


def _who(as_: str | None) -> Any:
    """Who is deciding, as the store's HumanDecision, or None when nobody
    can be named honestly: no --as and no terminal means no human is known
    to be there. The only place a HumanDecision is made."""
    from .store import _MINT, HumanDecision
    if as_ and as_.strip():
        return HumanDecision(as_.strip(), "--as", _token=_MINT)
    if not _is_tty():
        return None
    import getpass
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no name is an answer too
        user = ""
    name = _git_email() or os.environ.get("USER") or user
    return HumanDecision(name, "terminal", _token=_MINT) if name else None


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
    loose = m.without_repo()
    untrusted = [layer for layer in m.layers if not layer.trusted]
    freezes = _freeze_rows(m)
    approvals = [f for f in m.by_kind("approval") if f.live]
    if getattr(parsed, "org_json", False):
        print(json.dumps({"dir": str(m.dir), "dir_source": m.dir_source, "exists": exists,
                          "freezes": [{**f.summary(), "state": state, "applies_as": how}
                                      for f, state, how in freezes],
                          "approvals": [f.summary() for f in approvals],
                          "layers": [layer.to_dict() for layer in m.layers],
                          "counts": m.status_counts(), "by_kind": m.kind_counts(),
                          "coverage": cov, "stale": [f.summary() for f in stale],
                          "conflicts": [{"confirmed": w.summary(),
                                         ("confirmed_too" if p.confirmed else "proposed"):
                                         p.summary()} for w, p in conflicts],
                          "repo_paths_without_repo": [f.summary() for f in loose],
                          "warnings": m.warnings}, default=str))
        return 0
    where = {"argument": "--dir", "FINOPS_ORG_DIR": "FINOPS_ORG_DIR", "repo": "this repo",
             "data_dir": "nable data dir"}.get(m.dir_source, m.dir_source)
    print(f"Org model: {m.dir} ({where})" + ("" if exists else ", not created yet: "
                                             "run `nable org init`"))
    for layer in m.layers[1:]:
        print(f"  read under it: {layer.dir} (nable data dir)")
    if untrusted:
        print("  not trusted: this repo's nable.org/ came with the repo. Its owners do not "
              "pick the guard's team and its thresholds may only lower limits; "
              "`nable org trust --here` if it is yours.")
    c = m.status_counts()
    print(f"  facts: {c['confirmed']} confirmed, {c['proposed']} proposed, "
          f"{c['rejected']} rejected, {c['expired']} expired")
    for kind, kc in sorted(m.kind_counts().items()):
        print(f"    {kind}: " + ", ".join(f"{n} {s}" for s, n in kc.items() if n))
    print(f"  coverage: {cov['summary']}")
    if freezes:
        print(f"  freezes: {len(freezes)}")
        for f, state, how in freezes:
            v = f.value
            print(f"    {f.key}  {f.subject}  {v['start']} to {v['end']}  {state}, "
                  f"{how}: {v['reason']}")
    if approvals:
        print(f"  approvals: {len(approvals)}")
        for f in approvals:
            v = f.value
            sure = "confirmed" if m._sure(f, True) else (
                "proposed" if not f.confirmed else "not trusted, names nobody")
            print(f"    {f.key}  {f.subject}  {', '.join(v['action_classes'])}: "
                  f"{v.get('min', 1)} of {', '.join(v['approvers'])}"
                  + (", change ticket required" if v.get("change_ticket") else "")
                  + f"  ({sure})")
    if cov["basis"] == "subjects" and cov["subjects"]["total"]:
        s = cov["subjects"]
        print(f"    subjects: {s['confirmed_owner']} of {s['total']} with a confirmed owner, "
              f"{s['proposed_owner']} proposed")
    if stale:
        print(f"  stale (past review_after, still used): {len(stale)}")
        for f in stale[:10]:
            print(f"    {f.key}  {f.subject}  review was due {f.review_after}")
    if conflicts:
        print(f"  conflicts (a fact disagrees with a confirmed one): {len(conflicts)}")
        for w, p in conflicts[:10]:
            if p.confirmed:
                print(f"    {p.key} is also confirmed, as {p.value} for {p.subject}; {w.key} "
                      f"says {w.value} and answers. Keep one: nable org confirm KEY")
            else:
                print(f"    {p.key} proposes {p.value} for {p.subject}; confirmed {w.key} "
                      f"says {w.value}")
    if loose:
        print(f"  repo paths that name no repo: {len(loose)} ({', '.join(str(f.subject) for f in loose[:3])}"
              f"{', ...' if len(loose) > 3 else ''}). Fix: rewrite each id as "
              "repo_path:<repo>//<path> (`nable org set owner --subject repo_path:PATH` "
              "run inside the repo does), or move them into that repo's nable.org/.")
    for w in m.warnings:
        print(f"  warning: {w}", file=sys.stderr)
    return 0


def _freeze_rows(m) -> list[tuple[Any, str, str]]:
    """(fact, "in force" | "upcoming", what the guard does with it) for each
    live freeze that has not ended, the soonest to end first."""
    from datetime import UTC, datetime

    from .model import FactError, parse_when
    now = datetime.now(UTC)
    rows = []
    for f in m.freezes():
        try:
            start = parse_when(f.value.get("start"))
            end = parse_when(f.value.get("end"))
        except FactError:
            continue
        if end <= now:
            continue
        sure = m._sure(f, True)
        mode = f.value.get("mode", "ask")
        how = (("denies" if mode == "deny" else "asks") if sure
               else "asks (proposed" + (", not trusted" if f.confirmed else "")
               + ": a guess may only ask)")
        rows.append((f, "in force" if start <= now else "upcoming", how))
    return rows


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


def _bulk_keys(parsed, org, verb: str) -> tuple[list[str], str] | int | None:
    """The keys a --*-bulk flag names and what it was; an exit code when the
    set is not the one the question showed (printed, with the command for
    the set as it is now); None without a bulk flag."""
    import shlex

    from .questions import _label, bulk_digest, split_bulk
    for attr, name in (("org_owner_bulk", "owner"), ("org_env_bulk", "env"),
                       ("org_kind_bulk", "kind")):
        val = getattr(parsed, attr, None)
        if not val:
            continue
        what, digest = split_bulk(val)
        m = org.load(parsed.org_dir)
        facts = org.bulk_facts(m, **{name: what})
        keys = [f.key for f in facts]
        now = bulk_digest(keys)
        if digest == now and keys:
            return keys, f"--{name}-bulk {what}"
        if not keys:
            print(f"  --{name}-bulk {what}: nothing proposed to {verb}", file=sys.stderr)
            return 1
        why = ("a bulk answer needs the digest its question printed" if digest is None
               else "the proposals in this group changed since the question was shown")
        print(f"  --{name}-bulk {what}: not decided, {why}. The group is now "
              f"{len(facts)} fact(s):", file=sys.stderr)
        for f in facts:
            print(f"    {f.key}  {_label(m, f)}: {f.fact} {f.value}  ({f.source})",
                  file=sys.stderr)
        print(f"  To {verb} exactly these: nable org {verb} --{name}-bulk "
              f"{shlex.quote(what + '@' + now)}", file=sys.stderr)
        return 1
    return None


def _decide(parsed, org, verb: str) -> int:
    who = _who(parsed.org_as)
    if who is None:
        return _need_human()
    bulk = _bulk_keys(parsed, org, verb)
    if isinstance(bulk, int):
        return bulk
    if bulk is None and not parsed.org_keys:
        print(f"nable org {verb}: give KEY..., or --owner-bulk TEAM@DIGEST, --env-bulk "
              "ENV@DIGEST or --kind-bulk KIND@DIGEST (as `nable org questions` prints them)",
              file=sys.stderr)
        return 2
    if bulk is not None:
        keys, what = bulk
        many = org.confirm_many if verb == "confirm" else org.reject_many
        done = many(keys, who, parsed.org_dir)
        print(f"  {what}: {len(done)} fact(s) {done[0].status} by {who}")
        for f in done:
            print(f"    {f.key}  {f.fact} {f.subject} {f.value}")
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
    if kind in ("freeze", "approval"):
        built = _policy_value(parsed, kind)
        if isinstance(built, int):
            return built
        subject, value = parsed.org_subject, built
    elif kind == "threshold":
        if not parsed.org_subject:
            print("set threshold needs --subject team:TEAM, environment:ENV or org:org",
                  file=sys.stderr)
            return 2
        subject = parsed.org_subject
        value = {k: v for k, v in (("max_auto_monthly_usd", parsed.org_max_auto),
                                   ("velocity_cap_usd", parsed.org_velocity_cap))
                 if v is not None}
    elif kind == "team":
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
        subject = _qualified(org, org.subject_of(subject), parsed.org_dir)
        fact = org.Fact.from_dict({"fact": kind, "subject": subject.to_dict(),
                                   "value": value, "source": "human", "status": "confirmed",
                                   "review_after": parsed.org_review_after})
        f = org.set_fact(fact, who, parsed.org_dir)
    except (org.FactError, org.OrgError) as e:
        print(f"Not set: {e}", file=sys.stderr)
        return 1
    print(f"  {f.key}  confirmed by {who}: {f.fact} {f.subject} {f.value}")
    return 0


def _when(raw: str, zone: str | None) -> str:
    """An ISO 8601 time with its UTC offset: as written when it has one,
    else read in `zone` (an IANA name) or this machine's time zone."""
    from datetime import datetime
    dt = datetime.fromisoformat(raw.strip())
    if dt.tzinfo is None:
        if zone:
            from zoneinfo import ZoneInfo
            dt = dt.replace(tzinfo=ZoneInfo(zone))
        else:
            dt = dt.astimezone()
    return dt.isoformat()


def _policy_value(parsed, kind: str) -> dict[str, Any] | int:
    """The value of a freeze or approval fact from the flags, or an exit
    code (the problem printed)."""
    if not parsed.org_subject:
        what = ("org:org, team:TEAM, environment:ENV or aws_account:ID" if kind == "freeze"
                else "team:TEAM or environment:ENV")
        print(f"set {kind} needs --scope {what}", file=sys.stderr)
        return 2
    if kind == "freeze":
        missing = [n for n, v in (("--start", parsed.org_start), ("--end", parsed.org_end),
                                  ("--reason", parsed.org_reason)) if not v]
        if missing:
            print(f"set freeze needs {', '.join(missing)}", file=sys.stderr)
            return 2
        try:
            if parsed.org_tz:
                from zoneinfo import ZoneInfo
                ZoneInfo(parsed.org_tz)          # a zone nobody can read is refused
            return {"start": _when(parsed.org_start, parsed.org_tz),
                    "end": _when(parsed.org_end, parsed.org_tz),
                    "reason": parsed.org_reason, "mode": parsed.org_mode or "ask"}
        except (ValueError, KeyError) as e:
            # An unknown --tz is a ZoneInfoNotFoundError, which is a KeyError.
            print(f"Not set: {e}", file=sys.stderr)
            return 1
    from ..policy import ONE_WAY_DOORS, TWO_WAY_DOORS
    classes = [c.strip() for c in parsed.org_action_classes or [] if c.strip()]
    if not classes or not parsed.org_approvers:
        print("set approval needs --action-class CLASS and --approver KIND:ID (each "
              "repeatable)", file=sys.stderr)
        return 2
    known = {*TWO_WAY_DOORS, *ONE_WAY_DOORS, "*"}
    unknown = [c for c in classes if c.lower() not in known]
    if unknown:
        print(f"Not set: {unknown[0]!r} is not an action class nable knows ("
              f"{', '.join(sorted(known - {'*'}))}, or '*')", file=sys.stderr)
        return 1
    value: dict[str, Any] = {"action_classes": classes, "approvers": list(parsed.org_approvers),
                             "min": parsed.org_min if parsed.org_min is not None else 1}
    if parsed.org_change_ticket:
        value["change_ticket"] = True
    return value


def _qualified(org, s, org_dir):
    """A bare repo_path about the repo holding the working directory, with
    that repo named, unless the fact goes into the repo's own nable.org/: a
    path in the data dir's model must say which repo it is in."""
    from .model import split_repo_path
    if s.kind != "repo_path" or split_repo_path(s.id)[0] is not None:
        return s
    d, _ = org.resolve_dir(org_dir)
    root = org.git_root()
    if root is None:
        return s
    try:
        if d.resolve() == (root / org.ORG_DIR_NAME).resolve():
            return s
    except OSError:
        pass
    return org.subject_of(f"repo_path:{org.repo_identity(root)}//{s.id}")


def _trust(parsed, org) -> int:
    root = org.git_root()
    if not getattr(parsed, "org_here", False) and not getattr(parsed, "org_revoke", False):
        rows = org.trusted_repos()
        here = "not in a git repo" if root is None else (
            f"{root}: {'trusted' if org.is_trusted(root) else 'not trusted'}")
        print(f"This repo: {here}")
        for r in rows:
            print(f"  trusted: {r['root']}" + (f" ({r['remote']})" if r.get("remote") else "")
                  + f", by {r.get('by', '?')} on {r.get('at', '?')}")
        if root is not None and not org.is_trusted(root):
            print("  Trust it: nable org trust --here")
        return 0
    if root is None:
        print("nable org trust: run it inside the repo whose nable.org/ you trust.",
              file=sys.stderr)
        return 2
    who = _who(parsed.org_as)
    if who is None:
        return _need_human()
    revoke = bool(getattr(parsed, "org_revoke", False))
    org.trust(root, who, revoke=revoke)
    remote = org.remote_url(root)
    print(f"  {'No longer trusted' if revoke else 'Trusted'}: {root}"
          + (f" ({remote})" if remote else "") + f", by {who}.")
    return 0


def _learn(d, *, out=None) -> None:
    """Read the guard's decision ledger for what people keep deciding and
    propose the thresholds it supports (learning.policy_inference): the
    proposals then come up as questions like any other. Proposals only, and
    never on the hook path. A ledger or model that cannot be read costs the
    lessons, never the questions."""
    out = out or sys.stdout
    try:
        from ..recommendations.learning.policy_inference import propose_guard_facts
        from .store import _data_dir, resolve_dir
        if d is None and resolve_dir(None)[1] == "repo":
            # As init does: never into a repo's tracked files unasked; the
            # data dir's model is read under the repo's.
            d = _data_dir() / "org"
        got = propose_guard_facts(d)
    except Exception as e:  # noqa: BLE001 - lessons are optional; questions are not
        print(f"  (the guard's ledger was not read for lessons: {type(e).__name__}: {e})",
              file=sys.stderr)
        return
    new = [p for p in got["proposals"] if p.get("result") in ("added", "conflict")]
    if new:
        print(f"  learned from the guard's asks: {len(new)} threshold proposal(s), asked below "
              "(nable learn infer --dry-run shows the evidence)", file=out)


def _questions(parsed, org) -> int:
    as_json = getattr(parsed, "org_json", False)
    _learn(parsed.org_dir, out=sys.stderr if as_json else sys.stdout)
    qs = org.questions(parsed.org_limit, model=org.load(parsed.org_dir))
    if as_json:
        print(json.dumps({"questions": [q.to_dict() for q in qs]}, default=str))
        return 0
    if not qs:
        print("No questions: nothing proposed, stale or unowned.")
        return 0
    for i, q in enumerate(qs, 1):
        _print_question(i, q)
    return 0


def _print_question(i: int, q) -> None:
    print(f"  {i}. {q.text}")
    # A bulk answer decides every fact listed here, so every one is shown.
    for item in q.items:
        print(f"       {item}")
    print(f"     yes: {q.command}" + (f"    no: {q.no_command}" if q.no_command else ""))


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


def _one_by_one(q, org, d, who: str) -> bool:
    """A bulk question answered item by item: y, n or s(kip) each. False
    when the person quits."""
    m = org.load(d)
    from .questions import describe
    for key in q.keys:
        found = m.find(key)
        if not found:
            continue
        ans = _ask(f"     {describe(found[0])}? [Y/n/s/q] ")
        if ans is None or ans.lower() == "q":
            return False
        ans = (ans or "y").lower()[:1]
        if ans == "y":
            org.confirm(key, who, d)
        elif ans == "n":
            org.reject(key, who, d)
    return True


def _interview(qs, org, d, who: str) -> None:
    """Ask each question: y/n/edit with the default shown. 'q' stops. A bulk
    question's yes confirms every fact it lists, its no rejects them all, and
    edit walks through them one at a time."""
    for i, q in enumerate(qs, 1):
        print(f"\n  {i}. {q.text}")
        for item in q.items:
            print(f"       {item}")
        if q.kind == "bulk":
            ans = _ask("     yes / no / edit, one by one [Y/n/e/q] ")
            if ans is None or ans.lower() == "q":
                return
            ans = (ans or q.default).lower()[:1]
            if ans == "y":
                org.confirm_many(q.keys, who, d)
                print(f"     Confirmed {len(q.keys)} fact(s).")
            elif ans == "n":
                org.reject_many(q.keys, who, d)
                print(f"     Rejected {len(q.keys)} fact(s); they will not be proposed again.")
            elif ans == "e" and not _one_by_one(q, org, d, who):
                return
            continue
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


_RESULT = {"added": "new", "duplicate": "already there", "suppressed_rejected":
           "rejected before, not asked again", "conflict": "beside a confirmed fact",
           "invalid": "invalid, skipped"}


def _print_runs(runs) -> None:
    """What each adapter proposed: counts, and the top items by dollars."""
    from .questions import describe
    if not runs:
        return
    print("  adapters (they only propose; nothing here is confirmed until you say so):")
    for run in runs:
        if run.error:
            print(f"    {run.id}: failed, {run.error}")
            continue
        if not run.facts:
            print(f"    {run.id}: nothing to propose")
            continue
        counts = ", ".join(f"{n} {_RESULT.get(k, k)}" for k, n in sorted(run.counts.items()))
        print(f"    {run.id}: {len(run.facts)} proposed ({counts})")
        for f in run.top(3):
            usd = _fmt_usd(f.dollars_monthly)
            print(f"      {describe(f)}" + (f"  {usd}" if usd else "") +
                  f"  (confidence {f.confidence:.2f})")


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
        # Creating it is a person saying this repo's model is theirs.
        who = _who(parsed.org_as)
        if who is not None:
            org.trust(root, who)
            print(f"  trusted {root}'s nable.org/ (by {who})")
        else:
            print("  not trusted yet: `nable org trust --here` in a terminal, or with --as "
                  "WHO, lets its owners pick the guard's team and its thresholds raise limits",
                  file=sys.stderr)
    else:
        # Never the repo's tracked files: a repo's nable.org/ is written by
        # `init --here`, and nable's own facts (tag_rules.yaml, accounts.yaml)
        # are not the repo's to commit.
        d, why = org.resolve_dir(parsed.org_dir)
        if why == "repo":
            from .store import _data_dir
            d = _data_dir() / "org"
    from .store import ensure_dir
    made = ensure_dir(d)
    print(f"Org model: {d}" + (f" (created {len(made)} files)" if made else ""))
    added = org.import_legacy(d)
    print(f"  imported {added} fact(s) nable already had (tag_rules.yaml, accounts.yaml, "
          "FINOPS_REQUIRED_TAGS/FINOPS_PROTECTED_TAGS)")
    if not getattr(parsed, "org_no_adapters", False):
        _print_runs(org.run_adapters(d, repos=getattr(parsed, "org_repos", None) or ()))
    _learn(d)
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
            _print_question(i, q)
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
        if action == "trust":
            return _trust(parsed, org)
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
