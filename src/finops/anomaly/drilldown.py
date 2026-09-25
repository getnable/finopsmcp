"""Which usage types and resources are behind a service's cost change.

The detector (detector.py) and the drivers tool stop at the service: "Amazon
Elastic Compute Cloud - Compute is up $1,240". The next question is always the
same one, which instances, and this answers it from the billing data:

  1. Cost Explorer, one service, grouped by USAGE_TYPE and REGION, daily, over
     the baseline and the current window in one request. That names the usage
     type (BoxUsage:p4d.24xlarge in us-east-1), its dollar delta, and the day
     it started (the onset), which is what the CloudTrail step keys on.
  2. The resources behind the top usage types:
       - from the CUR (line_item_resource_id) when a CUR/Athena source is
         configured (connectors/cur.py), which covers any date the CUR holds;
       - otherwise from Cost Explorer's resource-level data
         (GetCostAndUsageWithResources), which an account has to opt in to and
         which covers the last 14 days only. When it is not enabled, or the
         spike is older than that, the answer says so and keeps the usage types.

Cost Explorer bills $0.01 a request (GetCostAndUsageWithResources too), and
Athena bills per byte scanned. Nothing here runs on a schedule or by default:
the tools call it only when asked (root_cause=True, `nable why`), and every
answer carries the number of requests it made.

Deltas are against the baseline scaled to the current window's length, so a
1-day current window against a 7-day baseline compares one day with the
baseline's average day.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

CE_REQUEST_USD = 0.01
MAX_PAGES = 5            # per query; each page is a billed request
RESOURCE_DAYS = 14       # how far back GetCostAndUsageWithResources reads
TOP_RESOURCES = 5        # resources kept per usage type
MIN_DELTA_USD = 1.0      # a usage type that moved less than this is noise
_BASELINE_DAYS = 7       # for a single anomaly day: the week before it

_DENIED = ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
           "UnauthorizedAccess", "NotAuthorized")


@dataclass(frozen=True)
class Window:
    """[start, end), whole UTC days, the way Cost Explorer reads a TimePeriod."""
    start: date
    end: date

    @property
    def days(self) -> int:
        return max((self.end - self.start).days, 0)

    def dates(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(self.days)]

    def as_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


def windows_for_day(day: date, baseline_days: int = _BASELINE_DAYS) -> tuple[Window, Window]:
    """An anomaly day against the week before it."""
    return Window(day, day + timedelta(days=1)), Window(day - timedelta(days=baseline_days), day)


def windows_for_period(days: int, today: date | None = None) -> tuple[Window, Window]:
    """The last `days` days against the `days` before them, the same windows
    explain_recent_cost_drivers compares."""
    today = today or datetime.now(UTC).date()
    start = today - timedelta(days=days)
    return Window(start, today), Window(start - timedelta(days=days), start)


class Meter:
    """Counts billed Cost Explorer requests, so the answer can say what it cost."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.requests = 0

    def pages(self, operation: str, **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        """Every page of one query, up to MAX_PAGES. Returns (pages, truncated)."""
        out: list[dict[str, Any]] = []
        token = None
        while True:
            if token:
                kwargs["NextPageToken"] = token
            self.requests += 1
            resp = getattr(self.client, operation)(**kwargs)
            out.append(resp)
            token = resp.get("NextPageToken")
            if not token:
                return out, False
            if len(out) >= MAX_PAGES:
                return out, True

    def note(self) -> str:
        return cost_note(self.requests)


def cost_note(n: int) -> str:
    """What n billed Cost Explorer requests cost, in words."""
    return (f"{n} Cost Explorer request{'s' if n != 1 else ''}, about "
            f"${n * CE_REQUEST_USD:.2f}, billed by AWS to this account "
            f"(${CE_REQUEST_USD:.2f} each).")


def cost_explorer(session: Any = None) -> Meter:
    """A metered client through billing_access.ce_client(), the package's one
    gate for Cost Explorer: it raises BillingAccessError in demo mode, in
    scheduled work, and where NABLE_NO_COST_EXPLORER forbids it."""
    from ..billing_access import ce_client
    return Meter(ce_client(session, region="us-east-1", reason="cost root-cause drill-down"))


def _code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def _message(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Message", "")) or str(exc)


def is_denied(exc: Exception) -> bool:
    code = _code(exc)
    return code in _DENIED or "AccessDenied" in code


def _filter(service: str, account_id: str | None,
            usage_types: list[str] | None = None) -> dict[str, Any]:
    parts: list[dict[str, Any]] = [{"Dimensions": {"Key": "SERVICE", "Values": [service]}}]
    if usage_types:
        parts.append({"Dimensions": {"Key": "USAGE_TYPE", "Values": list(usage_types)}})
    if account_id:
        parts.append({"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account_id]}})
    return parts[0] if len(parts) == 1 else {"And": parts}


def _amount(group: dict[str, Any], metric: str) -> tuple[float, str]:
    m = (group.get("Metrics") or {}).get(metric) or {}
    try:
        return float(m.get("Amount") or 0.0), str(m.get("Unit") or "")
    except (TypeError, ValueError):
        return 0.0, ""


def onset(series: dict[date, float], baseline: Window, current: Window) -> date | None:
    """The day the increase started: the first day of the run of days above
    the midpoint between the baseline average and the current average that
    contains the first such day in the current window. None when nothing rose."""
    base_days, cur_days = baseline.dates(), current.dates()
    if not cur_days:
        return None
    base_avg = sum(series.get(d, 0.0) for d in base_days) / len(base_days) if base_days else 0.0
    cur_avg = sum(series.get(d, 0.0) for d in cur_days) / len(cur_days)
    if cur_avg <= base_avg:
        return None
    threshold = base_avg + (cur_avg - base_avg) / 2
    first = next((d for d in cur_days if series.get(d, 0.0) > threshold), None)
    if first is None:
        return None
    day = first
    while day - timedelta(days=1) >= min(base_days or cur_days) \
            and series.get(day - timedelta(days=1), 0.0) > threshold:
        day -= timedelta(days=1)
    return day


def _deltas(series: dict[date, float], baseline: Window, current: Window,
            base_read: list[date] | None = None,
            cur_read: list[date] | None = None) -> dict[str, Any]:
    """Current against the baseline's average day, scaled to the current window.
    base_read/cur_read restrict both sides to the days actually read."""
    base_days = base_read if base_read is not None else baseline.dates()
    cur_days = cur_read if cur_read is not None else current.dates()
    base_total = sum(series.get(d, 0.0) for d in base_days)
    cur_total = sum(series.get(d, 0.0) for d in cur_days)
    base_avg = base_total / len(base_days) if base_days else 0.0
    cur_avg = cur_total / len(cur_days) if cur_days else 0.0
    per_day = cur_avg - base_avg
    return {
        "current_usd": round(cur_total, 2),
        "baseline_usd": round(base_avg * len(cur_days), 2),
        "delta_usd": round(cur_total - base_avg * len(cur_days), 2),
        "delta_per_day_usd": round(per_day, 2),
        "monthly_run_rate_usd": round(per_day * 30, 2),
    }


# Usage types whose suffix is an instance, node or class type, and the family
# that says which API launches it. "BoxUsage:p4d.24xlarge" -> "p4d.24xlarge".
_TYPED = ("BoxUsage", "SpotUsage", "DedicatedUsage", "HostUsage", "InstanceUsage",
          "Multi-AZUsage", "NodeUsage", "Node", "ESInstance", "Host", "Train", "Notebk",
          "Processing", "Aurora")


def _strip_region(usage_type: str) -> str:
    """"USE1-BoxUsage:p4d.24xlarge" -> "BoxUsage:p4d.24xlarge". us-east-1 has no prefix."""
    head, sep, rest = usage_type.partition("-")
    if sep and len(head) <= 5 and head.isalnum() and head.isupper() and (
            head == "EU" or any(c.isdigit() for c in head)):
        return rest
    return usage_type


def instance_type(usage_type: str) -> str | None:
    base = _strip_region(usage_type)
    kind, sep, rest = base.partition(":")
    if not sep or not rest or "." not in rest:
        return None
    if not any(kind == t or kind.startswith(t) for t in _TYPED):
        return None
    return rest


def usage_type_deltas(meter: Meter, service: str, current: Window, baseline: Window, *,
                      account_id: str | None = None, top_n: int = 5) -> dict[str, Any]:
    """One daily GetCostAndUsage over both windows, grouped by usage type and
    region. Returns {"rows", "service_delta_usd", "truncated"}; raises the
    botocore ClientError for the caller to word."""
    pages, truncated = meter.pages(
        "get_cost_and_usage",
        TimePeriod={"Start": baseline.start.isoformat(), "End": current.end.isoformat()},
        Granularity="DAILY",
        Filter=_filter(service, account_id),
        Metrics=["UnblendedCost", "UsageQuantity"],
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"},
                 {"Type": "DIMENSION", "Key": "REGION"}],
    )
    cost: dict[tuple[str, str], dict[date, float]] = {}
    usage: dict[tuple[str, str], dict[date, float]] = {}
    units: dict[tuple[str, str], str] = {}
    for page in pages:
        for result in page.get("ResultsByTime") or []:
            try:
                day = date.fromisoformat(result["TimePeriod"]["Start"][:10])
            except (KeyError, TypeError, ValueError):
                continue
            for group in result.get("Groups") or []:
                keys = list(group.get("Keys") or []) + ["", ""]
                key = (keys[0], keys[1])
                amount, _ = _amount(group, "UnblendedCost")
                qty, unit = _amount(group, "UsageQuantity")
                cost.setdefault(key, {})[day] = cost.get(key, {}).get(day, 0.0) + amount
                usage.setdefault(key, {})[day] = usage.get(key, {}).get(day, 0.0) + qty
                if unit:
                    units[key] = unit
    rows = []
    total = 0.0
    for (utype, region), series in cost.items():
        d = _deltas(series, baseline, current)
        total += d["delta_usd"]
        if d["delta_usd"] < MIN_DELTA_USD:
            continue
        u = _deltas(usage.get((utype, region), {}), baseline, current)
        start = onset(series, baseline, current)
        rows.append({
            "usage_type": utype,
            "region": region,
            **d,
            "onset": start.isoformat() if start else None,
            "instance_type": instance_type(utype),
            "usage_unit": units.get((utype, region)) or None,
            "usage_delta_per_day": u["delta_per_day_usd"],
            "resources": [],
        })
    rows.sort(key=lambda r: r["delta_usd"], reverse=True)
    return {"rows": rows[:top_n], "service_delta_usd": round(total, 2),
            "truncated": truncated, "usage_types_moved": len(rows)}


def service_deltas(meter: Meter, current: Window, baseline: Window, *,
                   account_id: str | None = None) -> dict[str, Any]:
    """Per-service deltas over the two windows, for `nable why` to pick the
    services worth drilling into. One daily request (plus pages)."""
    kwargs: dict[str, Any] = {
        "TimePeriod": {"Start": baseline.start.isoformat(), "End": current.end.isoformat()},
        "Granularity": "DAILY", "Metrics": ["UnblendedCost"],
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}]}
    if account_id:
        kwargs["Filter"] = {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account_id]}}
    pages, truncated = meter.pages("get_cost_and_usage", **kwargs)
    series: dict[str, dict[date, float]] = {}
    for page in pages:
        for result in page.get("ResultsByTime") or []:
            try:
                day = date.fromisoformat(result["TimePeriod"]["Start"][:10])
            except (KeyError, TypeError, ValueError):
                continue
            for group in result.get("Groups") or []:
                name = (group.get("Keys") or ["?"])[0]
                amount, _ = _amount(group, "UnblendedCost")
                series.setdefault(name, {})[day] = series.get(name, {}).get(day, 0.0) + amount
    services = [{"service": name, **_deltas(s, baseline, current)} for name, s in series.items()]
    services.sort(key=lambda s: s["delta_usd"], reverse=True)
    return {"services": services, "truncated": truncated}


