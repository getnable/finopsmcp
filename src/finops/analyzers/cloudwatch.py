"""
CloudWatch metrics client for deep AWS infrastructure utilization analysis.

Provides helpers to fetch metric statistics over configurable lookback windows
and pre-built helpers for EC2, RDS, and Lambda utilization profiles.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Hashable, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)


# ── Many-series reads ─────────────────────────────────────────────────────────
#
# What a read costs, from the AWS Price List (AmazonCloudWatch offer file,
# us-east-1, checked 2026-09-24):
#   CW:Requests      GetMetricStatistics and the other standard API calls.
#                    1,000,000 requests a month free (global), then $0.01 per 1,000.
#   CW:GMD-Metrics   GetMetricData. $0.01 per 1,000 metrics requested, NO free tier.
# `nable scan` promises no paid API calls, so the default path is one
# GetMetricStatistics call per series, run concurrently. GetMetricData batching
# is opt-in (FINOPS_CLOUDWATCH_GETMETRICDATA=1, or use_get_metric_data=True) for
# a host that has decided the round trips cost more than the metrics do.

# GetMetricData takes at most 500 MetricDataQueries per call.
MAX_QUERIES_PER_CALL = 500

# Concurrent GetMetricStatistics calls per fetch. The account quota is 400
# transactions per second per region; 8 workers at a 30ms round trip is ~270.
DEFAULT_WORKERS = 8

# Give up on whatever is still outstanding once no call has finished for this
# long. A hung call then costs at most this much past the last one to return,
# and every series still outstanding comes back unread.
DEFAULT_IDLE_TIMEOUT_S = 30.0

GET_METRIC_DATA_ENV = "FINOPS_CLOUDWATCH_GETMETRICDATA"


def get_metric_data_opted_in() -> bool:
    """True when this host has opted in to billed GetMetricData batching."""
    return (os.getenv(GET_METRIC_DATA_ENV) or "").strip().lower() in ("1", "true", "yes")


# One series as (timestamp, value) pairs, oldest first. A datapoint CloudWatch
# returned without a Timestamp sorts last with None in its place.
MetricPoints = list[tuple[datetime | None, float]]


@dataclass(frozen=True)
class MetricQuery:
    """One metric series to read. `key` is the caller's handle for the result
    and never reaches CloudWatch, so it can be any hashable value. `stat` is a
    standard statistic (Average, Sum, Maximum, Minimum, SampleCount)."""
    key: Hashable
    namespace: str
    metric_name: str
    dimensions: tuple[tuple[str, str], ...]
    stat: str
    period: int


def fetch_metric_values(
    cw_client: Any,
    queries: Sequence[MetricQuery],
    start: datetime,
    end: datetime,
    *,
    use_get_metric_data: bool | None = None,
    max_workers: int = DEFAULT_WORKERS,
    idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
    failures: dict[Hashable, str] | None = None,
) -> dict[Hashable, list[float] | None]:
    """
    Read many metric series without one sequential round trip per series. The
    detectors used to call get_metric_statistics inside the describe loop, so
    200 instances with two metrics each was 400 calls back to back.

    By default each series is still one GetMetricStatistics call, which stays
    inside CloudWatch's free request tier, but `max_workers` of them run at
    once, so the wall clock is about ceil(N / max_workers) round trips instead
    of N. With use_get_metric_data=True (None follows
    FINOPS_CLOUDWATCH_GETMETRICDATA) the series go out as GetMetricData, 500 per
    call plus a page for each NextToken, which is billed per metric requested
    with no free tier. See the price notes above.

    Returns {key: values}, values oldest first. The two empty answers mean
    different things and callers must keep them apart:

    - [] is a read that succeeded and found no datapoints, the same thing an
      empty Datapoints list from get_metric_statistics meant.
    - None is a read that failed: the call raised (throttling, AccessDenied),
      it was still outstanding when the fetch gave up (idle_timeout_s), or on
      the GetMetricData path CloudWatch marked the series Forbidden or
      InternalError, or left it PartialData with no page left to fetch. A
      partial series summed or averaged is a smaller number than the real one,
      which is how a busy resource gets called idle, so it is reported as
      unread rather than returned short. Treat None exactly as a
      get_metric_statistics exception.

    `failures`, when given, is filled with {key: why} for every None: the AWS
    error code (AccessDenied, Throttling), "Timeout" for a read the fetch gave
    up on, or the GetMetricData status. Never the message, which carries ARNs.
    """
    points = fetch_metric_points(
        cw_client, queries, start, end, use_get_metric_data=use_get_metric_data,
        max_workers=max_workers, idle_timeout_s=idle_timeout_s, failures=failures)
    return {key: None if p is None else [v for _, v in p] for key, p in points.items()}


def fetch_metric_points(
    cw_client: Any,
    queries: Sequence[MetricQuery],
    start: datetime,
    end: datetime,
    *,
    use_get_metric_data: bool | None = None,
    max_workers: int = DEFAULT_WORKERS,
    idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
    failures: dict[Hashable, str] | None = None,
) -> dict[Hashable, MetricPoints | None]:
    """
    fetch_metric_values with each value's timestamp kept, for a caller that
    needs to know when the newest datapoint was published and not only what it
    was. Returns {key: [(timestamp, value), ...]} oldest first. The same reads,
    the same bill and the same [] versus None as fetch_metric_values.
    """
    if not queries:
        return {}
    if use_get_metric_data is None:
        use_get_metric_data = get_metric_data_opted_in()
    if use_get_metric_data:
        return _fetch_with_get_metric_data(cw_client, queries, start, end, failures)
    return _fetch_with_get_metric_statistics(
        cw_client, queries, start, end, max_workers, idle_timeout_s, failures)


def _exc_code(exc: BaseException) -> str:
    """The AWS error code or the exception type, never the message."""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = (resp.get("Error") or {}).get("Code")
        if code:
            return str(code)
    return type(exc).__name__


def _read_statistics(
    cw_client: Any, q: MetricQuery, start: datetime, end: datetime,
    failures: dict[Hashable, str] | None = None,
) -> MetricPoints | None:
    """One GetMetricStatistics call for one series, never raising."""
    try:
        resp = cw_client.get_metric_statistics(
            Namespace=q.namespace,
            MetricName=q.metric_name,
            Dimensions=[{"Name": n, "Value": v} for n, v in q.dimensions],
            StartTime=start,
            EndTime=end,
            Period=q.period,
            Statistics=[q.stat],
        )
        datapoints = resp.get("Datapoints", [])
    except Exception as exc:
        log.debug("CloudWatch %s read failed for %s: %s", q.metric_name, q.dimensions, exc)
        if failures is not None:
            failures[q.key] = _exc_code(exc)
        return None
    if not isinstance(datapoints, list):
        if failures is not None:
            failures[q.key] = "MalformedResponse"
        return None
    # The API returns datapoints in no particular order. A missing Timestamp
    # sorts last rather than raising on None < None.
    ordered = sorted(datapoints, key=lambda dp: (dp.get("Timestamp") is None, dp.get("Timestamp")))
    return [(dp.get("Timestamp"), dp.get(q.stat, 0)) for dp in ordered]


def _fetch_with_get_metric_statistics(
    cw_client: Any,
    queries: Sequence[MetricQuery],
    start: datetime,
    end: datetime,
    max_workers: int,
    idle_timeout_s: float,
    failures: dict[Hashable, str] | None = None,
) -> dict[Hashable, MetricPoints | None]:
    out: dict[Hashable, MetricPoints | None] = {}
    pool = ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(queries))),
                              thread_name_prefix="cw-read")
    try:
        futures = {pool.submit(_read_statistics, cw_client, q, start, end, failures): q
                   for q in queries}
        pending = set(futures)
        last_progress = time.monotonic()
        while pending:
            remaining = last_progress + idle_timeout_s - time.monotonic()
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            if done:
                last_progress = time.monotonic()
            for f in done:
                out[futures[f].key] = f.result()
        if pending:
            log.warning("CloudWatch reads stalled; %d series left unread", len(pending))
            for f in pending:
                out[futures[f].key] = None
                if failures is not None:
                    failures[futures[f].key] = "Timeout"
    finally:
        # Never wait on a hung call: queued reads are cancelled, and a running
        # one finishes (or times out in botocore) on its own thread.
        pool.shutdown(wait=False, cancel_futures=True)
    return out


def _fetch_with_get_metric_data(
    cw_client: Any,
    queries: Sequence[MetricQuery],
    start: datetime,
    end: datetime,
    failures: dict[Hashable, str] | None = None,
) -> dict[Hashable, MetricPoints | None]:
    """The opt-in path: one billed GetMetricData call per 500 series."""
    out: dict[Hashable, MetricPoints | None] = {}
    for chunk_start in range(0, len(queries), MAX_QUERIES_PER_CALL):
        chunk = queries[chunk_start:chunk_start + MAX_QUERIES_PER_CALL]
        # Ids must match ^[a-z][a-zA-Z0-9_]*$ and be unique within the call.
        by_id = {f"q{i}": q for i, q in enumerate(chunk)}
        request = [
            {
                "Id": qid,
                "MetricStat": {
                    "Metric": {
                        "Namespace": q.namespace,
                        "MetricName": q.metric_name,
                        "Dimensions": [{"Name": n, "Value": v} for n, v in q.dimensions],
                    },
                    "Period": q.period,
                    "Stat": q.stat,
                },
                "ReturnData": True,
            }
            for qid, q in by_id.items()
        ]
        points: dict[str, list[tuple[Any, float]]] = {qid: [] for qid in by_id}
        status: dict[str, str] = {}
        try:
            token = None
            seen_tokens: set[str] = set()
            while True:
                kwargs: dict[str, Any] = {
                    "MetricDataQueries": request, "StartTime": start, "EndTime": end,
                }
                if token:
                    kwargs["NextToken"] = token
                resp = cw_client.get_metric_data(**kwargs)
                for r in resp.get("MetricDataResults", []):
                    qid = r.get("Id")
                    if qid not in points:
                        continue
                    points[qid].extend(zip(r.get("Timestamps", []), r.get("Values", [])))
                    # A series can come back PartialData on one page and
                    # Complete on a later one, so the last word wins. One that
                    # finished early is simply absent from later pages.
                    status[qid] = r.get("StatusCode", "Complete")
                token = resp.get("NextToken")
                if not isinstance(token, str) or not token:
                    break
                # A token that comes back twice would page forever. Nothing
                # read so far can be trusted as whole, so fail the chunk.
                if token in seen_tokens:
                    raise RuntimeError("GetMetricData repeated a NextToken")
                seen_tokens.add(token)
        except Exception as exc:
            log.warning("CloudWatch get_metric_data failed for %d series: %s", len(chunk), exc)
            for q in chunk:
                out[q.key] = None
                if failures is not None:
                    failures[q.key] = _exc_code(exc)
            continue

        for qid, q in by_id.items():
            if status.get(qid) != "Complete":
                out[q.key] = None
                if failures is not None:
                    failures[q.key] = status.get(qid) or "Missing"
            else:
                out[q.key] = sorted(points[qid], key=lambda p: p[0])
    return out


def fetch_metric_values_by_region(
    cw_client_for_region: Callable[[str], Any],
    queries_by_region: dict[str, list[MetricQuery]],
    start: datetime,
    end: datetime,
    *,
    use_get_metric_data: bool | None = None,
    failures: dict[Hashable, str] | None = None,
) -> dict[Hashable, list[float] | None]:
    """fetch_metric_values for series that live in different regions, one
    CloudWatch client per region. A region whose client cannot be built reads
    as unread for every series in it."""
    out: dict[Hashable, list[float] | None] = {}
    for region, queries in queries_by_region.items():
        try:
            cw = cw_client_for_region(region)
        except Exception as exc:
            log.warning("CloudWatch client for %s unavailable: %s", region, exc)
            out.update({q.key: None for q in queries})
            if failures is not None:
                failures.update({q.key: _exc_code(exc) for q in queries})
            continue
        out.update(fetch_metric_values(cw, queries, start, end,
                                       use_get_metric_data=use_get_metric_data,
                                       failures=failures))
    return out


def s3_bucket_region(s3_client: Any, bucket: dict, default: str) -> str:
    """
    The region a bucket lives in, which is where CloudWatch publishes its
    storage metrics (BucketSizeBytes, NumberOfObjects). Asking us-east-1 about
    a bucket in eu-west-1 is a successful read of nothing.

    Uses ListBuckets' BucketRegion when present, else get_bucket_location, whose
    LocationConstraint is None for us-east-1 and the legacy 'EU' for eu-west-1.
    Falls back to `default` when the location cannot be read.
    """
    region = bucket.get("BucketRegion")
    if region:
        return region
    try:
        location = s3_client.get_bucket_location(Bucket=bucket["Name"]).get("LocationConstraint")
    except Exception as exc:
        log.debug("get_bucket_location failed for %s: %s", bucket.get("Name"), exc)
        return default
    if location is None or location == "":
        return "us-east-1"
    if not isinstance(location, str):
        return default
    if location == "EU":
        return "eu-west-1"
    return location


# ── Low-level metric helper ───────────────────────────────────────────────────

def get_metric_stats(
    cw_client: Any,
    namespace: str,
    metric_name: str,
    dimensions: list[dict],
    period_days: int = 14,
    stat: str = "Average",
    extended_stats: list[str] | None = None,
) -> dict:
    """
    Fetch aggregated metric statistics over a lookback window.

    Args:
        cw_client: boto3 CloudWatch client
        namespace: e.g. "AWS/EC2"
        metric_name: e.g. "CPUUtilization"
        dimensions: list of {"Name": "...", "Value": "..."}
        period_days: lookback window in days (default 14)
        stat: standard statistic — "Average", "Maximum", "Minimum", "Sum"
        extended_stats: optional list like ["p99", "p95"] for percentile stats

    Returns:
        {
            "average": float | None,
            "maximum": float | None,
            "minimum": float | None,
            "p95": float | None,  # only if requested
            "p99": float | None,  # only if requested
            "datapoints": int,
            "unit": str,
        }
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=period_days)
    # Use 1-day periods for long windows to stay within CW limits
    period_seconds = 86400  # 1 day
    empty = {"average": None, "maximum": None, "minimum": None, "datapoints": 0, "unit": "None"}

    # GetMetricStatistics, inside CloudWatch's free request tier. This used to be
    # GetMetricData, which bills every metric requested with no free tier, and
    # the per-instance and per-database profiles call it five times each. The
    # API takes standard statistics or percentiles in one call, not both, so a
    # percentile request is a second call.
    def _read(**stat_kw: Any) -> list[dict] | None:
        try:
            resp = cw_client.get_metric_statistics(
                Namespace=namespace, MetricName=metric_name, Dimensions=dimensions,
                StartTime=start, EndTime=now, Period=period_seconds, **stat_kw)
        except Exception as exc:
            log.debug("CloudWatch get_metric_statistics failed: %s", exc)
            return None
        return sorted(resp.get("Datapoints", []),
                      key=lambda dp: (dp.get("Timestamp") is None, dp.get("Timestamp")))

    points = _read(Statistics=["Average", "Maximum", "Minimum"])
    if points is None:
        return empty

    def _agg(stat: str, agg_fn) -> float | None:
        vals = [dp[stat] for dp in points if stat in dp]
        return round(agg_fn(vals), 4) if vals else None

    output: dict[str, Any] = {
        "average": _agg("Average", lambda v: sum(v) / len(v)),
        "maximum": _agg("Maximum", max),
        "minimum": _agg("Minimum", min),
        "datapoints": len(points),
        "unit": next((dp["Unit"] for dp in points if dp.get("Unit")), "None"),
    }

    if extended_stats:
        ext_points = _read(ExtendedStatistics=list(extended_stats)) or []
        for ext in extended_stats:
            vals = [dp["ExtendedStatistics"][ext] for dp in ext_points
                    if ext in (dp.get("ExtendedStatistics") or {})]
            # CW returns one value per period; p99 over the window is max of daily p99s
            output[ext] = round(max(vals), 4) if vals else None

    return output


