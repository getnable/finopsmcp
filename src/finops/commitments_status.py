# SPDX-License-Identifier: Apache-2.0
"""What an AWS account has committed to, and when each commitment ends.

`nable commitments status` (cli_commitments.py) prints this. It reads, with
free Describe calls only, every reservation and Savings Plan the account holds:

    rds.describe_reserved_db_instances        each region, every page
    ec2.describe_reserved_instances           each region (one answer, no pages)
    elasticache.describe_reserved_cache_nodes each region, every page
    redshift.describe_reserved_nodes          each region, every page (Marker)
    opensearch.describe_reserved_instances    each region, every page (NextToken)
    memorydb.describe_reserved_nodes          each region, every page (NextToken)
    savingsplans.describe_savings_plans       once (a global service), every page

and lists each one with its class or type, count, engine or product, offering
type, term, start, end, days left, state, and its fixed and recurring charges
exactly as AWS reports them. RDS, ElastiCache, Redshift, OpenSearch and
MemoryDB report no end date: theirs is the start plus the term, and says so
(end_basis).

OpenSearch is read once, through the opensearch API's DescribeReservedInstances,
which lists the reservations bought through either OpenSearch API (the older
es one included), so none is counted twice. botocore has no paginator for it;
every page is read by its NextToken here, as every other read is. Where
botocore's endpoint list names no Redshift, OpenSearch or MemoryDB endpoint in
a region (MemoryDB is offered in fewer regions than EC2), that service is not
called there: it is listed as not offered, with why, and is neither a failed
read nor a zero.

A read that failed is a NotRead, per service and region, with its reason
class (denied, throttled, expired and so on, by aws_read_failures' rule for
what counts as not read). It is never a zero and never "none found": a
region whose read was denied holds an unknown number of commitments.

"Matched now" is the default utilization, and it is called that and nothing
else: each active reservation against what is running this minute, from free
describes of DB instances, EC2 instances and cache clusters (read only in a
region that holds an active reservation of that service). Reservations of one
kind share the instances running, so two reservations for four m5.large with
two running are two matched, never four. Size flexibility is not counted, so
it can read low, never high. It is not billed utilization, which only Cost
Explorer has, at $0.01 a request: read_billed() reads it when the person asks
(--spend), says what it costs before the first request, and caps its pages.
Savings Plans get no matched-now figure; their billed utilization needs Cost
Explorer, and an account sees only its own plans. Nor do Redshift, OpenSearch
and MemoryDB reservations: matched now reads "not compared" for them.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .aws_read_failures import ScopedThreadPool, is_unread

PAGE_SIZE = 100                 # RDS and ElastiCache MaxRecords
EC2_PAGE_SIZE = 1000            # DescribeInstances MaxResults
SP_PAGE_SIZE = 1000             # DescribeSavingsPlans maxResults
# Free describes follow every page. The cap only stops a token that never
# ends; reaching it is reported as not read past that page.
MAX_PAGES = 200
# Each Cost Explorer page is a billed request ($0.01).
CE_MAX_PAGES = 5
CE_DAYS = 30
_WORKERS = 8

# States in which a commitment is still bought: billing, about to, or queued
# to start (Redshift spells it pending-payment, and a Redshift reserved node
# being exchanged is still held until the exchange completes). Retired,
# returned and failed ones have ended.
LIVE_STATES = frozenset({"active", "payment-pending", "pending-payment", "queued",
                         "exchanging"})
_TERMS = {31_536_000: "1 year", 94_608_000: "3 years"}

# (service, call, IAM action, what it is for)
IDENTITY_READ = ("sts", "sts.get_caller_identity", "sts:GetCallerIdentity", "identity")
REGIONS_READ = ("ec2", "ec2.describe_regions", "ec2:DescribeRegions", "regions")
COMMITMENT_READS = (
    ("rds", "rds.describe_reserved_db_instances", "rds:DescribeReservedDBInstances",
     "commitments"),
    ("ec2", "ec2.describe_reserved_instances", "ec2:DescribeReservedInstances", "commitments"),
    ("elasticache", "elasticache.describe_reserved_cache_nodes",
     "elasticache:DescribeReservedCacheNodes", "commitments"),
    ("redshift", "redshift.describe_reserved_nodes", "redshift:DescribeReservedNodes",
     "commitments"),
    # The opensearch API's call; IAM names it under the es prefix.
    ("opensearch", "opensearch.describe_reserved_instances", "es:DescribeReservedInstances",
     "commitments"),
    ("memorydb", "memorydb.describe_reserved_nodes", "memorydb:DescribeReservedNodes",
     "commitments"),
)
# The services read only where botocore's endpoint list offers them, and
# whose reservations are not compared with what runs now.
ENDPOINT_CHECKED = frozenset({"redshift", "opensearch", "memorydb"})
NOT_COMPARED = {"basis": "matched now", "compared": False, "reason": "not compared"}
SAVINGS_PLANS_READ = ("savingsplans", "savingsplans.describe_savings_plans",
                      "savingsplans:DescribeSavingsPlans", "commitments")
INVENTORY_READS = {
    "rds": ("rds", "rds.describe_db_instances", "rds:DescribeDBInstances", "running now"),
    "ec2": ("ec2", "ec2.describe_instances", "ec2:DescribeInstances", "running now"),
    "elasticache": ("elasticache", "elasticache.describe_cache_clusters",
                    "elasticache:DescribeCacheClusters", "running now"),
}
SPEND_READS = (
    ("ce", "ce.get_reservation_utilization", "ce:GetReservationUtilization",
     "billed utilization"),
    ("ce", "ce.get_savings_plans_utilization_details", "ce:GetSavingsPlansUtilizationDetails",
     "billed utilization"),
)

MATCHED_NOW_NOTE = (
    "matched now: each active reservation against what is running this minute, in its "
    "region (same class, engine or platform, and deployment; size flexibility is not "
    "counted, so it can read low). It is a point-in-time match, not billed utilization; "
    "billed utilization needs Cost Explorer (`--spend`, $0.01 a request).")
SAVINGS_PLANS_NOTE = (
    "Savings Plans: billed utilization needs Cost Explorer (`--spend`, $0.01 a request), "
    "and this account sees only its own plans, not ones bought in other accounts of its "
    "organization.")


# ── failed reads ──────────────────────────────────────────────────────────────

@dataclass
class NotRead:
    """One read that did not happen. Not a zero: nothing was learned there."""

    service: str
    region: str
    call: str
    iam_action: str
    what: str
    reason: str
    code: str
    page: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PageCap(Exception):
    """More pages than MAX_PAGES: the rest was not read."""


_NO_CREDS = ("NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError")
_TOKEN_EXPIRED = ("SSOTokenLoadError", "UnauthorizedSSOTokenError", "TokenRetrievalError")
_UNREACHABLE = ("EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError",
                "ProxyConnectionError", "ConnectionClosedError", "SSLError")


def error_code(exc: BaseException) -> str:
    """The AWS error code, or the exception's class name. Never the message."""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = (resp.get("Error") or {}).get("Code")
        if code:
            return str(code)
    return type(exc).__name__


