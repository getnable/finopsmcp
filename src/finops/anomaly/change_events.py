"""Who changed what, around a cost spike: CloudTrail for the rows a drill-down named.

drilldown.py names the usage type and, where the CUR or resource-level Cost
Explorer data allows, the resources behind a delta, and the day it started.
This reads CloudTrail's LookupEvents for the changes that could have started
it, in a window from 24 hours before that day to 24 hours after it:

  - by resource (LookupAttributes ResourceName) for each resource id the
    drill-down found, which is what makes a "confirmed" match possible later;
  - by event name for the APIs that start that usage type's bill
    (BoxUsage -> RunInstances, StartInstances, ModifyInstanceAttribute, ...),
    kept only when the event source matches.

Only creating and modifying calls are kept (Run*, Create*, Modify*, Put*,
Update*, Start*, and a few that start a bill under another verb, such as
AllocateAddress and SetDesiredCapacity). Each event carries who made it
(identity ARN, the role behind an assumed-role session), the user agent read
as console, aws-cli, terraform, sdk or aws-service (guard_reconcile._via), and
whether the guard ledger has a verdict that accounts for it
(guard_reconcile.match, the same conservative matching `nable guard
reconcile` uses).

The pacing (2 requests a second per region), paging and the reading of an
event are guard_reconcile's, not copies. LookupEvents is free and read-only,
and it is not in the connect key by design: event history shows who did what,
a wider read than the cost data the key is for. When it is denied, the answer
says so and prints the one-line policy; the cost half of the answer stands.

This module attaches the events it found to each row. Deciding which of them
explains the delta, and how sure that is, is root_cause.py's job.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any

from .. import guard_reconcile as gr

CHANGE_PREFIXES = ("Run", "Create", "Modify", "Put", "Update", "Start")
# Calls that start or raise a bill without one of those verbs.
EXTRA_CHANGES = frozenset({
    "AllocateAddress", "RequestSpotInstances", "RequestSpotFleet", "SetDesiredCapacity",
    "ExecuteChangeSet", "ResizeCluster", "RestoreDBInstanceFromDBSnapshot",
    "RestoreDBClusterFromSnapshot", "IncreaseReplicaCount"})

WINDOW = timedelta(hours=24)       # either side of the onset day
HISTORY_DAYS = 90                  # how far back LookupEvents reads
MAX_CALLS = 40                     # about 20 seconds at 2 a second
MAX_PAGES_PER_QUERY = 4
RESOURCES_PER_ROW = 3
GUARD_TOLERANCE_MINUTES = 15       # a verdict this long before an event still accounts for it

_EC2, _ASG = "ec2.amazonaws.com", "autoscaling.amazonaws.com"
_RDS, _CACHE = "rds.amazonaws.com", "elasticache.amazonaws.com"
_SM, _ECS = "sagemaker.amazonaws.com", "ecs.amazonaws.com"

# Usage type (region prefix stripped) -> the calls that start its bill.
_FAMILIES: list[tuple[re.Pattern[str], tuple[tuple[str, str], ...]]] = [
    (re.compile(r"^(BoxUsage|SpotUsage|DedicatedUsage|HostUsage)"), (
        ("RunInstances", _EC2), ("StartInstances", _EC2), ("ModifyInstanceAttribute", _EC2),
        ("CreateFleet", _EC2), ("UpdateAutoScalingGroup", _ASG), ("SetDesiredCapacity", _ASG))),
    (re.compile(r"^EBS:Volume"), (
        ("CreateVolume", _EC2), ("ModifyVolume", _EC2), ("RunInstances", _EC2))),
    (re.compile(r"^EBS:Snapshot"), (("CreateSnapshot", _EC2), ("CreateSnapshots", _EC2))),
    (re.compile(r"^NatGateway-"), (("CreateNatGateway", _EC2),)),
    (re.compile(r"^(ElasticIP|PublicIPv4)"), (("AllocateAddress", _EC2),)),
    (re.compile(r"^(LoadBalancerUsage|LCUUsage)"),
     (("CreateLoadBalancer", "elasticloadbalancing.amazonaws.com"),)),
    (re.compile(r"^(InstanceUsage|Multi-AZUsage|Aurora:|RDS:)"), (
        ("CreateDBInstance", _RDS), ("ModifyDBInstance", _RDS), ("StartDBInstance", _RDS),
        ("CreateDBCluster", _RDS), ("ModifyDBCluster", _RDS),
        ("RestoreDBInstanceFromDBSnapshot", _RDS))),
    (re.compile(r"^NodeUsage:cache\."), (
        ("CreateCacheCluster", _CACHE), ("CreateReplicationGroup", _CACHE),
        ("ModifyReplicationGroup", _CACHE), ("ModifyCacheCluster", _CACHE),
        ("IncreaseReplicaCount", _CACHE))),
    (re.compile(r"^Node:"), (
        ("CreateCluster", "redshift.amazonaws.com"), ("ModifyCluster", "redshift.amazonaws.com"),
        ("ResizeCluster", "redshift.amazonaws.com"))),
    (re.compile(r"^ESInstance"), (
        ("CreateDomain", "es.amazonaws.com"), ("UpdateDomainConfig", "es.amazonaws.com"))),
    (re.compile(r"^(Host|Train|Notebk|Processing):ml\."), (
        ("CreateEndpoint", _SM), ("UpdateEndpoint", _SM), ("CreateTrainingJob", _SM),
        ("CreateNotebookInstance", _SM), ("StartNotebookInstance", _SM),
        ("CreateProcessingJob", _SM))),
    (re.compile(r"^Lambda-Provisioned"),
     (("PutProvisionedConcurrencyConfig", "lambda.amazonaws.com"),)),
    # Lambda's CloudTrail event names carry the API version.
    (re.compile(r"^Lambda-GB-Second"), (
        ("CreateFunction20150331", "lambda.amazonaws.com"),
        ("UpdateFunctionConfiguration20150331v2", "lambda.amazonaws.com"))),
    (re.compile(r"^(WriteCapacityUnit|ReadCapacityUnit)"), (
        ("CreateTable", "dynamodb.amazonaws.com"), ("UpdateTable", "dynamodb.amazonaws.com"))),
    (re.compile(r"^AmazonEKS-Hours"), (
        ("CreateCluster", "eks.amazonaws.com"), ("CreateNodegroup", "eks.amazonaws.com"))),
    (re.compile(r"^Fargate-"), (
        ("CreateService", _ECS), ("UpdateService", _ECS), ("RunTask", _ECS))),
    (re.compile(r"(ProvisionedThroughput|ModelUnits)"),
     (("CreateProvisionedModelThroughput", "bedrock.amazonaws.com"),)),
    (re.compile(r"ShardHour"), (
        ("CreateStream", "kinesis.amazonaws.com"), ("UpdateShardCount", "kinesis.amazonaws.com"))),
]

_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d+$")


# Changes that never move a bill: tagging.
_NO_BILL = re.compile(r"(Tag|Tags|Tagging)$")


def is_change(name: str) -> bool:
    """A call that creates or modifies something, which is all that can start a bill."""
    return (name.startswith(CHANGE_PREFIXES) or name in EXTRA_CHANGES) \
        and not _NO_BILL.search(name)


def family_events(usage_type: str) -> tuple[tuple[str, str], ...]:
    """(event name, event source) for the calls that start this usage type's bill."""
    from .drilldown import _strip_region
    base = _strip_region(usage_type)
    for pattern, events in _FAMILIES:
        if pattern.search(base):
            return events
    return ()