def _resource_rows(daily: dict[tuple[str, str], dict[date, float]], baseline: Window,
                   current: Window, read: Window) -> dict[str, list[dict[str, Any]]]:
    """Per usage type, its resources ranked by delta over the days read."""
    base_read = [d for d in baseline.dates() if read.start <= d < read.end]
    cur_read = [d for d in current.dates() if read.start <= d < read.end]
    out: dict[str, list[dict[str, Any]]] = {}
    for (rid, utype), series in daily.items():
        d = _deltas(series, baseline, current, base_read, cur_read)
        start = onset(series, Window(read.start, max(read.start, baseline.end)),
                      Window(max(read.start, current.start), current.end))
        out.setdefault(utype, []).append({
            "resource_id": rid, **d, "onset": start.isoformat() if start else None,
            "days_read": len(cur_read), "baseline_days_read": len(base_read)})
    for utype, rows in out.items():
        rows.sort(key=lambda r: r["delta_usd"], reverse=True)
        out[utype] = rows[:TOP_RESOURCES]
    return out


def resources_from_ce(meter: Meter, service: str, current: Window, baseline: Window,
                      usage_types: list[str], *, today: date,
                      account_id: str | None = None) -> dict[str, Any]:
    """GetCostAndUsageWithResources for the top usage types. Returns
    {"status": "ok"|"not_enabled"|"denied"|"out_of_range"|"error", ...}."""
    earliest = today - timedelta(days=RESOURCE_DAYS)
    if current.end <= earliest:
        return {"status": "out_of_range",
                "note": (f"Cost Explorer resource-level data covers the last {RESOURCE_DAYS} "
                         f"days and this change is older, so no resource ids.")}
    read = Window(max(baseline.start, earliest), current.end)
    from botocore.exceptions import BotoCoreError, ClientError
    try:
        pages, truncated = meter.pages(
            "get_cost_and_usage_with_resources",
            TimePeriod={"Start": read.start.isoformat(), "End": read.end.isoformat()},
            Granularity="DAILY",
            Filter=_filter(service, account_id, usage_types),
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "RESOURCE_ID"},
                     {"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        )
    except ClientError as exc:
        code, msg = _code(exc), _message(exc)
        low = msg.lower()
        if code == "DataUnavailableException" or (
                "resource" in low and ("opt" in low or "enable" in low)):
            return {"status": "not_enabled",
                    "note": ("Cost Explorer resource-level data is not enabled for this "
                             "account, so no resource ids from Cost Explorer. It is an opt-in "
                             "in Cost Explorer preferences ('Resource-level data at daily "
                             f"granularity') and covers the last {RESOURCE_DAYS} days.")}
        if is_denied(exc):
            return {"status": "denied",
                    "note": ("Cost Explorer refused GetCostAndUsageWithResources "
                             f"({code}); resource ids need ce:GetCostAndUsageWithResources.")}
        return {"status": "error", "note": f"Cost Explorer resource-level read failed: {code}: {msg}"}
    except BotoCoreError as exc:
        return {"status": "error", "note": f"Cost Explorer resource-level read failed: {exc}"}
    daily: dict[tuple[str, str], dict[date, float]] = {}
    for page in pages:
        for result in page.get("ResultsByTime") or []:
            try:
                day = date.fromisoformat(result["TimePeriod"]["Start"][:10])
            except (KeyError, TypeError, ValueError):
                continue
            for group in result.get("Groups") or []:
                keys = list(group.get("Keys") or []) + ["", ""]
                rid, utype = keys[0], keys[1]
                if not rid or rid.lower().startswith("noresourceid"):
                    continue
                amount, _ = _amount(group, "UnblendedCost")
                s = daily.setdefault((rid, utype), {})
                s[day] = s.get(day, 0.0) + amount
    out: dict[str, Any] = {"status": "ok", "by_usage_type": _resource_rows(
        daily, baseline, current, read), "read": read.as_dict()}
    if read.start > baseline.start:
        out["note"] = (f"Resource-level data covers the last {RESOURCE_DAYS} days, so resource "
                       f"deltas compare {read.start.isoformat()} onward only.")
    if truncated:
        out["truncated"] = True
    return out


def resources_from_cur(current: Window, baseline: Window, usage_types: list[str], *,
                       account_id: str | None = None) -> dict[str, Any]:
    """The same, from the CUR's line_item_resource_id."""
    from ..connectors import cur
    got = cur.get_resource_daily_costs(baseline.start, current.end - timedelta(days=1),
                                       usage_types, account_id=account_id)
    if got.get("error"):
        return {"status": "error", "note": f"CUR (Athena) read failed: {got['error']}"}
    daily: dict[tuple[str, str], dict[date, float]] = {}
    for r in got.get("rows") or []:
        try:
            day = date.fromisoformat(str(r.get("day"))[:10])
        except ValueError:
            continue
        s = daily.setdefault((r["resource_id"], r["usage_type"]), {})
        s[day] = s.get(day, 0.0) + float(r.get("cost") or 0.0)
    out: dict[str, Any] = {"status": "ok", "by_usage_type": _resource_rows(
        daily, baseline, current, Window(baseline.start, current.end)),
        "note": "Athena bills the CUR query per byte scanned (not counted above)."}
    if got.get("truncated"):
        out["truncated"] = True
    return out


def drill_down(service: str, current: Window, baseline: Window, *, meter: Meter | None = None,
               session: Any = None, account_id: str | None = None, top_n: int = 5,
               today: date | None = None, use_cur: bool | None = None) -> dict[str, Any]:
    """The usage types and resources behind `service`'s change between the
    windows. Never raises: what could not be read goes in `not_read`."""
    today = today or datetime.now(UTC).date()
    meter = meter or cost_explorer(session)
    before = meter.requests
    out: dict[str, Any] = {
        "service": service, "current": current.as_dict(), "baseline": baseline.as_dict(),
        "rows": [], "resource_source": None, "not_read": []}
    from botocore.exceptions import BotoCoreError, ClientError
    try:
        got = usage_type_deltas(meter, service, current, baseline, account_id=account_id,
                                top_n=top_n)
    except ClientError as exc:
        code = _code(exc)
        why = ("needs ce:GetCostAndUsage" if is_denied(exc) else _message(exc))
        out["error"] = f"Cost Explorer usage-type read failed ({code}): {why}"
        out["not_read"].append(out["error"])
        out["cost_explorer_requests"] = meter.requests - before
        return out
    except BotoCoreError as exc:
        out["error"] = f"Cost Explorer usage-type read failed: {exc}"
        out["not_read"].append(out["error"])
        out["cost_explorer_requests"] = meter.requests - before
        return out
    out["rows"] = got["rows"]
    out["service_delta_usd"] = got["service_delta_usd"]
    if got["truncated"]:
        out["not_read"].append(
            f"Cost Explorer had more than {MAX_PAGES} pages of usage types for {service}; "
            "the rest were not read, so smaller usage types may be missing.")
    usage_types = [r["usage_type"] for r in out["rows"]]
    if usage_types:
        if use_cur is None:
            from ..connectors import cur
            use_cur = cur.is_configured()
        if use_cur:
            res = resources_from_cur(current, baseline, usage_types, account_id=account_id)
            source = "cur"
        else:
            out["not_read"].append(
                "No CUR source is configured (CUR_ATHENA_* not set), so resource ids come "
                "from Cost Explorer's resource-level data only.")
            res = resources_from_ce(meter, service, current, baseline, usage_types,
                                    today=today, account_id=account_id)
            source = "ce_resources"
        if res["status"] == "ok":
            out["resource_source"] = source
            for row in out["rows"]:
                row["resources"] = res["by_usage_type"].get(row["usage_type"], [])
            if res.get("note"):
                out.setdefault("notes", []).append(res["note"])
            if res.get("truncated"):
                out["not_read"].append("Resource rows were capped; smaller resources may be "
                                       "missing.")
        else:
            out["not_read"].append(res["note"])
    out["cost_explorer_requests"] = meter.requests - before
    return out