def _status(exc: BaseException) -> int:
    resp = getattr(exc, "response", None)
    try:
        return int(((resp or {}).get("ResponseMetadata") or {}).get("HTTPStatusCode") or 400)
    except (TypeError, ValueError, AttributeError):
        return 400


def reason_for(code: str, status: int = 400) -> str:
    """The reason class for an error code (or exception class name)."""
    if code == "PageCap":
        return "page limit"
    if code in _NO_CREDS:
        return "no credentials"
    if code in _TOKEN_EXPIRED or code in ("ExpiredToken", "ExpiredTokenException",
                                          "RequestExpired"):
        return "expired"
    if code in ("InvalidClientTokenId", "SignatureDoesNotMatch", "AuthFailure",
                "UnrecognizedClientException", "InvalidAccessKeyId"):
        return "credentials rejected"
    if code == "OptInRequired":
        return "region not enabled"
    if "AccessDenied" in code or code in ("UnauthorizedOperation", "AuthorizationError",
                                          "UnauthorizedAccess"):
        return "denied"
    if any(m in code for m in ("Throttl", "RequestLimitExceeded", "TooManyRequests",
                               "SlowDown")) or code == "LimitExceededException":
        return "throttled"
    if code in _UNREACHABLE:
        return "unreachable"
    if is_unread(code, status):
        return "service error"
    return "error"