# ── EC2 utilization ───────────────────────────────────────────────────────────

def get_ec2_utilization(
    ec2_client: Any,
    cw_client: Any,
    instance_id: str,
    period_days: int = 14,
) -> dict:
    """
    Fetch a comprehensive utilization profile for an EC2 instance.

    Returns:
        {
            "instance_id": str,
            "instance_type": str | None,
            "state": str | None,
            "cpu": {average, maximum, p95, p99},
            "network_in_bytes":  {average_per_day, maximum_per_day},
            "network_out_bytes": {average_per_day, maximum_per_day},
            "disk_read_ops":     {average_per_day, maximum_per_day},
            "disk_write_ops":    {average_per_day, maximum_per_day},
            "period_days": int,
        }
    """
    dims = [{"Name": "InstanceId", "Value": instance_id}]

    instance_type = None
    state = None
    try:
        resp = ec2_client.describe_instances(InstanceIds=[instance_id])
        reservations = resp.get("Reservations", [])
        if reservations:
            inst = reservations[0]["Instances"][0]
            instance_type = inst.get("InstanceType")
            state = inst.get("State", {}).get("Name")
    except Exception as exc:
        log.debug("describe_instances failed for %s: %s", instance_id, exc)

    cpu = get_metric_stats(
        cw_client, "AWS/EC2", "CPUUtilization", dims,
        period_days=period_days, extended_stats=["p95", "p99"],
    )
    net_in = get_metric_stats(cw_client, "AWS/EC2", "NetworkIn", dims, period_days=period_days)
    net_out = get_metric_stats(cw_client, "AWS/EC2", "NetworkOut", dims, period_days=period_days)
    disk_read = get_metric_stats(cw_client, "AWS/EC2", "DiskReadOps", dims, period_days=period_days)
    disk_write = get_metric_stats(cw_client, "AWS/EC2", "DiskWriteOps", dims, period_days=period_days)

    return {
        "instance_id": instance_id,
        "instance_type": instance_type,
        "state": state,
        "period_days": period_days,
        "cpu": {
            "average": cpu.get("average"),
            "maximum": cpu.get("maximum"),
            "p95": cpu.get("p95"),
            "p99": cpu.get("p99"),
        },
        "network_in_bytes": {
            "average_per_day": net_in.get("average"),
            "maximum_per_day": net_in.get("maximum"),
        },
        "network_out_bytes": {
            "average_per_day": net_out.get("average"),
            "maximum_per_day": net_out.get("maximum"),
        },
        "disk_read_ops": {
            "average_per_day": disk_read.get("average"),
            "maximum_per_day": disk_read.get("maximum"),
        },
        "disk_write_ops": {
            "average_per_day": disk_write.get("average"),
            "maximum_per_day": disk_write.get("maximum"),
        },
    }