def window_for(onset: date) -> tuple[datetime, datetime]:
    """From 24 hours before the onset day to 24 hours after it (UTC, as Cost
    Explorer's days are)."""
    start = datetime.combine(onset, dtime(0, 0), tzinfo=UTC)
    return start - WINDOW, start + timedelta(days=1) + WINDOW


def trail_region(region: str | None) -> str:
    """Global usage (REGION "global" or "NoRegion") is logged in us-east-1."""
    return region if region and _REGION_RE.match(region) else "us-east-1"


def tail(resource_id: str) -> str:
    """The name part of an ARN ("arn:aws:ec2:...:natgateway/nat-0abc" -> "nat-0abc");
    a bare id unchanged."""
    if not resource_id.startswith("arn:"):
        return resource_id
    rest = resource_id.split(":", 5)[-1]
    return re.split(r"[/:]", rest)[-1] or resource_id


def _details(raw: dict[str, Any]) -> dict[str, Any]:
    """What the event launched: instance type, how many, and the ids it names."""
    try:
        detail = json.loads(raw.get("CloudTrailEvent") or "{}")
    except ValueError:
        detail = {}
    req = detail.get("requestParameters") or {}
    resp = detail.get("responseElements") or {}
    if not isinstance(req, dict):
        req = {}
    if not isinstance(resp, dict):
        resp = {}
    launched = ((resp.get("instancesSet") or {}).get("items") or []) \
        if isinstance(resp.get("instancesSet"), dict) else []
    itype: Any = (req.get("instanceType") or req.get("dBInstanceClass")
                  or req.get("cacheNodeType") or req.get("nodeType"))
    if isinstance(itype, dict):
        itype = itype.get("value")
    if not itype and launched:
        itype = launched[0].get("instanceType")
    count = len(launched) or None
    if count is None:
        asked = ((req.get("instancesSet") or {}).get("items") or [{}]) \
            if isinstance(req.get("instancesSet"), dict) else [{}]
        count = asked[0].get("minCount") if isinstance(asked[0], dict) else None
        count = count or req.get("numCacheNodes") or req.get("desiredCapacity") \
            or req.get("numberOfNodes")
    ids = [str(r.get("ResourceName")) for r in raw.get("Resources") or [] if r.get("ResourceName")]
    ids += [str(i["instanceId"]) for i in launched if isinstance(i, dict) and i.get("instanceId")]
    for key in ("dBInstanceIdentifier", "dBClusterIdentifier", "volumeId", "natGatewayId",
                "cacheClusterId", "replicationGroupId", "autoScalingGroupName", "endpointName",
                "functionName", "tableName", "clusterIdentifier", "domainName"):
        for src in (req, resp):
            v = src.get(key)
            if isinstance(v, str) and v:
                ids.append(v)
    nat = resp.get("natGateway")
    if isinstance(nat, dict) and nat.get("natGatewayId"):
        ids.append(str(nat["natGatewayId"]))
    try:
        count = int(count) if count is not None else None
    except (TypeError, ValueError):
        count = None
    return {"instance_type": str(itype) if itype else None, "count": count,
            "resource_ids": sorted({tail(i) for i in ids})}


