"""Why did cost spike: the resource, the change, and how sure the link is.

drilldown.py finds the usage types and resources behind a service's delta and
the day each started; change_events.py finds who changed what around that
day. This decides which change, if any, explains which delta, and words it:

    EC2 +$1,240/mo since Sep 21: 8x p4d.24xlarge launched by
    arn:aws:iam::123456789012:role/ci via terraform (guard: allowed at
    $128k/mo, session s1) [confirmed (resource id match)]

The rules are strict on purpose. An answer that names the wrong person for a
bill does more harm than one that says it does not know:

  - A change is attributed only when it is a call that starts this usage
    type's bill (RunInstances for BoxUsage, CreateNatGateway for NatGateway),
    in the same region, did not fail, and happened from 24 hours before the
    day the cost rose to the end of that day. A change after the cost had
    risen cannot have started it; one for a different instance type did not.
  - "confirmed (resource id match)": the change names a resource the billing
    data (the CUR, or resource-level Cost Explorer data) shows costing.
  - "likely": everything above lines up but no resource id ties them, because
    the billing data has none or the change names none. When both sides name
    resources and none are shared, the change launched something else and is
    not attributed, unless none of the billed resources it was compared with
    could have started the rise (they were costing before it, or the billing
    data has no baseline to tell), or the change is on a group (an Auto
    Scaling group, a fleet, an ECS service, an EKS node group) whose event
    names the group rather than what it launched: those are at most "likely".
  - Otherwise the row says no change lines up, and the changes that were
    found stay listed with the reason each was not attributed. It says so
    only when CloudTrail was read in full for the row; a row read in part,
    or not at all, says that instead.

Every answer says what could not be read: no CUR, resource-level Cost
Explorer data not enabled or too old, CloudTrail denied, a region not read,
calls capped. Nothing here raises.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from . import change_events, drilldown
from .drilldown import Meter, Window

CONFIRMED = "confirmed (resource id match)"
LIKELY = "likely"

RULES = (
    "A change is named as the cause only when it is a call that starts that usage "
    "type's bill, in the same region, that did not fail, made from 24 hours before "
    "the day the cost rose to the end of that day, and (where both are known) for "
    "the same instance type. 'confirmed (resource id match)' means the change names "
    "a resource the billing data shows costing; 'likely' means the rest lines up but "
    "no resource id ties them (a change to an Auto Scaling group, fleet, ECS service or "
    "EKS node group is at most 'likely': it names the group, not what it launched). "
    "Where nothing lines up the answer says so, and only when CloudTrail was read in "
    "full for that row."
)

# CloudTrail LookupEvents reads the account the credentials are in, nothing else.
ACCOUNT_NOTE = ("CloudTrail LookupEvents reads only the account these credentials are in, "
                "so a change made in another account of the organization is not seen here.")

_SHORT = {
    "Amazon Elastic Compute Cloud - Compute": "EC2",
    "EC2 - Other": "EC2-Other",
    "Amazon Relational Database Service": "RDS",
    "Amazon Simple Storage Service": "S3",
    "Amazon ElastiCache": "ElastiCache",
    "Amazon Elastic Kubernetes Service": "EKS",
    "Amazon Elastic Container Service": "ECS",
    "Amazon SageMaker": "SageMaker",
    "Amazon OpenSearch Service": "OpenSearch",
    "Amazon Elastic Load Balancing": "ELB",
    "Amazon Virtual Private Cloud": "VPC",
    "Amazon DynamoDB": "DynamoDB",
    "Amazon Redshift": "Redshift",
    "AWS Lambda": "Lambda",
    "Amazon Bedrock": "Bedrock",
}


def short_name(service: str) -> str:
    if service in _SHORT:
        return _SHORT[service]
    for prefix in ("Amazon ", "AWS "):
        if service.startswith(prefix):
            return service[len(prefix):]
    return service


def _when(text: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _day(text: Any) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return None


def _reason_not_attributed(row: dict[str, Any], ch: dict[str, Any], family: set[str],
                           onset: date | None) -> str | None:
    if ch.get("error_code"):
        return f"the call failed ({ch['error_code']})"
    if ch.get("event") not in family:
        return "not a call that starts this usage type's bill"
    when = _when(ch.get("time"))
    if onset is None or when is None:
        return "no day the cost rose to line it up with"
    start = datetime.combine(onset, datetime.min.time(), tzinfo=UTC)
    if when >= start + timedelta(days=1):
        return "made after the cost had already risen"
    if when < start - change_events.WINDOW:
        return "made more than 24 hours before the cost rose"
    if ch.get("region") and change_events.trail_region(row.get("region")) != ch["region"]:
        return f"in {ch['region']}, the cost is in {row.get('region')}"
    want, got = row.get("instance_type"), ch.get("instance_type")
    if want and got and want.lower() != got.lower():
        return f"for {got}, the cost is {want}"
    return None


def _could_start_rise(res: dict[str, Any], row_onset: date | None) -> bool:
    """Whether a billed resource could be what started the row's rise. One
    that was costing before it (its own onset is more than a day earlier),
    did not rise at all (an onset read as None), or was already there when
    data with no baseline begins, could not, so a change naming something
    else is not ruled out by it. With no onset field at all, it could."""
    if res.get("no_baseline") and not res.get("new_in_window"):
        return False
    if "onset" not in res:
        return True
    own = _day(res.get("onset"))
    if own is None:
        return False
    return row_onset is None or own >= row_onset - timedelta(days=1)


def attribute(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Mark each of the row's changes with `attribution` (CONFIRMED, LIKELY or
    None, with `not_attributed_because`), and return the attributed ones,
    best first: confirmed before likely, then nearest the onset."""
    family = {n for n, _ in change_events.family_events(row.get("usage_type") or "")}
    row_onset = _day(row.get("onset"))
    resources = {change_events.tail(r["resource_id"]): r
                 for r in row.get("resources") or [] if r.get("resource_id")}
    # Only resources that could have started the rise can rule a change out
    # for naming something else.
    starters = {rid for rid, r in resources.items() if _could_start_rise(r, row_onset)}
    causes = []
    for ch in row.get("changes") or []:
        container = ch.get("event") in change_events.CONTAINER_EVENTS
        ids = {change_events.tail(i) for i in ch.get("resource_ids") or []}
        shared = [] if container else sorted(ids & set(resources))
        # A resource's own onset is the sharper clock when the change names it.
        onset = row_onset
        for rid in shared:
            onset = _day(resources[rid].get("onset")) or onset
            break
        reason = _reason_not_attributed(row, ch, family, onset)
        if reason is None and not container and starters and ids and not shared:
            reason = "names other resources than the ones behind the cost"
        if reason:
            ch["attribution"] = None
            ch["not_attributed_because"] = reason
            continue
        if shared:
            ch["attribution"] = CONFIRMED
            ch["matched_resources"] = shared
        else:
            ch["attribution"] = LIKELY
            ch["why_likely"] = (
                "a change to a group that launches what is billed (it names the group, "
                "not the resources), in the same region and time as the cost rise"
                if container else
                "same kind of change, region and time as the cost rise, but no resource "
                "id ties them")
        ch["_rank"] = (ch["attribution"] != CONFIRMED,
                       abs((_when(ch["time"]) - datetime.combine(
                           onset, datetime.min.time(), tzinfo=UTC)).total_seconds()))
        causes.append(ch)
    causes.sort(key=lambda c: c.pop("_rank"))
    return causes