def not_read(spec: tuple[str, str, str, str], region: str, exc: BaseException,
             page: int | None = None) -> NotRead:
    service, call, action, what = spec
    code = error_code(exc)
    return NotRead(service=service, region=region, call=call, iam_action=action, what=what,
                   reason=reason_for(code, _status(exc)), code=code, page=page)


def _paged(call: Callable[..., dict], params: dict[str, Any], list_key: str,
           token: str | None, cap: int = MAX_PAGES
           ) -> tuple[list[dict], BaseException | None, int]:
    """(items from every page read, the exception that stopped it or None,
    the page it stopped on)."""
    items: list[dict] = []
    page, nxt = 0, None
    while True:
        page += 1
        kw = dict(params)
        if nxt:
            kw[token] = nxt
        try:
            resp = call(**kw)
        except Exception as exc:  # noqa: BLE001 - a failed read is reported, never raised
            return items, exc, page
        items.extend(resp.get(list_key) or [])
        nxt = resp.get(token) if token else None
        if not nxt:
            return items, None, page
        if page >= cap:
            return items, PageCap(), page


# ── time ──────────────────────────────────────────────────────────────────────

def _aware(t: Any) -> datetime | None:
    if isinstance(t, str):
        try:
            t = datetime.fromisoformat(t)
        except ValueError:
            return None
    if not isinstance(t, datetime):
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def days_left(end: datetime, now: datetime) -> int:
    """Whole days from now to end, rounded down (negative once it has ended)."""
    return math.floor((end - now).total_seconds() / 86_400)


def _term(seconds: Any) -> str | None:
    if not isinstance(seconds, int) or seconds <= 0:
        return None
    return _TERMS.get(seconds) or f"{seconds // 86_400} days"


def _when(t: datetime | None) -> str | None:
    return t.astimezone(UTC).isoformat() if t else None


def _picked(raw: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: raw[k] for k in keys if k in raw}


# ── one commitment, as AWS reported it ────────────────────────────────────────

def _record(service: str, region: str, cid: str, *, cls: str | None, count: int | None,
            product: str | None, offering: str | None, term_s: Any, start: datetime | None,
            end: datetime | None, end_basis: str, state: str, charges: dict[str, Any],
            now: datetime, **extra: Any) -> dict[str, Any]:
    return {"service": service, "id": cid, "region": region, "class": cls, "count": count,
            "product": product, "offering_type": offering, "term": _term(term_s),
            "term_seconds": term_s, "start": _when(start), "end": _when(end),
            "end_basis": end_basis if end else None,
            "days_left": days_left(end, now) if end else None, "state": state,
            "charges": charges, "matched_now": None, **extra}


def _rds(raw: dict[str, Any], region: str, now: datetime) -> dict[str, Any]:
    start, dur = _aware(raw.get("StartTime")), raw.get("Duration")
    end = start + timedelta(seconds=dur) if start and isinstance(dur, int) else None
    return _record("rds", region, str(raw.get("ReservedDBInstanceId")),
                   cls=raw.get("DBInstanceClass"), count=raw.get("DBInstanceCount"),
                   product=raw.get("ProductDescription"), offering=raw.get("OfferingType"),
                   term_s=dur, start=start, end=end, end_basis="start plus term",
                   state=str(raw.get("State") or ""), now=now,
                   charges=_picked(raw, ("FixedPrice", "UsagePrice", "CurrencyCode",
                                         "RecurringCharges")),
                   multi_az=raw.get("MultiAZ"),
                   offering_id=raw.get("ReservedDBInstancesOfferingId"),
                   _lease=raw.get("LeaseId"), _arn=raw.get("ReservedDBInstanceArn"))


def _ec2(raw: dict[str, Any], region: str, now: datetime) -> dict[str, Any]:
    return _record("ec2", region, str(raw.get("ReservedInstancesId")),
                   cls=raw.get("InstanceType"), count=raw.get("InstanceCount"),
                   product=raw.get("ProductDescription"), offering=raw.get("OfferingType"),
                   term_s=raw.get("Duration"), start=_aware(raw.get("Start")),
                   end=_aware(raw.get("End")), end_basis="reported",
                   state=str(raw.get("State") or ""), now=now,
                   charges=_picked(raw, ("FixedPrice", "UsagePrice", "CurrencyCode",
                                         "RecurringCharges")),
                   offering_class=raw.get("OfferingClass"), scope=raw.get("Scope"),
                   availability_zone=raw.get("AvailabilityZone"),
                   tenancy=raw.get("InstanceTenancy"))