def _targets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i, row in enumerate(rows):
        if not row.get("onset"):
            continue
        try:
            onset = date.fromisoformat(row["onset"])
        except ValueError:
            continue
        rids = [r["resource_id"] for r in row.get("resources") or [] if r.get("resource_id")]
        out.append({"index": i, "region": trail_region(row.get("region")), "onset": onset,
                    "resource_ids": rids[:RESOURCES_PER_ROW],
                    "events": family_events(row.get("usage_type") or "")})
    return out


def denied_note(regions: Iterable[str], code: str) -> str:
    policy = json.dumps(gr.lookup_events_policy(), separators=(",", ":"))
    return (f"CloudTrail not read: LookupEvents was denied in {', '.join(sorted(regions))} "
            f"({code}), so no change events. It is free and read-only, and not in the connect "
            f"key by design (event history shows who did what). To grant it: {policy}")


def read_changes(session: Any, rows: list[dict[str, Any]], *, now: datetime | None = None,
                 max_calls: int = MAX_CALLS, sleep: Callable[[float], Any] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """LookupEvents for each drill-down row with an onset. Returns
    {"by_row": {row index: [event, ...]}, "calls", "not_read", "regions_read"}.
    Never raises."""
    from botocore.config import Config
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        NoCredentialsError,
        PartialCredentialsError,
    )

    now = now or datetime.now(UTC)
    not_read: list[str] = []
    # (region, key, value, start, end, source or None) -> row indexes
    queries: dict[tuple[str, str, str, datetime, datetime, str | None], set[int]] = {}
    for t in _targets(rows):
        start, end = window_for(t["onset"])
        end = min(end, now)
        if start >= end:
            continue
        if start < now - timedelta(days=HISTORY_DAYS):
            not_read.append(f"The change on {t['onset'].isoformat()} is older than the "
                            f"{HISTORY_DAYS} days of CloudTrail event history LookupEvents reads.")
            continue
        for rid in t["resource_ids"]:
            queries.setdefault((t["region"], "ResourceName", tail(rid), start, end, None),
                               set()).add(t["index"])
        for name, source in t["events"]:
            queries.setdefault((t["region"], "EventName", name, start, end, source),
                               set()).add(t["index"])
        if not t["resource_ids"] and not t["events"]:
            not_read.append(f"No change event is known to start {rows[t['index']]['usage_type']}"
                            "'s bill, and no resource id was found to look up, so CloudTrail "
                            "was not asked about it.")

    by_row: dict[int, dict[str, dict[str, Any]]] = {}
    calls = 0
    capped = False
    denied: dict[str, str] = {}
    region_errors: dict[str, str] = {}
    regions_read: list[str] = []
    config = Config(retries={"mode": "standard", "max_attempts": 5})
    for region in dict.fromkeys(q[0] for q in queries):
        pacer = gr._Pacer(gr.TPS, sleep, clock)
        try:
            client = session.client("cloudtrail", region_name=region, config=config)
        except (BotoCoreError, ValueError) as exc:
            region_errors[region] = f"{type(exc).__name__}: {exc}"
            continue
        read_any = False
        for (qregion, key, value, start, end, source), idxs in queries.items():
            if qregion != region:
                continue
            if calls >= max_calls:
                capped = True
                break
            try:
                pages = 0
                for resp in gr.lookup(client, pacer, key, value, start, end):
                    calls += 1
                    pages += 1
                    read_any = True
                    for raw in resp.get("Events") or []:
                        name = raw.get("EventName") or ""
                        if not is_change(name):
                            continue
                        if source and raw.get("EventSource") and raw["EventSource"] != source:
                            continue
                        ev = gr._event(raw, region)
                        ev.update(_details(raw))
                        if key == "ResourceName" and value not in ev["resource_ids"]:
                            ev["resource_ids"] = sorted({*ev["resource_ids"], value})
                        eid = ev["event_id"] or f"{region}:{name}:{ev['time']}"
                        for i in idxs:
                            by_row.setdefault(i, {})[eid] = ev
                    if pages >= MAX_PAGES_PER_QUERY or calls >= max_calls:
                        if resp.get("NextToken"):
                            capped = True
                        break
            except (NoCredentialsError, PartialCredentialsError):
                not_read.append("CloudTrail not read: no AWS credentials were found.")
                return {"by_row": {}, "calls": calls, "not_read": not_read,
                        "regions_read": regions_read}
            except ClientError as exc:
                code = gr._client_error_code(exc)
                if gr.is_denied(code):
                    denied[region] = code
                else:
                    region_errors[region] = f"{code or type(exc).__name__}: {exc}"
                break
            except BotoCoreError as exc:
                region_errors[region] = f"{type(exc).__name__}: {exc}"
                break
        if read_any and region not in denied and region not in region_errors:
            regions_read.append(region)
        if capped:
            break
    if denied:
        not_read.append(denied_note(denied, next(iter(denied.values()))))
    for region, err in region_errors.items():
        not_read.append(f"CloudTrail region {region} not read: {err}")
    if capped:
        not_read.append(f"CloudTrail reading stopped at {calls} LookupEvents calls (paced at "
                        f"2 a second); some rows or pages were not read.")
    return {"by_row": {i: sorted(evs.values(), key=lambda e: e["time"] or "")
                       for i, evs in by_row.items()},
            "calls": calls, "not_read": not_read, "regions_read": regions_read}