_VERBS = (("Run", "launched"), ("Create", "created"), ("Start", "started"),
          ("Modify", "changed"), ("Update", "changed"), ("Put", "set"), ("Set", "changed"),
          ("Allocate", "allocated"), ("Request", "requested"), ("Restore", "restored"),
          ("Resize", "resized"), ("Increase", "increased"), ("Execute", "ran"))


def describe(ch: dict[str, Any]) -> str:
    """"8x p4d.24xlarge launched", "CreateNatGateway nat-0abc", ..."""
    name = ch.get("event") or "?"
    verb = next((v for p, v in _VERBS if name.startswith(p)), "changed")
    itype, count = ch.get("instance_type"), ch.get("count")
    if name in ("RunInstances", "StartInstances") and itype:
        return f"{count}x {itype} {verb}" if count else f"{itype} {verb}"
    ids = ch.get("resource_ids") or []
    what = f"{name} {ids[0]}" if ids else name
    if itype and name.startswith(("Modify", "Create", "Update")):
        what += f" ({itype})"
    return what


def _money(v: float) -> str:
    return f"${abs(v):,.0f}"


def sentence(service: str, row: dict[str, Any], causes: list[dict[str, Any]]) -> str:
    head = f"{short_name(service)} +{_money(row.get('monthly_run_rate_usd') or 0)}/mo"
    onset = _day(row.get("onset"))
    if onset:
        head += f" since {onset.strftime('%b')} {onset.day}"
    where = f"{row['usage_type']} in {row.get('region') or '?'}"
    if causes:
        c = causes[0]
        who = c.get("who") or "an unknown identity"
        text = (f"{head}: {describe(c)} by {who} via {c.get('via') or 'unknown'} "
                f"({c.get('guard') or 'guard: no record'}) [{c['attribution']}]")
        return text if len(causes) == 1 else text + f" (and {len(causes) - 1} more)"
    status = row.get("cloudtrail_status") or (
        change_events.READ if row.get("cloudtrail_read") else change_events.NOT_READ)
    if status == change_events.NOT_READ:
        return f"{head}: {where}; CloudTrail for it was not read, so no change is named"
    found = len(row.get("changes") or [])
    if status == change_events.PARTLY_READ:
        gaps = row.get("cloudtrail_gaps") or []
        why = f" ({gaps[0]})" if gaps else ""
        text = f"{head}: {where}; CloudTrail for it was partly read{why}"
        if found:
            return text + f", and none of the {found} change(s) found lines up with it"
        return text + ", so no change is named"
    if found:
        return f"{head}: {where}; {found} change(s) found nearby, none lines up with it"
    return f"{head}: {where}; no change event lines up with it"