def _cache(raw: dict[str, Any], region: str, now: datetime) -> dict[str, Any]:
    start, dur = _aware(raw.get("StartTime")), raw.get("Duration")
    end = start + timedelta(seconds=dur) if start and isinstance(dur, int) else None
    return _record("elasticache", region, str(raw.get("ReservedCacheNodeId")),
                   cls=raw.get("CacheNodeType"), count=raw.get("CacheNodeCount"),
                   product=raw.get("ProductDescription"), offering=raw.get("OfferingType"),
                   term_s=dur, start=start, end=end, end_basis="start plus term",
                   state=str(raw.get("State") or ""), now=now,
                   charges=_picked(raw, ("FixedPrice", "UsagePrice", "RecurringCharges")),
                   offering_id=raw.get("ReservedCacheNodesOfferingId"),
                   _arn=raw.get("ReservationARN"))


def _start_plus_term(raw: dict[str, Any]) -> tuple[datetime | None, Any, datetime | None]:
    start, dur = _aware(raw.get("StartTime")), raw.get("Duration")
    return start, dur, (start + timedelta(seconds=dur) if start and isinstance(dur, int)
                        else None)


def _redshift(raw: dict[str, Any], region: str, now: datetime) -> dict[str, Any]:
    start, dur, end = _start_plus_term(raw)
    return _record("redshift", region, str(raw.get("ReservedNodeId")),
                   cls=raw.get("NodeType"), count=raw.get("NodeCount"), product=None,
                   offering=raw.get("OfferingType"), term_s=dur, start=start, end=end,
                   end_basis="start plus term", state=str(raw.get("State") or ""), now=now,
                   charges=_picked(raw, ("FixedPrice", "UsagePrice", "CurrencyCode",
                                         "RecurringCharges")),
                   offering_id=raw.get("ReservedNodeOfferingId"),
                   node_offering_type=raw.get("ReservedNodeOfferingType"))


def _opensearch(raw: dict[str, Any], region: str, now: datetime) -> dict[str, Any]:
    start, dur, end = _start_plus_term(raw)
    sub = raw.get("BillingSubscriptionId")
    return _record("opensearch", region, str(raw.get("ReservedInstanceId")),
                   cls=raw.get("InstanceType"), count=raw.get("InstanceCount"), product=None,
                   offering=raw.get("PaymentOption"), term_s=dur, start=start, end=end,
                   end_basis="start plus term", state=str(raw.get("State") or ""), now=now,
                   charges=_picked(raw, ("FixedPrice", "UsagePrice", "CurrencyCode",
                                         "RecurringCharges")),
                   reservation_name=raw.get("ReservationName"),
                   offering_id=raw.get("ReservedInstanceOfferingId"),
                   _lease=str(sub) if sub is not None else None)


def _memorydb(raw: dict[str, Any], region: str, now: datetime) -> dict[str, Any]:
    start, dur, end = _start_plus_term(raw)
    return _record("memorydb", region, str(raw.get("ReservationId")),
                   cls=raw.get("NodeType"), count=raw.get("NodeCount"), product=None,
                   offering=raw.get("OfferingType"), term_s=dur, start=start, end=end,
                   end_basis="start plus term", state=str(raw.get("State") or ""), now=now,
                   charges=_picked(raw, ("FixedPrice", "RecurringCharges")),
                   offering_id=raw.get("ReservedNodesOfferingId"), _arn=raw.get("ARN"))


def _plan(raw: dict[str, Any], now: datetime) -> dict[str, Any]:
    kind = " ".join(x for x in (raw.get("savingsPlanType"), raw.get("ec2InstanceFamily")) if x)
    return _record("savingsplans", str(raw.get("region") or "all regions"),
                   str(raw.get("savingsPlanId")), cls=kind or None, count=None,
                   product=", ".join(raw.get("productTypes") or []) or None,
                   offering=raw.get("paymentOption"), term_s=raw.get("termDurationInSeconds"),
                   start=_aware(raw.get("start")), end=_aware(raw.get("end")),
                   end_basis="reported", state=str(raw.get("state") or ""), now=now,
                   charges=_picked(raw, ("commitment", "currency", "upfrontPaymentAmount",
                                         "recurringPaymentAmount")),
                   arn=raw.get("savingsPlanArn"), offering_id=raw.get("offeringId"),
                   _arn=raw.get("savingsPlanArn"))


