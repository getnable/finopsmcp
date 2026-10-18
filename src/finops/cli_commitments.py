# SPDX-License-Identifier: Apache-2.0
"""`nable commitments status`: the reservations and Savings Plans this account
holds, and when each one ends.

    nable commitments status [--regions R ...] [--json] [--spend]
                             [--fail-within DAYS] [--dry-run]

Free by default: Describe calls only (commitments_status.py lists them, and
`--dry-run` prints them with their IAM actions without reading credentials).
Each RDS, EC2, ElastiCache, Redshift, OpenSearch and MemoryDB reservation and
each Savings Plan is listed with its end date, days left, state and charges as
AWS reports them, and RDS, EC2 and ElastiCache ones with "matched now" against
what is running this minute. A read that failed is listed as not read, with
its reason; it is never a zero. A service botocore lists no endpoint for in a
region is not called there and is listed as not offered.

`--spend` adds billed utilization from Cost Explorer, which AWS bills at $0.01
a request: what it can cost is printed on stderr before the first request,
and its pages are capped.

Exit codes:
    0  every read succeeded
    3  credentials expired
    4  every commitment read was denied
    5  some reads failed (each is listed as not read)
    6  no credentials, or AWS rejected them
With --fail-within DAYS the code is the gate's instead:
    1  a commitment that was read ends within DAYS (this wins)
    2  a commitment read failed, so it cannot check (no credentials included)
    0  every commitment read succeeded and none ends within DAYS
argparse's 2 is a usage error, and so is a --regions value that is not a
region code.

With --json, stdout is one document whatever happens, a failure included;
progress goes to stderr. Nothing reads stdin, and there are no escape codes
unless stdout is a terminal.

Under the telemetry consent that already exists, and only then, a run that
read or tried to sends one commitments_status_run count (finops.telemetry):
its exit class, how many commitments were read, how many of those end within
90 days, how many services were not read, and whether --json was on. A
--dry-run or a usage error sends nothing. No id, ARN, charge or region leaves.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from typing import Any

from . import commitments_status as cs

EXIT_OK = 0
EXIT_ENDING = 1
EXIT_CANNOT_CHECK = 2
EXIT_EXPIRED = 3
EXIT_DENIED = 4
EXIT_PARTIAL = 5
EXIT_NO_CREDS = 6
EXIT_CANCELLED = 130

# What each code means, in the words docs/HEADLESS.md uses for it
# (tests/test_commitments_status.py holds the two together).
EXIT_CODES = {
    EXIT_OK: "every read succeeded",
    EXIT_ENDING: "a commitment that was read ends within DAYS",
    EXIT_CANNOT_CHECK: "a commitment read failed, so it cannot check",
    EXIT_EXPIRED: "credentials expired",
    EXIT_DENIED: "every commitment read was denied",
    EXIT_PARTIAL: "some reads failed",
    EXIT_NO_CREDS: "no credentials, or AWS rejected them",
}

# The commitment services each run reads: the reservations in each region,
# and the Savings Plans once.
SERVICES_READ = len(cs.COMMITMENT_READS) + 1
# What the usage count calls ending soon.
_ENDING_DAYS = 90

_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d+$")
_GRANT = ("`nable iam-template` grants them in its OptionalCommitments statement "
          "(parameter IncludeCommitments), Describe only")


def exit_class(code: int) -> int:
    """The usage count's three classes: 0 every read succeeded (and, with
    --fail-within, nothing ends inside), 1 a commitment read ends within
    DAYS, 2 something could not be read or checked (exits 2 to 6)."""
    return 0 if code == EXIT_OK else 1 if code == EXIT_ENDING else 2


def _count_run(code: int, commitments: list[dict], missed: list[cs.NotRead] | None, *,
               as_json: bool) -> None:
    """The commitments_status_run usage count, sent only under a telemetry
    consent that named it (telemetry.send_usage decides, and drops anything
    off its allow-list). Counts only: what was read never leaves.
    `missed` None means nothing was read, so every service is not read."""
    try:
        from . import telemetry
        ending = sum(1 for c in commitments if cs.is_live(c)
                     and isinstance(c.get("days_left"), int)
                     and 0 <= c["days_left"] <= _ENDING_DAYS)
        not_read = (SERVICES_READ if missed is None else
                    len({n.service for n in missed if n.what == "commitments"}))
        telemetry.send_usage("commitments_status_run", {
            "exit_class": exit_class(code), "commitments_read": len(commitments),
            "ending_within_90_days": ending, "services_not_read": not_read,
            "json": as_json})
    except Exception:  # noqa: BLE001, S110 - a count is never worth a failed command
        pass


def _days(raw: str) -> int:
    try:
        n = int(raw)
    except ValueError:
        n = -1
    if n < 0:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a whole number of days, 0 or more")
    return n


def add_parser(sub) -> None:
    """Register `commitments status` on the wizard's argparse tree."""
    p = sub.add_parser(
        "commitments",
        help="Reservations and Savings Plans this account holds, and when each ends, free",
    )
    actions = p.add_subparsers(dest="commitments_action", metavar="<action>")
    s = actions.add_parser(
        "status",
        help="list every reservation and Savings Plan, with end dates (free reads)",
        description=("Every RDS, EC2, ElastiCache, Redshift, OpenSearch and MemoryDB "
                     "reservation and every Savings Plan this account holds, with its end "
                     "date, days left, state and charges as AWS reports them. Free Describe "
                     "calls only, unless --spend."),
        epilog="exit codes:\n" + "\n".join(f"  {k}  {v}" for k, v in EXIT_CODES.items()),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    s.add_argument("--regions", nargs="+", action="extend", metavar="REGION",
                   help="read exactly these regions instead of every enabled one "
                        "(space or comma separated; repeat the flag to add more)")
    s.add_argument("--json", action="store_true", help="one JSON document on stdout")
    s.add_argument("--spend", action="store_true",
                   help="add billed utilization from Cost Explorer ($0.01 a request, at most "
                        f"{2 * cs.CE_MAX_PAGES} requests; the cost is printed first)")
    s.add_argument("--fail-within", type=_days, default=None, metavar="DAYS",
                   help="exit 1 when a commitment ends within DAYS, 2 when one could not be "
                        "read, else 0")
    s.add_argument("--dry-run", action="store_true",
                   help="list every call and IAM action, read no credentials, and exit")


# ── output ────────────────────────────────────────────────────────────────────

def _colour(stream: Any) -> bool:
    try:
        tty = bool(stream.isatty())
    except (AttributeError, ValueError):
        tty = False
    return tty and not os.getenv("NO_COLOR") and os.getenv("TERM") != "dumb"


class _Ink:
    def __init__(self, stream: Any) -> None:
        self.on = _colour(stream)

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text


def _split_regions(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        for r in re.split(r"[,\s]+", v.strip().lower()):
            if r and r not in out:
                out.append(r)
    return out


def _report(now: datetime, days: int | None) -> dict[str, Any]:
    return {"command": "nable commitments status", "account": None,
            "scope": "this account only", "as_of": now.astimezone(UTC).isoformat(),
            "regions": [], "commitments": [], "not_read": [], "not_offered": [], "summary": {},
            "utilization": {"basis": "matched now", "note": cs.MATCHED_NOW_NOTE},
            "savings_plans_note": cs.SAVINGS_PLANS_NOTE, "billed_utilization": None,
            "gate": None if days is None else {"days": days, "ending": [], "cannot_check": True},
            "exit_code": None, "error": None}


def _fail(report: dict[str, Any], klass: str, code: int, lines: list[str], *, out: Any,
          err: Any, as_json: bool, days: int | None) -> int:
    if days is not None:
        code = EXIT_CANNOT_CHECK
        lines = [*lines, (f"  --fail-within {days}: nothing was read, so it cannot check "
                          f"(exit {EXIT_CANNOT_CHECK})")]
    report.update(exit_code=code, error={"class": klass, "exit_code": code,
                                         "message": lines[0].strip()})
    for line in lines:
        print(line, file=err if as_json else out)
    if as_json:
        print(json.dumps(report, indent=2, default=str), file=out)
    return code


def _dry_run(spend: bool, *, out: Any, as_json: bool) -> int:
    plan = cs.dry_run_plan(spend)
    if as_json:
        print(json.dumps({"dry_run": True, "executed": False, **plan}, indent=2), file=out)
        return EXIT_OK
    lines = ["nable commitments status --dry-run",
             "Nothing below was executed. This is what the command would call.", ""]
    for c in plan["calls"]:
        billed = "  billed $0.01 a request" if c["billed"] else ""
        lines.append(f"  {c['call']:<44} {c['iam_action']}")
        lines.append(f"      {c['what']}, {c['where']}{billed}")
    lines.append("")
    if spend:
        lines.append(f"Cost Explorer: at most {plan['cost_explorer_max_requests']} requests at "
                     "$0.01 each, printed on stderr before the first one.")
    else:
        lines.append("Cost Explorer is NOT called. `--spend` adds billed utilization, and each "
                     "request is billed to your account at $0.01.")
    lines += ["", (f"{len(plan['iam_actions'])} IAM actions, every one a Describe or Get. "
                   "Nothing here can buy or change a commitment.")]
    print("\n".join(lines), file=out)
    return EXIT_OK


def _charges(c: dict[str, Any]) -> str:
    ch = c.get("charges") or {}
    if c["service"] == "savingsplans":
        parts = [f"commitment {ch['commitment']} {ch.get('currency', '')}/hr".replace(" /", "/")
                 ] if "commitment" in ch else []
        parts += [f"{k} {ch[k]}" for k in ("upfrontPaymentAmount", "recurringPaymentAmount")
                  if k in ch]
        return ", ".join(parts) or "none reported"
    parts = []
    if "FixedPrice" in ch:
        parts.append(f"FixedPrice {ch['FixedPrice']}"
                     + (f" {ch['CurrencyCode']}" if ch.get("CurrencyCode") else ""))
    if "UsagePrice" in ch:
        parts.append(f"UsagePrice {ch['UsagePrice']}")
    rec = ch.get("RecurringCharges")
    if rec is not None:
        each = [f"{r.get('RecurringChargeAmount', r.get('Amount'))} "
                f"{r.get('RecurringChargeFrequency', r.get('Frequency'))}" for r in rec]
        parts.append("RecurringCharges " + ("; ".join(each) if each else "none"))
    return ", ".join(parts) or "none reported"


def _ends(c: dict[str, Any]) -> str:
    end, left = c.get("end"), c.get("days_left")
    if not end:
        return "end not reported"
    day = end[:10]
    if left is None:
        return f"ends {day}"
    if cs.is_live(c) and left >= 0:
        return f"ends {day} ({left} day{'s' if left != 1 else ''} left)"
    if left < 0:
        return f"ended {day} ({-left} day{'s' if left != -1 else ''} ago)"
    return f"ended, its term ran to {day}"


def _what(c: dict[str, Any]) -> str:
    bits = [str(c.get("class") or "?")]
    if isinstance(c.get("count"), int):
        bits[0] += f" x{c['count']}"
    if c["service"] != "savingsplans" and c.get("product"):
        bits.append(str(c["product"]))
    if c.get("multi_az"):
        bits.append("Multi-AZ")
    return " ".join(bits)


def _header(report: dict[str, Any], stream: Any) -> None:
    """The first lines, printed before the reads (on stderr with --json)."""
    ink = _Ink(stream)
    acct = report.get("account") or "not read"
    print(f"{ink('nable commitments status', '1')} "
          f"{ink(f'· account {acct} · this account only', '2')}", file=stream)
    regions = report["regions"]
    print(f"reading {len(regions)} region{'s' if len(regions) != 1 else ''}: "
          f"{', '.join(regions)} (free Describe calls)", file=stream)


def _render(report: dict[str, Any], out: Any, code: int) -> None:
    ink = _Ink(out)
    print(file=out)
    rows = report["commitments"]
    missed = report["not_read"]
    if not rows:
        print("  nothing to list from the reads that answered" if missed else
              "  this account holds no reservations or Savings Plans in the regions read",
              file=out)
    for c in rows:
        line = (f"  {c['service']:<12} {c['id']:<22} {c['region']:<14} {_what(c):<34} "
                f"{', '.join(x for x in (c.get('offering_type'), c.get('term')) if x):<26} "
                f"{_ends(c)}  {c.get('state')}")
        mn = c.get("matched_now")
        if mn and mn.get("compared") is False:
            line += "  matched now not compared"
        elif mn and mn.get("read"):
            line += f"  matched now {mn['matched']} of {mn['reserved']}"
        elif mn:
            line += f"  matched now not read ({mn['reason']})"
        bu = c.get("billed_utilization")
        if bu and bu.get("UtilizationPercentage") is not None:
            line += f"  billed {bu['UtilizationPercentage']}% (Cost Explorer)"
        print(line, file=out)
        print(ink(f"      charges as AWS reports them: {_charges(c)}", "2"), file=out)
    if missed:
        print(file=out)
        print(ink("not read (nothing was learned there; it is not a zero):", "33"), file=out)
        for n in missed:
            page = f" from page {n['page']}" if n.get("page") else ""
            note = ("; matched now is not known there" if n["what"] == "running now" else "")
            print(f"  {n['service']:<12} {n['region']:<14} {n['reason']} ({n['code']}){page}, "
                  f"needs {n['iam_action']}{note}", file=out)
        if any(n["reason"] == "denied" and n["what"] == "commitments" for n in missed):
            lead = ("permission denied for every commitment read" if code == EXIT_DENIED
                    else "permission denied for some commitment reads")
            print(f"  {lead}: {_GRANT}", file=out)
    skipped = report.get("not_offered") or []
    if skipped:
        print(file=out)
        print(ink("not offered there, so not called (no endpoint in botocore's list; not a "
                  "failed read and not a zero):", "2"), file=out)
        for n in skipped:
            print(ink(f"  {n['service']:<12} {n['region']}", "2"), file=out)
    print(file=out)
    for note in (cs.MATCHED_NOW_NOTE, cs.SAVINGS_PLANS_NOTE):
        print(ink(textwrap.fill(note, 100, break_on_hyphens=False), "2"), file=out)
    billed = report.get("billed_utilization")
    if billed:
        period = billed["period"]
        print(ink(f"billed utilization: Cost Explorer, {period['start']} to {period['end']}, "
                  f"shown on each line it named"
                  + (f"; {len(billed['unmatched'])} more it named are in --json"
                     if billed["unmatched"] else "")
                  + (f"; only the first {cs.CE_MAX_PAGES} pages of each query were read"
                     if billed["truncated"] else ""), "2"), file=out)
    gate = report.get("gate")
    if gate is not None:
        days = gate["days"]
        if gate["ending"]:
            text = (f"--fail-within {days}: {len(gate['ending'])} commitment"
                    f"{'s' if len(gate['ending']) != 1 else ''} end within {days} days "
                    f"({', '.join(gate['ending'])})"
                    + ("; some others could not be read" if gate["cannot_check"] else ""))
        elif gate["cannot_check"]:
            text = (f"--fail-within {days}: nothing read ends within {days} days, but some "
                    "commitments could not be read, so it cannot check")
        else:
            text = f"--fail-within {days}: nothing ends within {days} days"
        print(f"{text} (exit {code})", file=out)


# ── the run ───────────────────────────────────────────────────────────────────

def _config() -> Any:
    try:
        from botocore.config import Config
    except ImportError:
        return None
    return Config(connect_timeout=5, read_timeout=30,
                  retries={"max_attempts": 4, "mode": "standard"})


def _resolve_regions(session: Any) -> tuple[list[str], cs.NotRead | None]:
    """Every enabled region, found and ordered the way `nable scan` finds
    them; when they cannot be listed, us-east-1 alone and why."""
    import logging

    from .cli_scan import _pick_regions
    quiet = logging.getLogger("finops.analyzers")
    level = quiet.level
    quiet.setLevel(logging.ERROR)
    try:
        regions = _pick_regions(None, session)
    finally:
        quiet.setLevel(level)
    code = getattr(regions, "error_code", None)
    found = [r for r in regions if isinstance(r, str)] or ["us-east-1"]
    if not code:
        return found, None
    _svc, call, action, what = cs.REGIONS_READ
    return found, cs.NotRead(service="ec2", region="us-east-1", call=call, iam_action=action,
                             what=what, reason=cs.reason_for(str(code)), code=str(code))


def _exit_code(commitments: list[dict], missed: list[cs.NotRead], attempted: int) -> int:
    """`attempted` is how many commitment reads were made: one per service
    offered in each region, and the Savings Plans read."""
    if not missed:
        return EXIT_OK
    reads = [n for n in missed if n.what == "commitments"]
    every = len({(n.service, n.region) for n in reads}) == attempted
    if every and not commitments:
        reasons = {n.reason for n in reads}
        if reasons == {"expired"}:
            return EXIT_EXPIRED
        if reasons == {"denied"}:
            return EXIT_DENIED
    return EXIT_PARTIAL


def _gate(commitments: list[dict], missed: list[cs.NotRead], days: int, now: datetime
          ) -> dict[str, Any]:
    window = now + timedelta(days=days)
    ending, unknown = [], False
    for c in commitments:
        if not cs.is_live(c):
            continue
        end = datetime.fromisoformat(c["end"]) if c.get("end") else None
        if end is None:
            unknown = True
        elif end <= window:
            ending.append(c["id"])
    cannot = unknown or any(n.what in ("commitments", "regions") for n in missed)
    return {"days": days, "ending": ending, "cannot_check": cannot}


def run(args: argparse.Namespace, *, session: Any = None, now: datetime | None = None,
        stdout: Any = None, stderr: Any = None) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    if getattr(args, "commitments_action", None) != "status":
        print("usage: nable commitments status [--regions R ...] [--json] [--spend] "
              "[--fail-within DAYS] [--dry-run]", file=err)
        return 2
    as_json = bool(getattr(args, "json", False))
    days = getattr(args, "fail_within", None)
    if getattr(args, "dry_run", False):
        return _dry_run(bool(getattr(args, "spend", False)), out=out, as_json=as_json)
    wanted = _split_regions(getattr(args, "regions", None))
    bad = [r for r in wanted if not _REGION_RE.match(r)]
    if bad:
        print(f"nable commitments status: not a region code: {', '.join(bad)} "
              "(for example: --regions us-east-1 eu-west-1)", file=err)
        return 2
    now = now or datetime.now(UTC)
    chrome = err if as_json else out
    report = _report(now, days)

    def fail(klass: str, code: int, lines: list[str]) -> int:
        code = _fail(report, klass, code, lines, out=out, err=err, as_json=as_json, days=days)
        _count_run(code, [], None, as_json=as_json)
        return code

    no_creds = [
        "no AWS credentials found on this machine",
        "  looked in: env vars, ~/.aws/credentials, ~/.aws/config (SSO), instance metadata",
        "  fix: `aws configure sso` (company SSO) or `aws configure` (access key)",
        ("  evaluating first? `nable commitments status --dry-run` lists every call and "
         "permission it needs, without running one"),
    ]
    try:
        if session is None:
            import boto3
            session = boto3.Session()
        creds = session.get_credentials()
    except Exception as exc:  # noqa: BLE001 - an unusable AWS setup is no credentials here
        return fail("no-credentials", EXIT_NO_CREDS,
                    [f"AWS credentials could not be loaded ({type(exc).__name__})", *no_creds[2:]])
    if creds is None:
        return fail("no-credentials", EXIT_NO_CREDS, no_creds)
    try:
        ident = session.client("sts", region_name="us-east-1").get_caller_identity()
        report["account"] = ident.get("Account")
    except Exception as exc:  # noqa: BLE001 - classified below; the reads say the rest
        reason = cs.reason_for(cs.error_code(exc), cs._status(exc))
        if reason == "expired":
            return fail("expired", EXIT_EXPIRED, [
                "your AWS session has expired",
                "  fix: `aws sso login` (or refresh your temporary credentials), then rerun"])
        if reason in ("no credentials", "credentials rejected"):
            return fail("no-credentials", EXIT_NO_CREDS, [
                "AWS rejected these credentials",
                ("  fix: `aws sts get-caller-identity` to confirm, then `aws configure` to "
                 "replace them")])

    missed: list[cs.NotRead] = []
    if wanted:
        regions = wanted
    else:
        regions, listing = _resolve_regions(session)
        if listing is not None:
            missed.append(listing)
    report["regions"] = regions
    _header(report, chrome)
    result = cs.read_all(session, regions, now=now, config=_config())
    missed += result["not_read"]
    commitments = cs.ordered(result["commitments"])
    if getattr(args, "spend", False):
        billed, failed = cs.read_billed(session, commitments, now=now,
                                        say=lambda line: print(line, file=err))
        missed += failed
        report["billed_utilization"] = billed
    code = _exit_code(commitments, missed, result["commitment_reads"])
    if days is not None:
        report["gate"] = _gate(commitments, missed, days, now)
        code = (EXIT_ENDING if report["gate"]["ending"] else
                EXIT_CANNOT_CHECK if report["gate"]["cannot_check"] else EXIT_OK)
    report.update(commitments=[cs.public(c) for c in commitments],
                  not_read=[n.to_dict() for n in missed], summary=result["summary"],
                  not_offered=result["not_offered"], exit_code=code)
    if as_json:
        print(json.dumps(report, indent=2, default=str), file=out)
    else:
        _render(report, out, code)
    _count_run(code, commitments, missed, as_json=as_json)
    return code


def main(args: argparse.Namespace) -> int:
    """run(), with an unexpected error said in one line and exit 1 rather
    than a traceback, and Ctrl-C as 130."""
    as_json = bool(getattr(args, "json", False))
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        if as_json:
            print(json.dumps({"error": {"class": "cancelled", "exit_code": EXIT_CANCELLED,
                                        "message": "cancelled"}}))
        return EXIT_CANCELLED
    except Exception as exc:  # noqa: BLE001 - a bug is reported, never a traceback
        line = (f"nable commitments status stopped on an unexpected error "
                f"({type(exc).__name__}); please report it: "
                "https://github.com/getnable/finopsmcp/issues/new")
        print(line, file=sys.stderr)
        if as_json:
            print(json.dumps({"error": {"class": "crash", "exit_code": 1, "message": line}}))
        return 1