def explain(service: str, current: Window, baseline: Window, *, session: Any = None,
            meter: Meter | None = None, account_id: str | None = None, top_n: int = 3,
            read_cloudtrail: bool = True, records: list[dict[str, Any]] | None = None,
            now: datetime | None = None, today: date | None = None,
            use_cur: bool | None = None, max_calls: int = change_events.MAX_CALLS,
            sleep: Callable[[float], Any] = time.sleep,
            clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """The whole answer for one service. Never raises."""
    if session is None and (meter is None or read_cloudtrail):
        import boto3
        session = boto3.Session()
    if meter is None:
        from ..billing_access import BillingAccessError
        try:
            meter = drilldown.cost_explorer(session)
        except BillingAccessError as exc:
            return {"service": service, "service_short": short_name(service),
                    "current": current.as_dict(), "baseline": baseline.as_dict(),
                    "rows": [], "resource_source": None, "error": str(exc),
                    "lines": [f"{short_name(service)}: not drilled into. {exc}"],
                    "not_read": [f"Cost Explorer not called: {exc}"],
                    "cost_explorer_requests": 0, "lookup_calls": 0,
                    "attribution_rules": RULES}
    before = meter.requests
    dd = drilldown.drill_down(service, current, baseline, meter=meter, account_id=account_id,
                              top_n=top_n, today=today, use_cur=use_cur)
    rows = dd["rows"]
    not_read = list(dd["not_read"])
    out: dict[str, Any] = {
        "service": service, "service_short": short_name(service),
        "current": dd["current"], "baseline": dd["baseline"],
        "service_delta_usd": dd.get("service_delta_usd"),
        "resource_source": dd["resource_source"], "rows": rows, "lookup_calls": 0}
    if dd.get("error"):
        out["error"] = dd["error"]
    if rows and read_cloudtrail:
        ct = change_events.attach(rows, session, records=records, now=now, max_calls=max_calls,
                                  sleep=sleep, clock=clock)
        not_read += ct["not_read"]
        not_read.append(ACCOUNT_NOTE)
        out["lookup_calls"] = ct["lookup_calls"]
        statuses = ct.get("row_status") or {}
        for i, row in enumerate(rows):
            got = statuses.get(i) or {"status": change_events.NOT_READ, "gaps": []}
            row["cloudtrail_status"] = got["status"]
            row["cloudtrail_gaps"] = got["gaps"]
            row["cloudtrail_read"] = got["status"] == change_events.READ
    elif rows:
        not_read.append("CloudTrail was not read for this answer, so no change is named.")
    lines = []
    for row in rows:
        row.setdefault("changes", [])
        row.setdefault("cloudtrail_read", False)
        causes = attribute(row)
        row["cause"] = causes[0] if causes else None
        lines.append(sentence(service, row, causes))
    if not rows and not dd.get("error"):
        lines.append(f"{short_name(service)}: no usage type rose by ${drilldown.MIN_DELTA_USD:.0f} "
                     "or more between the two windows.")
    if dd.get("error"):
        lines.append(f"{short_name(service)}: {dd['error']}")
    out["lines"] = lines
    out["not_read"] = list(dict.fromkeys(not_read))
    if dd.get("notes"):
        out["notes"] = dd["notes"]
    out["cost_explorer_requests"] = meter.requests - before
    out["attribution_rules"] = RULES
    return out


def compact(result: dict[str, Any], *, resources: int = 3) -> dict[str, Any]:
    """explain()'s answer trimmed for a tool response: the lines, each row's
    delta, top resources and cause, and how many other changes were found."""
    rows = []
    for row in result.get("rows") or []:
        others = [c for c in row.get("changes") or [] if not c.get("attribution")]
        rows.append({
            **{k: row.get(k) for k in ("usage_type", "region", "delta_usd", "delta_per_day_usd",
                                       "monthly_run_rate_usd", "onset", "instance_type")},
            "resources": [{k: r.get(k) for k in ("resource_id", "delta_usd", "onset")}
                          for r in (row.get("resources") or [])[:resources]],
            "cause": row.get("cause"),
            "other_changes_found": len(others),
            "other_changes": [{k: c.get(k) for k in ("event", "time", "who", "via",
                                                     "not_attributed_because")}
                              for c in others[:3]],
        })
    out = {k: result.get(k) for k in ("service", "current", "baseline", "service_delta_usd",
                                      "resource_source", "lines", "not_read",
                                      "cost_explorer_requests", "lookup_calls")}
    out["rows"] = rows
    if result.get("error"):
        out["error"] = result["error"]
    return out


def combine(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Several services' answers as one block: every line, what could not be
    read (once), and what the Cost Explorer requests cost."""
    requests = sum(int(r.get("cost_explorer_requests") or 0) for r in results)
    return {
        "lines": [line for r in results for line in r.get("lines") or []],
        "services": [compact(r) for r in results],
        "not_read": list(dict.fromkeys(n for r in results for n in r.get("not_read") or [])),
        "cost_explorer_requests": requests,
        "cost_note": drilldown.cost_note(requests),
        "lookup_calls": sum(int(r.get("lookup_calls") or 0) for r in results),
        "attribution_rules": RULES,
    }