# ── matched now ───────────────────────────────────────────────────────────────

# DB instance states in which RDS bills instance hours. Others (stopped,
# starting, creating, deleting, failed) are not counted: it can read low.
_RDS_RUNNING = frozenset({"available", "backing-up", "modifying", "maintenance",
                          "storage-optimization", "upgrading", "rebooting"})
_RDS_LICENSE = {"byol": "bring-your-own-license", "li": "license-included"}
_EC2_NOT_COVERED = frozenset({"spot", "scheduled", "capacity-block"})


def _rds_kind(product: Any) -> tuple[str, str | None]:
    """("postgres", None) for "postgresql"; ("oracle-se2", "bring-your-own-license")
    for "oracle-se2(byol)": the engine a DB instance reports, and the licence."""
    text = str(product or "").strip().lower()
    base, _, lic = text.partition("(")
    base = base.strip()
    return ("postgres" if base == "postgresql" else base), _RDS_LICENSE.get(lic.rstrip(")"))


def _rds_units(items: list[dict]) -> list[tuple]:
    return [(i.get("DBInstanceClass"), str(i.get("Engine") or "").lower(), bool(i.get("MultiAZ")),
             i.get("LicenseModel")) for i in items if i.get("DBInstanceStatus") in _RDS_RUNNING]


def _rds_fits(c: dict[str, Any], unit: tuple) -> bool:
    engine, lic = _rds_kind(c.get("product"))
    return (unit[0] == c.get("class") and unit[1] == engine
            and unit[2] == bool(c.get("multi_az")) and (lic is None or unit[3] == lic))


def _ec2_units(reservations: list[dict]) -> list[tuple]:
    out = []
    for r in reservations:
        for i in r.get("Instances") or []:
            if (i.get("State") or {}).get("Name") != "running":
                continue
            if i.get("InstanceLifecycle") in _EC2_NOT_COVERED:
                continue
            place = i.get("Placement") or {}
            out.append((i.get("InstanceType"), i.get("PlatformDetails"),
                        place.get("Tenancy") or "default", place.get("AvailabilityZone")))
    return out


def _ec2_fits(c: dict[str, Any], unit: tuple) -> bool:
    platform = str(c.get("product") or "").replace(" (Amazon VPC)", "")
    zonal = c.get("scope") == "Availability Zone"
    return (unit[0] == c.get("class") and unit[1] == platform
            and unit[2] == (c.get("tenancy") or "default")
            and (not zonal or unit[3] == c.get("availability_zone")))


def _cache_units(items: list[dict]) -> list[tuple]:
    out = []
    for i in items:
        if i.get("CacheClusterStatus") != "available":
            continue
        n = i.get("NumCacheNodes")
        out += [(i.get("CacheNodeType"), str(i.get("Engine") or "").lower())] * (
            n if isinstance(n, int) and n > 0 else 0)
    return out


def _cache_fits(c: dict[str, Any], unit: tuple) -> bool:
    return unit[0] == c.get("class") and unit[1] == str(c.get("product") or "").lower()


_MATCHERS: dict[str, tuple[Any, Any]] = {
    "rds": (_rds_units, _rds_fits),
    "ec2": (_ec2_units, _ec2_fits),
    "elasticache": (_cache_units, _cache_fits),
}


def match_now(active: list[dict[str, Any]], units: list[tuple], fits: Any) -> None:
    """Set matched_now on each active reservation: the running units it
    covers, each unit counted for one reservation only. A zonal EC2
    reservation takes its units first, then the soonest to end."""
    used = [False] * len(units)
    order = sorted(active, key=lambda c: (c.get("scope") != "Availability Zone",
                                          c.get("end") or "", c["id"]))
    for c in order:
        fitting = [k for k, u in enumerate(units) if fits(c, u)]
        reserved = c.get("count") if isinstance(c.get("count"), int) else 0
        took = 0
        for k in fitting:
            if took >= reserved:
                break
            if not used[k]:
                used[k] = True
                took += 1
        c["matched_now"] = {"basis": "matched now", "read": True, "reserved": reserved,
                            "running": len(fitting), "matched": took}


# ── one region ────────────────────────────────────────────────────────────────

