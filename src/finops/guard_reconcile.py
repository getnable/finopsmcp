"""`nable guard reconcile`: the guard's ledger against what CloudTrail says happened.

The ledger (guard_ledger.py) records what an agent asked to do and what the
guard answered. It cannot show what then happened in the account, or what
happened without asking: a change made in the console, by a script the guard
never saw, or by an agent whose hook was bypassed. CloudTrail can. This reads
CloudTrail's management events for the changes that create, destroy or commit
money and lines them up against the ledger:

  seen_and_happened     the guard saw it (allowed, warned or asked) and it happened
  denied_but_happened   the guard denied something like it and it happened anyway
  no_guard_record       it happened and nothing in the ledger accounts for it
  service_initiated     an AWS service did it on someone's behalf (a stack
                        launching its instances, Auto Scaling replacing one)
  guarded_without_event the guard let an AWS command through and CloudTrail
                        shows nothing matching (declined at the prompt, failed
                        before the API call, or in a region not read)

Each event carries the CloudTrail userIdentity ARN and user agent, so a human
can tell an agent's CLI from a person in the console.

Matching is conservative, and says so. The ledger holds no resource ids (the
guard runs before anything exists), so an event is matched to a record by kind
(create, destroy, commit), by time (from a minute before the verdict to
`tolerance_minutes` after it) and, where the ledger's command names the API
(`aws ec2 run-instances` can only be RunInstances), by event name. A broad
command (terraform apply, an MCP call) can account for any event of its kind
in its window. When in doubt an event is left unmatched: an audit that
over-reports "no guard record" costs a look, one that explains away a change
it should not is worse.

Read-only: one boto3 session, LookupEvents only (free; 2 requests a second
per account and region, which this paces itself to), and the local ledger.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

# CloudTrail event name -> (kind, event source). Management events that start
# or stop a bill, or commit money.
EVENTS: dict[str, tuple[str, str]] = {
    "RunInstances": ("create", "ec2.amazonaws.com"),
    "CreateNatGateway": ("create", "ec2.amazonaws.com"),
    "CreateStack": ("create", "cloudformation.amazonaws.com"),
    "UpdateStack": ("create", "cloudformation.amazonaws.com"),
    "ExecuteChangeSet": ("create", "cloudformation.amazonaws.com"),
    "CreateDBInstance": ("create", "rds.amazonaws.com"),
    "CreateDBCluster": ("create", "rds.amazonaws.com"),
    "CreateLoadBalancer": ("create", "elasticloadbalancing.amazonaws.com"),
    "CreateCacheCluster": ("create", "elasticache.amazonaws.com"),
    "TerminateInstances": ("destroy", "ec2.amazonaws.com"),
    "DeleteNatGateway": ("destroy", "ec2.amazonaws.com"),
    "ReleaseAddress": ("destroy", "ec2.amazonaws.com"),
    "DeleteSnapshot": ("destroy", "ec2.amazonaws.com"),
    "DeleteStack": ("destroy", "cloudformation.amazonaws.com"),
    "DeleteDBInstance": ("destroy", "rds.amazonaws.com"),
    "DeleteDBCluster": ("destroy", "rds.amazonaws.com"),
    "DeleteLoadBalancer": ("destroy", "elasticloadbalancing.amazonaws.com"),
    "DeleteBucket": ("destroy", "s3.amazonaws.com"),
    "PurchaseReservedInstancesOffering": ("commit", "ec2.amazonaws.com"),
    "PurchaseHostReservation": ("commit", "ec2.amazonaws.com"),
    "PurchaseReservedDBInstancesOffering": ("commit", "rds.amazonaws.com"),
    "CreateSavingsPlan": ("commit", "savingsplans.amazonaws.com"),
}

# The ledger's action types (policy.py vocabulary) as event kinds.
KIND_OF_ACTION = {
    "infra_apply": "create",
    "delete_resource": "destroy",
    "terminate_instance": "destroy",
    "release_ip": "destroy",
    "snapshot_delete": "destroy",
    "purchase_commitment": "commit",
}

# A ledger command that names one AWS API can only account for that API's
# events. Checked in order against the ledger's (redacted) command summary.
_EXPECT: list[tuple[re.Pattern[str], frozenset[str]]] = [
    (re.compile(r"\baws\s+(?:\S+\s+)*?ec2\s+run-instances\b"), frozenset({"RunInstances"})),
    (re.compile(r"\baws\s+(?:\S+\s+)*?ec2\s+terminate-instances\b"),
     frozenset({"TerminateInstances"})),
    (re.compile(r"\baws\s+(?:\S+\s+)*?ec2\s+release-address\b"), frozenset({"ReleaseAddress"})),
    (re.compile(r"\baws\s+(?:\S+\s+)*?ec2\s+delete-snapshot\b"), frozenset({"DeleteSnapshot"})),
    (re.compile(r"\baws\s+(?:\S+\s+)*?ec2\s+delete-nat-gateway\b"),
     frozenset({"DeleteNatGateway"})),
    (re.compile(r"\baws\s+(?:\S+\s+)*?ec2\s+purchase-reserved-instances-offering\b"),
     frozenset({"PurchaseReservedInstancesOffering"})),
    (re.compile(r"\baws\s+(?:\S+\s+)*?ec2\s+purchase-host-reservation\b"),
     frozenset({"PurchaseHostReservation"})),
    (re.compile(r"\baws\s+(?:\S+\s+)*?rds\s+purchase-reserved-db-instances-offering\b"),
     frozenset({"PurchaseReservedDBInstancesOffering"})),
    (re.compile(r"\baws\s+(?:\S+\s+)*?savingsplans\s+create-savings-plan\b"),
     frozenset({"CreateSavingsPlan"})),
    (re.compile(r"\baws\s+(?:\S+\s+)*?rds\s+create-db-instance\b"),
     frozenset({"CreateDBInstance"})),
    (re.compile(r"\baws\s+(?:\S+\s+)*?rds\s+delete-db-instance\b"),
     frozenset({"DeleteDBInstance"})),
    (re.compile(r"\baws\s+(?:\S+\s+)*?rds\s+delete-db-cluster\b"), frozenset({"DeleteDBCluster"})),
    (re.compile(r"\baws\s+(?:\S+\s+)*?cloudformation\s+create-stack\b"),
     frozenset({"CreateStack"})),
    (re.compile(r"\baws\s+(?:\S+\s+)*?cloudformation\s+update-stack\b"),
     frozenset({"UpdateStack"})),
    (re.compile(r"\baws\s+(?:\S+\s+)*?cloudformation\s+deploy\b"),
     frozenset({"CreateStack", "UpdateStack", "ExecuteChangeSet"})),
    (re.compile(r"\baws\s+(?:\S+\s+)*?cloudformation\s+delete-stack\b"),
     frozenset({"DeleteStack"})),
    (re.compile(r"\baws\s+(?:\S+\s+)*?s3\s+rb\b"), frozenset({"DeleteBucket"})),
]
# Tools that call AWS APIs, and tools that never do. A record for the second
# kind accounts for no CloudTrail event (a Kubernetes controller creating a
# load balancer shows up under the controller's own identity, not the agent's).
_AWS_TOOLS = re.compile(r"\b(?:aws|terraform|tofu|terragrunt|pulumi|cdk|sam|eksctl|"
                        r"cloudcontrol)\b")
_NOT_AWS_TOOLS = re.compile(r"\b(?:kubectl|helm|gcloud|gsutil|az)\b")

TPS = 2.0                   # LookupEvents: 2 requests per second per account and region
PAGE_SIZE = 50              # the API's maximum
SKEW = timedelta(minutes=1)  # an event this much before the verdict still matches (clocks)
_DENIED_CODES = ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
                 "UnauthorizedAccess", "NotAuthorized")

MATCHING_NOTE = (
    "Matched by kind and time only: the ledger holds no resource ids (the guard "
    "runs before anything exists). A command that names one API (aws ec2 "
    "run-instances) matches only that API's events; a broad one (terraform apply, "
    "an MCP call) matches any event of its kind in its window. Unmatched means "
    "nothing in the ledger accounts for it, not proof that no agent did it."
)


class ReconcileError(Exception):
    """A failure the user can act on; the message says how."""


def lookup_events_policy() -> dict[str, Any]:
    """The IAM policy reconcile needs, and nothing else."""
    from .scan_manifest import GUARD_RECONCILE_ACTIONS
    return {"Version": "2012-10-17",
            "Statement": [{"Sid": "NableGuardReconcile", "Effect": "Allow",
                           "Action": [a for _, a in GUARD_RECONCILE_ACTIONS],
                           "Resource": "*"}]}


class _Pacer:
    """At most `tps` calls a second: sleeps until the next slot is due."""

    def __init__(self, tps: float, sleep: Callable[[float], Any],
                 clock: Callable[[], float]) -> None:
        self.gap = 1.0 / tps
        self.sleep, self.clock = sleep, clock
        self.due: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self.due is not None and now < self.due:
            self.sleep(self.due - now)
            now = self.due
        self.due = now + self.gap


_CONSOLE_SIGNIN_HOST = "signin.amazonaws.com"


def _via(user_agent: str, identity: dict[str, Any]) -> str:
    """A coarse, human-readable reading of who made the call."""
    ua = (user_agent or "").lower()
    # CloudTrail records a console action's user agent as the exact host
    # "signin.amazonaws.com" (or a console.*.amazonaws.com host). Compare whole
    # tokens, not substrings: "signin.amazonaws.com" inside some other string
    # says nothing about who made the call.
    tokens = set(ua.replace("/", " ").replace("[", " ").replace("]", " ").split())
    if (any(t == _CONSOLE_SIGNIN_HOST for t in tokens)
            or any(t.startswith("console.") and t.endswith(".amazonaws.com") for t in tokens)
            or ("aws-internal" in ua and "console" in ua)):
        return "console"
    if identity.get("type") == "AWSService" or str(identity.get("invokedBy") or "").endswith(
            ".amazonaws.com"):
        return "aws-service"
    if "terraform" in ua or "hashicorp" in ua:
        return "terraform"
    if "aws-cli" in ua:
        return "aws-cli"
    if "boto3" in ua or "botocore" in ua or "aws-sdk" in ua:
        return "sdk"
    return "unknown"


def _event(raw: dict[str, Any], region: str) -> dict[str, Any]:
    try:
        detail = json.loads(raw.get("CloudTrailEvent") or "{}")
    except ValueError:
        detail = {}
    identity = detail.get("userIdentity") or {}
    issuer = ((identity.get("sessionContext") or {}).get("sessionIssuer") or {})
    when = raw.get("EventTime")
    if isinstance(when, datetime) and when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    name = raw.get("EventName") or detail.get("eventName") or "?"
    ua = detail.get("userAgent") or ""
    return {
        "time": when.isoformat(timespec="seconds") if isinstance(when, datetime) else None,
        "region": detail.get("awsRegion") or region,
        "event": name,
        "kind": EVENTS.get(name, ("other", ""))[0],
        "source": raw.get("EventSource") or detail.get("eventSource"),
        "event_id": raw.get("EventId"),
        "user": raw.get("Username"),
        "identity_arn": identity.get("arn"),
        "identity_type": identity.get("type"),
        "role_arn": issuer.get("arn"),
        "invoked_by": identity.get("invokedBy"),
        "user_agent": ua,
        "via": _via(ua, identity),
        "source_ip": detail.get("sourceIPAddress"),
        "error_code": detail.get("errorCode"),
        "resources": [f"{r.get('ResourceType')}:{r.get('ResourceName')}"
                      for r in raw.get("Resources") or []],
        "_when": when,
    }


def _client_error_code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def is_denied(code: str) -> bool:
    """True for the error codes that mean the credentials lack the permission."""
    return code in _DENIED_CODES or "AccessDenied" in code


def lookup(client: Any, pacer: _Pacer, key: str, value: str, start: datetime,
           end: datetime) -> Iterator[dict[str, Any]]:
    """One LookupEvents query (one lookup attribute), every page, each request
    paced. Yields each page's response; the caller counts and reads them.
    Shared with the cost root-cause reader (anomaly/change_events.py)."""
    token = None
    while True:
        kwargs: dict[str, Any] = {
            "LookupAttributes": [{"AttributeKey": key, "AttributeValue": value}],
            "StartTime": start, "EndTime": end, "MaxResults": PAGE_SIZE}
        if token:
            kwargs["NextToken"] = token
        pacer.wait()
        resp = client.lookup_events(**kwargs)
        yield resp
        token = resp.get("NextToken")
        if not token:
            return


def read_events(session: Any, regions: Iterable[str], start: datetime, end: datetime, *,
                names: Iterable[str] = tuple(EVENTS), sleep: Callable[[float], Any] = time.sleep,
                clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """LookupEvents for each name in each region, paged and paced.

    One lookup attribute per call is all the API allows, so this is one
    query per event name; each region gets its own pacer (the limit is per
    region). Raises ReconcileError on AccessDenied or missing credentials;
    any other failure in a region is reported under region_errors and the
    other regions are still read."""
    from botocore.config import Config
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        NoCredentialsError,
        PartialCredentialsError,
    )

    config = Config(retries={"mode": "standard", "max_attempts": 5})
    events: dict[str, dict[str, Any]] = {}
    region_errors: dict[str, str] = {}
    calls = 0
    for region in regions:
        pacer = _Pacer(TPS, sleep, clock)
        client = session.client("cloudtrail", region_name=region, config=config)
        try:
            for name in names:
                for resp in lookup(client, pacer, "EventName", name, start, end):
                    calls += 1
                    for raw in resp.get("Events") or []:
                        ev = _event(raw, region)
                        events[ev["event_id"] or f"{region}:{len(events)}"] = ev
        except (NoCredentialsError, PartialCredentialsError) as exc:
            raise ReconcileError(
                "No AWS credentials found. reconcile reads CloudTrail with the same "
                "credentials as the AWS CLI (AWS_PROFILE, ~/.aws, or the environment). "
                f"({exc})") from exc
        except ClientError as exc:
            code = _client_error_code(exc)
            if is_denied(code):
                raise ReconcileError(
                    f"CloudTrail refused LookupEvents in {region} ({code}). The "
                    "credentials need cloudtrail:LookupEvents: read-only, and free. "
                    "A policy with exactly that:\n"
                    + json.dumps(lookup_events_policy(), indent=2)) from exc
            region_errors[region] = f"{code or type(exc).__name__}: {exc}"
        except BotoCoreError as exc:
            region_errors[region] = f"{type(exc).__name__}: {exc}"
    return {"events": sorted(events.values(), key=lambda e: e["time"] or ""),
            "region_errors": region_errors, "calls": calls}


def _expected(command: str) -> frozenset[str] | None:
    """The event names a ledger command can account for; None when any event
    of its kind will do; an empty set when it calls no AWS API at all."""
    for pattern, names in _EXPECT:
        if pattern.search(command):
            return names
    if not _AWS_TOOLS.search(command) and _NOT_AWS_TOOLS.search(command):
        return frozenset()
    return None


def _candidates(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in records:
        kind = KIND_OF_ACTION.get(r.get("action_type") or "")
        if kind is None or r.get("decision") not in ("allow", "warn", "ask", "deny"):
            continue
        try:
            ts = datetime.fromisoformat(r["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({**r, "_kind": kind, "_ts": ts,
                    "_expect": _expected(str(r.get("command") or ""))})
    return out


def _ledger_view(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r.get(k) for k in ("ts", "decision", "action_type", "command", "harness",
                                  "session", "tool", "monthly_usd")}


def match(events: list[dict[str, Any]], records: list[dict[str, Any]], *,
          tolerance: timedelta) -> dict[str, list[dict[str, Any]]]:
    """Put each event in one bucket (see the module docstring). An event
    matches the nearest compatible record; a record that let the call through
    is preferred over a deny, so "bypass" is claimed only when a deny is the
    only thing that could account for it."""
    cands = _candidates(records)
    used: set[int] = set()
    out: dict[str, list[dict[str, Any]]] = {
        "seen_and_happened": [], "denied_but_happened": [], "no_guard_record": [],
        "service_initiated": []}
    for ev in events:
        public = {k: v for k, v in ev.items() if not k.startswith("_")}
        if ev["via"] == "aws-service":
            out["service_initiated"].append(public)
            continue
        when = ev.get("_when")
        fits = []
        for i, r in enumerate(cands):
            if r["_kind"] != ev["kind"] or not isinstance(when, datetime):
                continue
            if not (r["_ts"] - SKEW <= when <= r["_ts"] + tolerance):
                continue
            if r["_expect"] is not None and ev["event"] not in r["_expect"]:
                continue
            fits.append((r["decision"] == "deny", abs((when - r["_ts"]).total_seconds()), i))
        if not fits:
            out["no_guard_record"].append(public)
            continue
        denied, _, i = min(fits)
        used.add(i)
        bucket = "denied_but_happened" if denied else "seen_and_happened"
        out[bucket].append({**public, "ledger": _ledger_view(cands[i])})
    out["guarded_without_event"] = [
        _ledger_view(r) for i, r in enumerate(cands)
        if i not in used and r["decision"] != "deny" and r["_expect"]]
    return out


def reconcile(hours: float = 24, regions: list[str] | None = None, *, session: Any = None,
              now: datetime | None = None, tolerance_minutes: float = 5,
              records: list[dict[str, Any]] | None = None,
              sleep: Callable[[float], Any] = time.sleep,
              clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Read CloudTrail for the last `hours` in `regions` (default: the
    session's region, else us-east-1) and match it against the ledger."""
    if session is None:
        import boto3
        session = boto3.Session()
    end = now or datetime.now(UTC)
    start = end - timedelta(hours=hours)
    regions = list(regions or [getattr(session, "region_name", None) or "us-east-1"])
    tolerance = timedelta(minutes=tolerance_minutes)
    got = read_events(session, regions, start, end, sleep=sleep, clock=clock)
    since = start - tolerance
    if records is None:
        from . import guard_ledger
        # read() counts back from the clock; the window counts back from `end`.
        back = datetime.now(UTC) - since + timedelta(minutes=1)
        records = guard_ledger.read(max(back.total_seconds(), 60) / 86400)
    window = []
    for r in records:
        try:
            if since <= datetime.fromisoformat(r["ts"]) <= end:
                window.append(r)
        except (KeyError, TypeError, ValueError):
            continue
    buckets = match(got["events"], window, tolerance=tolerance)
    return {
        "window": {"start": start.isoformat(timespec="seconds"),
                   "end": end.isoformat(timespec="seconds"), "hours": hours},
        "regions": regions,
        "event_names": sorted(EVENTS),
        "lookup_calls": got["calls"],
        "region_errors": got["region_errors"],
        "events_read": len(got["events"]),
        "ledger_records_in_window": len(window),
        "tolerance_minutes": tolerance_minutes,
        **buckets,
        "matching": MATCHING_NOTE,
    }