# ── RDS utilization ───────────────────────────────────────────────────────────

def get_rds_utilization(
    rds_client: Any,
    cw_client: Any,
    db_identifier: str,
    period_days: int = 14,
) -> dict:
    """
    Fetch a comprehensive utilization profile for an RDS instance.

    Returns:
        {
            "db_identifier": str,
            "engine": str | None,
            "instance_class": str | None,
            "cpu": {average, maximum},
            "connections": {average, maximum},
            "free_storage_bytes": {average, minimum},
            "read_iops": {average, maximum},
            "write_iops": {average, maximum},
            "period_days": int,
        }
    """
    dims = [{"Name": "DBInstanceIdentifier", "Value": db_identifier}]

    engine = None
    instance_class = None
    allocated_storage_gb = None
    try:
        resp = rds_client.describe_db_instances(DBInstanceIdentifier=db_identifier)
        instances = resp.get("DBInstances", [])
        if instances:
            db = instances[0]
            engine = db.get("Engine")
            instance_class = db.get("DBInstanceClass")
            allocated_storage_gb = db.get("AllocatedStorage")
    except Exception as exc:
        log.debug("describe_db_instances failed for %s: %s", db_identifier, exc)

    cpu = get_metric_stats(cw_client, "AWS/RDS", "CPUUtilization", dims, period_days=period_days)
    connections = get_metric_stats(cw_client, "AWS/RDS", "DatabaseConnections", dims, period_days=period_days)
    free_storage = get_metric_stats(cw_client, "AWS/RDS", "FreeStorageSpace", dims, period_days=period_days)
    read_iops = get_metric_stats(cw_client, "AWS/RDS", "ReadIOPS", dims, period_days=period_days)
    write_iops = get_metric_stats(cw_client, "AWS/RDS", "WriteIOPS", dims, period_days=period_days)

    return {
        "db_identifier": db_identifier,
        "engine": engine,
        "instance_class": instance_class,
        "allocated_storage_gb": allocated_storage_gb,
        "period_days": period_days,
        "cpu": {
            "average": cpu.get("average"),
            "maximum": cpu.get("maximum"),
        },
        "connections": {
            "average": connections.get("average"),
            "maximum": connections.get("maximum"),
        },
        "free_storage_bytes": {
            "average": free_storage.get("average"),
            "minimum": free_storage.get("minimum"),
        },
        "read_iops": {
            "average": read_iops.get("average"),
            "maximum": read_iops.get("maximum"),
        },
        "write_iops": {
            "average": write_iops.get("average"),
            "maximum": write_iops.get("maximum"),
        },
    }