_COMMITMENT_CALLS = {
    "rds": ("describe_reserved_db_instances", {"MaxRecords": PAGE_SIZE}, "ReservedDBInstances",
            "Marker", _rds),
    "ec2": ("describe_reserved_instances", {}, "ReservedInstances", None, _ec2),
    "elasticache": ("describe_reserved_cache_nodes", {"MaxRecords": PAGE_SIZE},
                    "ReservedCacheNodes", "Marker", _cache),
    "redshift": ("describe_reserved_nodes", {"MaxRecords": PAGE_SIZE}, "ReservedNodes",
                 "Marker", _redshift),
    "opensearch": ("describe_reserved_instances", {"MaxResults": PAGE_SIZE},
                   "ReservedInstances", "NextToken", _opensearch),
    "memorydb": ("describe_reserved_nodes", {"MaxResults": PAGE_SIZE}, "ReservedNodes",
                 "NextToken", _memorydb),
}
_INVENTORY_CALLS = {
    "rds": ("describe_db_instances", {"MaxRecords": PAGE_SIZE}, "DBInstances", "Marker"),
    "ec2": ("describe_instances",
            {"Filters": [{"Name": "instance-state-name", "Values": ["running"]}],
             "MaxResults": EC2_PAGE_SIZE}, "Reservations", "NextToken"),
    "elasticache": ("describe_cache_clusters", {"MaxRecords": PAGE_SIZE}, "CacheClusters",
                    "Marker"),
}


def read_region(clients: dict[str, Any], region: str, now: datetime
                ) -> tuple[list[dict], list[NotRead], dict[str, Any]]:
    """(commitments, not read, {service: how many, or "not read"}) for one region."""
    found: list[dict] = []
    missed: list[NotRead] = []
    summary: dict[str, Any] = {}
    for spec in COMMITMENT_READS:
        service = spec[0]
        op, params, key, token, shape = _COMMITMENT_CALLS[service]
        if service not in clients:
            continue                    # not offered in this region (offered_in)
        client = clients[service]
        if isinstance(client, BaseException):
            missed.append(not_read(spec, region, client))
            summary[service] = "not read"
            continue
        items, exc, page = _paged(getattr(client, op), params, key, token)
        rows = [shape(raw, region, now) for raw in items]
        found += rows
        if exc is not None:
            missed.append(not_read(spec, region, exc, page if page > 1 else None))
            summary[service] = "not read"
        else:
            summary[service] = len(rows)
        active = [c for c in rows if c["state"] == "active"]
        if not active:
            continue
        if service not in INVENTORY_READS:
            for c in active:
                c["matched_now"] = dict(NOT_COMPARED)
            continue
        inv = INVENTORY_READS[service]
        op, params, key, token = _INVENTORY_CALLS[service]
        units_of, fits = _MATCHERS[service]
        items, exc, page = _paged(getattr(client, op), params, key, token)
        if exc is not None:
            failed = not_read(inv, region, exc, page if page > 1 else None)
            missed.append(failed)
            for c in active:
                c["matched_now"] = {"basis": "matched now", "read": False,
                                    "reason": failed.reason}
            continue
        match_now(active, units_of(items), fits)
    return found, missed, summary


def read_savings_plans(client: Any, now: datetime) -> tuple[list[dict], list[NotRead], Any]:
    if isinstance(client, BaseException):
        return [], [not_read(SAVINGS_PLANS_READ, "all regions", client)], "not read"
    items, exc, page = _paged(client.describe_savings_plans, {"maxResults": SP_PAGE_SIZE},
                              "savingsPlans", "nextToken")
    rows = [_plan(raw, now) for raw in items]
    if exc is not None:
        return rows, [not_read(SAVINGS_PLANS_READ, "all regions", exc,
                               page if page > 1 else None)], "not read"
    return rows, [], len(rows)


def _client(session: Any, service: str, region: str, config: Any) -> Any:
    try:
        return (session.client(service, region_name=region, config=config) if config is not None
                else session.client(service, region_name=region))
    except Exception as exc:  # noqa: BLE001 - a client that cannot be built is not read
        return exc


def offered_in(session: Any, service: str, region: str) -> bool:
    """Whether botocore's endpoint list offers `service` in `region`, for the
    services in ENDPOINT_CHECKED (always True for the others). True whenever
    the list cannot be read, or names the service nowhere in the region's
    partition: a read is skipped only when the list is sure."""
    if service not in ENDPOINT_CHECKED:
        return True
    try:
        partition = session.get_partition_for_region(region)
        listed = session.get_available_regions(service, partition_name=partition)
    except Exception:  # noqa: BLE001 - an unreadable list never skips a read
        return True
    return not listed or region in listed