def guard_verdicts(events: list[dict[str, Any]], records: list[dict[str, Any]] | None = None, *,
                   tolerance_minutes: float = GUARD_TOLERANCE_MINUTES,
                   now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """event_id -> {"bucket", "ledger"} from guard_reconcile.match. Every
    change here starts or raises a bill, so an event outside reconcile's
    vocabulary is matched as a create (what `terraform apply` is recorded as)."""
    timed = [e for e in events if isinstance(e.get("_when"), datetime) and e.get("event_id")]
    if not timed:
        return {}
    if records is None:
        from .. import guard_ledger
        earliest = min(e["_when"] for e in timed)
        back = (now or datetime.now(UTC)) - earliest + timedelta(
            minutes=tolerance_minutes + 1)
        records = guard_ledger.read(max(back.total_seconds(), 60) / 86400)
    evs = [{**e, "kind": gr.EVENTS.get(e["event"], ("create", ""))[0]} for e in timed]
    buckets = gr.match(evs, records, tolerance=timedelta(minutes=tolerance_minutes))
    out: dict[str, dict[str, Any]] = {}
    for bucket, items in buckets.items():
        if bucket == "guarded_without_event":
            continue
        for ev in items:
            out[ev["event_id"]] = {"bucket": bucket, "ledger": ev.get("ledger")}
    return out


def _short_usd(v: Any) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return ""
    if x >= 1000:
        return f"${x / 1000:,.0f}k" if x >= 10000 else f"${x / 1000:,.1f}k"
    return f"${x:,.0f}"


_PAST = {"allow": "allowed", "warn": "warned", "ask": "asked", "deny": "denied"}


def guard_label(verdict: dict[str, Any] | None, event: dict[str, Any]) -> str:
    """"guard: allowed at $128k/mo, session s1", or why there is no verdict."""
    if event.get("via") == "aws-service":
        return f"done by an AWS service ({event.get('invoked_by') or 'service'})"
    if not verdict or verdict.get("bucket") == "no_guard_record" or not verdict.get("ledger"):
        return "guard: no record"
    led = verdict["ledger"]
    parts = [_PAST.get(led.get("decision"), led.get("decision") or "?")]
    money = _short_usd(led.get("monthly_usd"))
    if money:
        parts[0] += f" at {money}/mo"
    if led.get("session"):
        parts.append(f"session {led['session']}")
    text = "guard: " + ", ".join(parts)
    if verdict["bucket"] == "denied_but_happened":
        text += ", happened anyway"
    return text


def attach(rows: list[dict[str, Any]], session: Any = None, *,
           records: list[dict[str, Any]] | None = None, now: datetime | None = None,
           max_calls: int = MAX_CALLS, sleep: Callable[[float], Any] = time.sleep,
           clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Read the change events for drill-down rows and put them on each row as
    `changes` (who, what, when, via, guard). Returns the read's summary."""
    if session is None:
        import boto3
        session = boto3.Session()
    got = read_changes(session, rows, now=now, max_calls=max_calls, sleep=sleep, clock=clock)
    every = {e["event_id"]: e for evs in got["by_row"].values() for e in evs if e.get("event_id")}
    verdicts = guard_verdicts(list(every.values()), records, now=now) if every else {}
    for i, row in enumerate(rows):
        row["changes"] = []
        for ev in got["by_row"].get(i, []):
            v = verdicts.get(ev.get("event_id") or "")
            public = {k: val for k, val in ev.items() if not k.startswith("_")}
            public["who"] = ev.get("role_arn") or ev.get("identity_arn") or ev.get("user")
            public["guard"] = guard_label(v, ev)
            public["guard_ledger"] = (v or {}).get("ledger")
            row["changes"].append(public)
    return {"lookup_calls": got["calls"], "not_read": got["not_read"],
            "regions_read": got["regions_read"],
            "guard_tolerance_minutes": GUARD_TOLERANCE_MINUTES}