# ── Lambda utilization ────────────────────────────────────────────────────────

def get_lambda_utilization(
    lambda_client: Any,
    cw_client: Any,
    function_name: str,
    period_days: int = 14,
) -> dict:
    """
    Fetch a comprehensive utilization profile for a Lambda function.

    Note: Lambda does not expose actual memory usage via CloudWatch natively.
    We use InitDuration as a proxy for cold-start overhead, and Duration p99
    to estimate actual execution time vs. configured timeout.

    Returns:
        {
            "function_name": str,
            "runtime": str | None,
            "configured_memory_mb": int | None,
            "configured_timeout_s": int | None,
            "duration_ms": {average, p99, maximum},
            "errors": {sum},
            "throttles": {sum},
            "invocations": {sum},
            "concurrent_executions": {average, maximum},
            "init_duration_ms": {average, maximum},
            "period_days": int,
        }
    """
    dims = [{"Name": "FunctionName", "Value": function_name}]

    runtime = None
    configured_memory_mb = None
    configured_timeout_s = None
    try:
        resp = lambda_client.get_function_configuration(FunctionName=function_name)
        runtime = resp.get("Runtime")
        configured_memory_mb = resp.get("MemorySize")
        configured_timeout_s = resp.get("Timeout")
    except Exception as exc:
        log.debug("get_function_configuration failed for %s: %s", function_name, exc)

    duration = get_metric_stats(
        cw_client, "AWS/Lambda", "Duration", dims,
        period_days=period_days, extended_stats=["p99"],
    )
    errors = get_metric_stats(cw_client, "AWS/Lambda", "Errors", dims, period_days=period_days, stat="Sum")
    throttles = get_metric_stats(cw_client, "AWS/Lambda", "Throttles", dims, period_days=period_days, stat="Sum")
    invocations = get_metric_stats(cw_client, "AWS/Lambda", "Invocations", dims, period_days=period_days, stat="Sum")
    concurrent = get_metric_stats(cw_client, "AWS/Lambda", "ConcurrentExecutions", dims, period_days=period_days)
    init_duration = get_metric_stats(cw_client, "AWS/Lambda", "InitDuration", dims, period_days=period_days)

    return {
        "function_name": function_name,
        "runtime": runtime,
        "configured_memory_mb": configured_memory_mb,
        "configured_timeout_s": configured_timeout_s,
        "period_days": period_days,
        "duration_ms": {
            "average": duration.get("average"),
            "p99": duration.get("p99"),
            "maximum": duration.get("maximum"),
        },
        "errors": {
            "sum": errors.get("average"),  # stat=Sum so "average" field holds sum-per-period
        },
        "throttles": {
            "sum": throttles.get("average"),
        },
        "invocations": {
            "sum": invocations.get("average"),
        },
        "concurrent_executions": {
            "average": concurrent.get("average"),
            "maximum": concurrent.get("maximum"),
        },
        "init_duration_ms": {
            "average": init_duration.get("average"),
            "maximum": init_duration.get("maximum"),
        },
    }