def read_all(session: Any, regions: list[str], *, now: datetime, config: Any = None
             ) -> dict[str, Any]:
    """Every commitment in `regions` and every Savings Plan, with matched now.

    Clients are built here, on this thread (a boto3 session is not safe to
    share across threads); the regions are then read in parallel. A service
    botocore offers nowhere in a region is not called there and is listed
    under not_offered."""
    not_offered = [{"service": svc, "region": r,
                    "reason": f"botocore's endpoint list has no {svc} endpoint in {r}"}
                   for r in regions for svc in _COMMITMENT_CALLS
                   if not offered_in(session, svc, r)]
    skipped = {(n["service"], n["region"]) for n in not_offered}
    per_region = {r: {svc: _client(session, svc, r, config) for svc in _COMMITMENT_CALLS
                      if (svc, r) not in skipped}
                  for r in regions}
    sp_client = _client(session, "savingsplans", "us-east-1", config)
    commitments: list[dict] = []
    missed: list[NotRead] = []
    summary: dict[str, Any] = {}
    with ScopedThreadPool(max_workers=max(1, min(_WORKERS, len(regions) + 1))) as pool:
        futures = {r: pool.submit(read_region, per_region[r], r, now) for r in regions}
        plans = pool.submit(read_savings_plans, sp_client, now)
        for r in regions:
            rows, failed, counts = futures[r].result()
            commitments += rows
            missed += failed
            summary[r] = {**counts, **{svc: "not offered" for svc, where in skipped
                                       if where == r}}
        rows, failed, count = plans.result()
    commitments += rows
    missed += failed
    summary["all regions"] = {"savingsplans": count}
    return {"commitments": commitments, "not_read": missed, "summary": summary,
            "not_offered": not_offered,
            "commitment_reads": len(regions) * len(_COMMITMENT_CALLS) - len(skipped) + 1}


def is_live(c: dict[str, Any]) -> bool:
    return c.get("state") in LIVE_STATES


def ordered(commitments: list[dict]) -> list[dict]:
    """Live ones first, soonest to end first; then the ended ones."""
    return sorted(commitments, key=lambda c: (not is_live(c), c.get("end") or "9999",
                                              c["service"], c["id"]))


def public(c: dict[str, Any]) -> dict[str, Any]:
    """A commitment as the report shows it (the matching keys left out)."""
    return {k: v for k, v in c.items() if not k.startswith("_")}


# ── billed utilization (Cost Explorer, only when asked) ───────────────────────

def billed_window(now: datetime) -> tuple[str, str]:
    """The last CE_DAYS whole days (Cost Explorer's end date is exclusive)."""
    end = now.astimezone(UTC).date()
    return (end - timedelta(days=CE_DAYS)).isoformat(), end.isoformat()


def ce_cost_line() -> str:
    from .anomaly.drilldown import CE_REQUEST_USD
    most = 2 * CE_MAX_PAGES
    return (f"billed utilization: up to {most} Cost Explorer requests (2 queries, at most "
            f"{CE_MAX_PAGES} pages each), ${CE_REQUEST_USD:.2f} each, at most "
            f"${most * CE_REQUEST_USD:.2f}, billed by AWS to this account")


def _tail(arn: Any) -> str:
    text = str(arn or "")
    return text.rsplit("/", 1)[-1].rsplit(":", 1)[-1] if text else ""


