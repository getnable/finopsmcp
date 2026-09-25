"""`nable why`: why AWS cost went up, down to the resource and the change.

    nable why [--days 7] [--service EC2] [--json] [--no-cloudtrail]

Compares the last N days with the N before, picks the AWS services that rose
the most (or the one named), and for each prints one line per usage type
that moved:

    EC2 +$1,240/mo since Sep 21: 8x p4d.24xlarge launched by
    arn:aws:iam::123456789012:role/ci via terraform (guard: allowed at
    $128k/mo, session s1) [confirmed (resource id match)]

then what could not be read, and what the run cost. The attribution rules
are anomaly/root_cause.py's: a change is named only when its resource (or
usage type) and time line up with the cost, and is labelled "confirmed" only
on a resource id match.

Cost Explorer bills $0.01 a request. This makes one request to rank the
services, then about two per service (usage types, then resources), and says
so before the first one. CloudTrail LookupEvents is free.

Exit codes: 0 answered (including "nothing rose"), 1 could not answer (no
credentials, Cost Explorer denied, or every service drilled into failed),
2 usage.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

EXIT_OK = 0
EXIT_ERROR = 1
_MAX_SERVICES = 3
_MIN_SERVICE_DELTA_USD = 1.0


def add_parser(sub) -> None:
    p = sub.add_parser(
        "why",
        help="Why AWS cost went up: the resource, and the change that started it",
    )
    p.add_argument("--days", type=int, default=7, metavar="N",
                   help="compare the last N days with the N before (default 7, max 45)")
    p.add_argument("--service", default=None, metavar="NAME",
                   help="one service (EC2, RDS, or its Cost Explorer name); "
                        "default: the services that rose the most")
    p.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    p.add_argument("--no-cloudtrail", action="store_true",
                   help="skip CloudTrail: the usage types and resources only")


def _tty() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _tty() else s


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _tty() else s


def resolve_service(wanted: str, names: list[str]) -> str:
    """"EC2", "rds" or a Cost Explorer name -> the Cost Explorer name. Falls
    back to what was typed, so an exact name nobody spent on still reads."""
    from .anomaly.root_cause import short_name
    from .connectors.universal import _AWS_ALIASES

    w = wanted.strip()
    low = w.lower()
    for n in names:
        if n.lower() == low or short_name(n).lower() == low:
            return n
    prefix = _AWS_ALIASES.get(low)
    if prefix:
        hits = [n for n in names if n.startswith(prefix)]
        if hits:
            return hits[0]
    hits = [n for n in names if low in n.lower()]
    return hits[0] if hits else w


def _signed(v: Any) -> str:
    x = float(v or 0.0)
    return f"{'-' if x < 0 else '+'}${abs(x):,.0f}"


def _fail(msg: str, as_json: bool, code: int = EXIT_ERROR) -> int:
    if as_json:
        print(json.dumps({"error": {"message": msg, "exit_code": code}}))
    else:
        print(f"nable why: {msg}", file=sys.stderr)
    return code


def run(args, *, session: Any = None) -> int:
    as_json = bool(getattr(args, "json", False))
    days = int(getattr(args, "days", 7) or 7)
    if not 1 <= days <= 45:
        return _fail("--days takes 1 to 45 (Cost Explorer's daily data, and CloudTrail's "
                     "90 days of history, bound how far back this can compare)", as_json, 2)
    wanted = getattr(args, "service", None)
    read_ct = not getattr(args, "no_cloudtrail", False)

    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

    from .anomaly import drilldown
    from .anomaly.root_cause import combine, explain
    from .billing_access import BillingAccessError

    if session is None:
        import boto3
        session = boto3.Session()
    try:
        meter = drilldown.cost_explorer(session)
    except BillingAccessError as exc:
        return _fail(str(exc), as_json)
    current, baseline = drilldown.windows_for_period(days)
    note = sys.stderr if as_json else sys.stdout
    most = 1 + 2 * (1 if wanted else _MAX_SERVICES)
    if not as_json:
        print(_bold(f"nable why · the last {days} day(s) against the {days} before"))
    print(_dim(f"Cost Explorer: 1 request to rank services, then about 2 per service, so "
               f"{'about' if wanted else 'up to about'} {most} requests, "
               f"${most * drilldown.CE_REQUEST_USD:.2f} on your AWS bill ($0.01 each; a "
               f"large account's extra pages add more, and the total is printed at the end). "
               f"CloudTrail reads are free."), file=note)

    try:
        ranked = drilldown.service_deltas(meter, current, baseline)
    except NoCredentialsError:
        return _fail("no AWS credentials found (AWS_PROFILE, ~/.aws, or the environment)",
                     as_json)
    except ClientError as exc:
        if drilldown.is_denied(exc):
            return _fail("Cost Explorer refused GetCostAndUsage: the credentials need "
                         "ce:GetCostAndUsage (and ce:GetCostAndUsageWithResources for "
                         "resource ids)", as_json)
        return _fail(f"Cost Explorer failed: {exc}", as_json)
    except BotoCoreError as exc:
        return _fail(f"Cost Explorer failed: {exc}", as_json)

    names = [s["service"] for s in ranked["services"]]
    if wanted:
        services = [resolve_service(wanted, names)]
    else:
        services = [s["service"] for s in ranked["services"]
                    if s["delta_usd"] >= _MIN_SERVICE_DELTA_USD][:_MAX_SERVICES]

    results = [explain(svc, current, baseline, session=session, meter=meter,
                       read_cloudtrail=read_ct) for svc in services]
    block = combine(results)
    block["cost_explorer_requests"] = meter.requests
    block["cost_note"] = meter.note()
    if ranked["truncated"]:
        block["not_read"].insert(0, "Cost Explorer had more pages of services than were read; "
                                    "smaller services may be missing from the ranking.")
    # Every service drilled into failed and none answered: that is "could not
    # answer" (exit 1), not an answer, even though each failure is printed.
    code = (EXIT_ERROR if results and all(r.get("error") and not r.get("rows")
                                           for r in results) else EXIT_OK)

    if as_json:
        print(json.dumps({"command": "why", "ok": code == EXIT_OK, "current": current.as_dict(),
                          "baseline": baseline.as_dict(), "services_ranked": ranked["services"][:10],
                          **block}, indent=2, default=str))
        return code

    print()
    if not services:
        print(f"No AWS service rose by ${_MIN_SERVICE_DELTA_USD:.0f} or more between "
              f"{baseline.start} to {baseline.end} and {current.start} to {current.end}.")
    for r in results:
        for line in r["lines"]:
            print(f"  {line}")
        for row in r["rows"]:
            res = row.get("resources") or []
            if res:
                shown = ", ".join(f"{x['resource_id']} ({_signed(x['delta_usd'])})"
                                  for x in res[:3])
                print(_dim(f"      resources: {shown}"))
            others = [c for c in row.get("changes") or [] if not c.get("attribution")]
            for c in others[:2]:
                print(_dim(f"      not the cause: {c['event']} at {c['time']} by "
                           f"{c.get('who') or '?'} ({c['not_attributed_because']})"))
    if block["not_read"]:
        print()
        print(_bold("  Not read"))
        for n in block["not_read"]:
            print(f"    - {n}")
    print()
    print(_dim(f"  {block['cost_note']} {block['lookup_calls']} CloudTrail LookupEvents "
               f"call(s), free."))
    print(_dim("  'likely' means the usage type, region and time line up but no resource id "
               "ties them; 'confirmed' means one does."))
    return code