def read_billed(session: Any, commitments: list[dict], *, now: datetime,
                say: Callable[[str], None]) -> tuple[dict[str, Any] | None, list[NotRead]]:
    """Billed utilization from Cost Explorer for the last CE_DAYS days, put on
    the commitments it names. Says what it costs (`say`) before the first
    request. Returns (the billed block, the reads that did not happen)."""
    from .anomaly.drilldown import cost_note
    from .billing_access import BillingAccessError, ce_client

    start, end = billed_window(now)
    say(ce_cost_line())
    try:
        ce = ce_client(session, region="us-east-1", reason="billed commitment utilization")
    except BillingAccessError as exc:
        missed = [NotRead(service="ce", region="us-east-1", call=call, iam_action=action,
                          what=what, reason="cost explorer refused here",
                          code=type(exc).__name__) for _, call, action, what in SPEND_READS]
        return None, missed
    period = {"Start": start, "End": end}
    requests = 0
    truncated = False
    missed: list[NotRead] = []
    by_ref: dict[str, dict] = {}
    for c in commitments:
        for ref in (c["id"], c.get("_lease"), _tail(c.get("_arn"))):
            if ref:
                by_ref.setdefault(str(ref), c)
    unmatched: list[dict[str, Any]] = []

    groups, exc, pages = _paged(ce.get_reservation_utilization,
                                {"TimePeriod": period,
                                 "GroupBy": [{"Type": "DIMENSION", "Key": "SUBSCRIPTION_ID"}]},
                                "UtilizationsByTime", "NextPageToken", cap=CE_MAX_PAGES)
    requests += pages
    if isinstance(exc, PageCap):
        truncated = True
    elif exc is not None:
        missed.append(not_read(SPEND_READS[0], "us-east-1", exc, pages if pages > 1 else None))
    for by_time in groups:
        for g in by_time.get("Groups") or []:
            attrs = g.get("Attributes") or {}
            refs = [attrs.get("leaseId"), _tail(attrs.get("reservationARN")), g.get("Value")]
            hit = next((by_ref[str(r)] for r in refs if r and str(r) in by_ref), None)
            util = dict(g.get("Utilization") or {})
            if hit is not None:
                hit["billed_utilization"] = util
            else:
                unmatched.append({"subscription_id": g.get("Value"), "attributes": attrs,
                                  "utilization": util})

    details, exc, pages = _paged(ce.get_savings_plans_utilization_details,
                                 {"TimePeriod": period}, "SavingsPlansUtilizationDetails",
                                 "NextToken", cap=CE_MAX_PAGES)
    requests += pages
    if isinstance(exc, PageCap):
        truncated = True
    elif exc is not None:
        missed.append(not_read(SPEND_READS[1], "us-east-1", exc, pages if pages > 1 else None))
    for d in details:
        hit = by_ref.get(_tail(d.get("SavingsPlanArn")))
        util = dict(d.get("Utilization") or {})
        if hit is not None:
            hit["billed_utilization"] = util
        else:
            unmatched.append({"savings_plan_arn": d.get("SavingsPlanArn"), "utilization": util})

    say(cost_note(requests))
    return {"source": "Cost Explorer", "period": {"start": start, "end": end},
            "requests": requests, "truncated": truncated, "unmatched": unmatched}, missed


# ── what a dry run lists ──────────────────────────────────────────────────────

def dry_run_plan(spend: bool) -> dict[str, Any]:
    """Every call the command can make, where, and the IAM action for each."""
    calls = [
        {"call": IDENTITY_READ[1], "iam_action": IDENTITY_READ[2], "what": IDENTITY_READ[3],
         "where": "once", "billed": False},
        {"call": REGIONS_READ[1], "iam_action": REGIONS_READ[2], "what": REGIONS_READ[3],
         "where": "once, unless --regions names them", "billed": False},
    ]
    for service, call, action, what in COMMITMENT_READS:
        where = ("each region (one answer, no pages)" if service == "ec2"
                 else "each region, every page")
        if service in ENDPOINT_CHECKED:
            where = "each region botocore lists an endpoint in, every page"
        calls.append({"call": call, "iam_action": action, "what": what, "where": where,
                      "billed": False})
    calls.append({"call": SAVINGS_PLANS_READ[1], "iam_action": SAVINGS_PLANS_READ[2],
                  "what": SAVINGS_PLANS_READ[3], "where": "once, every page", "billed": False})
    for _, call, action, what in INVENTORY_READS.values():
        calls.append({"call": call, "iam_action": action, "what": what,
                      "where": "each region holding an active reservation of that service",
                      "billed": False})
    if spend:
        for _, call, action, what in SPEND_READS:
            calls.append({"call": call, "iam_action": action, "what": what,
                          "where": f"once, at most {CE_MAX_PAGES} pages", "billed": True})
    return {"calls": calls, "iam_actions": sorted({c["iam_action"] for c in calls}),
            "cost_explorer_called": spend,
            "cost_explorer_max_requests": 2 * CE_MAX_PAGES if spend else 0}
