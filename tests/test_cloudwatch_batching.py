"""CloudWatch reads run concurrently, stay free by default, and never turn a
failed read into zero.

The waste detectors used to call get_metric_statistics once per resource per
metric, inside the describe loop. 200 instances with two metrics each was 400
sequential round trips for one check in one region.

Two ways out, with different bills (AWS Price List, AmazonCloudWatch offer file,
us-east-1, checked 2026-09-24): GetMetricStatistics sits in CW:Requests, which
has 1,000,000 free requests a month; GetMetricData is CW:GMD-Metrics, $0.01 per
1,000 metrics with no free tier. `nable scan` promises no paid API calls, so the
default keeps one GetMetricStatistics call per series and runs them
concurrently. GetMetricData batching (500 series a call) is opt-in through
FINOPS_CLOUDWATCH_GETMETRICDATA=1.

Either way a failure has to reach the detector as "unread", the same thing an
exception from get_metric_statistics was, and never as an empty series that
reads as idle. On the batched path that includes a throttled call covering a
whole chunk and a single series answered Forbidden, InternalError or
PartialData.

The fakes answer with the real response shapes, and the botocore Stubber tests
validate each request against the real service model.
"""
from __future__ import annotations

import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

from finops.analyzers.cloudwatch import (
    GET_METRIC_DATA_ENV,
    MetricQuery,
    fetch_metric_points,
    fetch_metric_values,
)


_T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
_ID_PATTERN = re.compile(r"^[a-z][a-zA-Z0-9_]*$")


@pytest.fixture(autouse=True)
def _free_path_unless_asked(monkeypatch):
    """A developer shell with the opt-in set must not flip every test here onto
    the billed path."""
    monkeypatch.delenv(GET_METRIC_DATA_ENV, raising=False)


@pytest.fixture
def opted_in(monkeypatch):
    monkeypatch.setenv(GET_METRIC_DATA_ENV, "1")


def _ts(hours: int) -> datetime:
    return _T0 + timedelta(hours=hours)


def _q(key, metric="CPUUtilization", value="i-1", stat="Average", period=3600):
    return MetricQuery(key, "AWS/EC2", metric, (("InstanceId", value),), stat, period)


def _stubbed_cloudwatch():
    client = boto3.client(
        "cloudwatch", region_name="us-east-1",
        aws_access_key_id="testing", aws_secret_access_key="testing")
    return client, Stubber(client)


class _CountingCloudWatch:
    """Answers every series with one datapoint through either API, counting
    calls. `fail_call` throttles that GetMetricData call; `hang` names series
    whose GetMetricStatistics call takes `hang_s` seconds."""

    def __init__(self, fail_call: int | None = None, latency: float = 0.0,
                 hang: tuple = (), hang_s: float = 0.0):
        self.calls: list[list[dict]] = []
        self.statistics_calls = 0
        self.max_in_flight = 0
        self._in_flight = 0
        self._lock = threading.Lock()
        self._fail_call = fail_call
        self._latency = latency
        self._hang, self._hang_s = hang, hang_s

    def get_metric_statistics(self, **kw):
        with self._lock:
            self.statistics_calls += 1
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            value = kw["Dimensions"][0]["Value"]
            time.sleep(self._hang_s if value in self._hang else self._latency)
            stat = kw["Statistics"][0]
            return {"Datapoints": [{"Timestamp": _T0, stat: 1.0, "Unit": "Percent"}],
                    "Label": kw["MetricName"]}
        finally:
            with self._lock:
                self._in_flight -= 1

    def get_metric_data(self, **kw):
        self.calls.append(kw["MetricDataQueries"])
        if self._fail_call == len(self.calls):
            raise ClientError(
                {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
                "GetMetricData")
        return {"MetricDataResults": [
            {"Id": q["Id"], "Label": q["Id"], "Timestamps": [_T0],
             "Values": [1.0], "StatusCode": "Complete"}
            for q in kw["MetricDataQueries"]
        ], "Messages": []}


# ── the helper, default path: GetMetricStatistics, concurrent ───────────────

def test_the_default_request_matches_the_service_model():
    client, stub = _stubbed_cloudwatch()
    stub.add_response(
        "get_metric_statistics",
        {"Label": "CPUUtilization", "Datapoints": [
            {"Timestamp": _ts(1), "Average": 3.0, "Unit": "Percent"},
            {"Timestamp": _ts(0), "Average": 2.0, "Unit": "Percent"},
        ]},
        {
            "Namespace": "AWS/EC2",
            "MetricName": "CPUUtilization",
            "Dimensions": [{"Name": "InstanceId", "Value": "i-1"}],
            "StartTime": _T0,
            "EndTime": _ts(24),
            "Period": 3600,
            "Statistics": ["Average"],
        },
    )
    with stub:
        got = fetch_metric_values(client, [_q("i-1")], _T0, _ts(24))
    stub.assert_no_pending_responses()
    # Oldest first, whatever order CloudWatch returned them in.
    assert got == {"i-1": [2.0, 3.0]}


def test_the_default_path_never_calls_the_billed_api():
    cw = _CountingCloudWatch()
    got = fetch_metric_values(cw, [_q(n, value=f"i-{n}") for n in range(30)], _T0, _ts(24))
    assert cw.calls == []
    assert cw.statistics_calls == 30
    assert all(v == [1.0] for v in got.values())


def test_the_env_flag_opts_in_to_get_metric_data(opted_in):
    cw = _CountingCloudWatch()
    fetch_metric_values(cw, [_q(n, value=f"i-{n}") for n in range(30)], _T0, _ts(24))
    assert len(cw.calls) == 1
    assert cw.statistics_calls == 0


def test_an_explicit_argument_beats_the_env_flag(opted_in):
    cw = _CountingCloudWatch()
    fetch_metric_values(cw, [_q("a")], _T0, _ts(24), use_get_metric_data=False)
    assert cw.calls == [] and cw.statistics_calls == 1


def test_n_slow_reads_take_about_n_over_workers_round_trips():
    """64 reads at 50ms each: 3.2s one after another, ~0.4s eight at a time."""
    latency, n, workers = 0.05, 64, 8
    cw = _CountingCloudWatch(latency=latency)

    t0 = time.monotonic()
    got = fetch_metric_values(cw, [_q(i, value=f"i-{i}") for i in range(n)], _T0, _ts(24),
                              max_workers=workers)
    elapsed = time.monotonic() - t0

    assert len(got) == n and cw.statistics_calls == n
    assert cw.max_in_flight == workers
    assert elapsed >= math.ceil(n / workers) * latency * 0.9
    assert elapsed < n * latency / 3, f"{elapsed:.2f}s is not concurrent"


def test_a_hung_read_is_abandoned_as_unread_without_holding_up_the_rest():
    cw = _CountingCloudWatch(hang=("i-hung",), hang_s=2.0)
    queries = [_q("hung", value="i-hung")] + [_q(i, value=f"i-{i}") for i in range(20)]

    t0 = time.monotonic()
    got = fetch_metric_values(cw, queries, _T0, _ts(24), idle_timeout_s=0.3)
    elapsed = time.monotonic() - t0

    assert got["hung"] is None
    assert all(got[i] == [1.0] for i in range(20))
    assert elapsed < 1.5, f"waited {elapsed:.2f}s on a hung call"


def test_a_failed_read_is_unread_and_an_empty_one_is_not():
    class _CW:
        def get_metric_statistics(self, **kw):
            if kw["Dimensions"][0]["Value"] == "i-denied":
                raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}},
                                  "GetMetricStatistics")
            return {"Datapoints": [], "Label": kw["MetricName"]}

    got = fetch_metric_values(
        _CW(), [_q("a", value="i-denied"), _q("b", value="i-quiet")], _T0, _ts(24))
    assert got == {"a": None, "b": []}


def test_no_queries_means_no_calls():
    cw = _CountingCloudWatch()
    assert fetch_metric_values(cw, [], _T0, _ts(24)) == {}
    assert cw.calls == [] and cw.statistics_calls == 0


# ── the helper, opt-in path: GetMetricData, batched ──────────────────────────

def test_the_batched_request_matches_the_service_model():
    client, stub = _stubbed_cloudwatch()
    stub.add_response(
        "get_metric_data",
        {"MetricDataResults": [{
            "Id": "q0", "Label": "CPUUtilization",
            "Timestamps": [_ts(1), _ts(0)], "Values": [3.0, 2.0],
            "StatusCode": "Complete",
        }]},
        {
            "MetricDataQueries": [{
                "Id": "q0",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/EC2",
                        "MetricName": "CPUUtilization",
                        "Dimensions": [{"Name": "InstanceId", "Value": "i-1"}],
                    },
                    "Period": 3600,
                    "Stat": "Average",
                },
                "ReturnData": True,
            }],
            "StartTime": _T0,
            "EndTime": _ts(24),
        },
    )
    with stub:
        got = fetch_metric_values(client, [_q("i-1")], _T0, _ts(24), use_get_metric_data=True)
    stub.assert_no_pending_responses()
    assert got == {"i-1": [2.0, 3.0]}


def test_one_call_carries_up_to_500_series():
    cw = _CountingCloudWatch()
    queries = [_q(("i", n), value=f"i-{n}") for n in range(1001)]

    got = fetch_metric_values(cw, queries, _T0, _ts(24), use_get_metric_data=True)

    assert len(cw.calls) == math.ceil(1001 / 500) == 3
    assert [len(c) for c in cw.calls] == [500, 500, 1]
    assert len(got) == 1001 and all(v == [1.0] for v in got.values())
    for call in cw.calls:
        ids = [q["Id"] for q in call]
        assert len(set(ids)) == len(ids)
        assert all(_ID_PATTERN.match(i) for i in ids)


def test_next_token_pages_are_followed_and_joined():
    client, stub = _stubbed_cloudwatch()
    stub.add_response(
        "get_metric_data",
        {"MetricDataResults": [
            {"Id": "q0", "Timestamps": [_ts(2), _ts(1)], "Values": [5.0, 4.0],
             "StatusCode": "PartialData"},
            {"Id": "q1", "Timestamps": [_ts(0)], "Values": [9.0],
             "StatusCode": "Complete"},
        ], "NextToken": "page-2"},
        {"MetricDataQueries": ANY, "StartTime": ANY, "EndTime": ANY},
    )
    stub.add_response(
        "get_metric_data",
        {"MetricDataResults": [
            {"Id": "q0", "Timestamps": [_ts(0)], "Values": [3.0],
             "StatusCode": "Complete"},
        ]},
        {"MetricDataQueries": ANY, "StartTime": ANY, "EndTime": ANY,
         "NextToken": "page-2"},
    )
    with stub:
        got = fetch_metric_values(
            client, [_q("a", value="i-a"), _q("b", value="i-b")], _T0, _ts(24),
            use_get_metric_data=True)
    stub.assert_no_pending_responses()
    assert got == {"a": [3.0, 4.0, 5.0], "b": [9.0]}


def test_a_series_left_partial_is_unread_not_short():
    """A PartialData series with no page left would sum to less than the truth,
    which is how a busy resource reads as idle. It must come back None."""
    client, stub = _stubbed_cloudwatch()
    stub.add_response(
        "get_metric_data",
        {"MetricDataResults": [
            {"Id": "q0", "Timestamps": [_ts(0)], "Values": [1.0],
             "StatusCode": "PartialData"},
        ]},
        {"MetricDataQueries": ANY, "StartTime": ANY, "EndTime": ANY},
    )
    with stub:
        got = fetch_metric_values(client, [_q("a")], _T0, _ts(24), use_get_metric_data=True)
    assert got == {"a": None}


@pytest.mark.parametrize("status", ["Forbidden", "InternalError"])
def test_a_failed_series_is_unread_and_its_neighbours_are_not(status):
    client, stub = _stubbed_cloudwatch()
    stub.add_response(
        "get_metric_data",
        {"MetricDataResults": [
            {"Id": "q0", "Timestamps": [], "Values": [], "StatusCode": status,
             "Messages": [{"Code": status, "Value": "denied"}]},
            {"Id": "q1", "Timestamps": [], "Values": [], "StatusCode": "Complete"},
        ]},
        {"MetricDataQueries": ANY, "StartTime": ANY, "EndTime": ANY},
    )
    with stub:
        got = fetch_metric_values(
            client, [_q("a", value="i-a"), _q("b", value="i-b")], _T0, _ts(24),
            use_get_metric_data=True)
    # a failed; b was read and genuinely had nothing, which is a real answer.
    assert got == {"a": None, "b": []}


def test_a_series_missing_from_the_response_is_unread():
    client, stub = _stubbed_cloudwatch()
    stub.add_response(
        "get_metric_data", {"MetricDataResults": []},
        {"MetricDataQueries": ANY, "StartTime": ANY, "EndTime": ANY},
    )
    with stub:
        assert fetch_metric_values(
            client, [_q("a")], _T0, _ts(24), use_get_metric_data=True) == {"a": None}


def test_a_throttled_call_marks_only_its_own_chunk_unread():
    cw = _CountingCloudWatch(fail_call=2)
    queries = [_q(n, value=f"i-{n}") for n in range(600)]

    got = fetch_metric_values(cw, queries, _T0, _ts(24), use_get_metric_data=True)

    assert all(got[n] == [1.0] for n in range(500))
    assert all(got[n] is None for n in range(500, 600))


def test_a_repeated_next_token_ends_the_read_as_unread():
    """Paging must terminate. A token CloudWatch hands back twice means the
    pages cannot be trusted as a whole series, so the chunk is unread."""
    class _Loops:
        calls = 0

        def get_metric_data(self, **kw):
            self.calls += 1
            assert self.calls < 10, "paged forever"
            return {"MetricDataResults": [
                {"Id": "q0", "Timestamps": [_T0], "Values": [1.0],
                 "StatusCode": "PartialData"}], "NextToken": "same"}

    cw = _Loops()
    assert fetch_metric_values(
        cw, [_q("a")], _T0, _ts(24), use_get_metric_data=True) == {"a": None}
    assert cw.calls == 2


@pytest.mark.parametrize("batched", [False, True])
def test_a_client_that_is_not_boto_shaped_does_not_hang(batched):
    """A MagicMock client answers .get() with a truthy mock. The read must stop
    and report the series unread, not page forever or read a mock as data."""
    from unittest.mock import MagicMock
    assert fetch_metric_values(
        MagicMock(), [_q("a")], _T0, _ts(24), use_get_metric_data=batched) == {"a": None}


@pytest.mark.parametrize("batched", [False, True])
def test_points_keep_each_value_with_its_timestamp(batched):
    """fetch_metric_points is fetch_metric_values with the timestamps kept, for
    a caller that reports when a series was last published: the same order,
    the same [] for a quiet series and the same None for an unread one."""
    cw = _Metrics({("CPUUtilization", "i-1"): [3.0, 1.0, 2.0],
                   ("CPUUtilization", "i-denied"): "Forbidden"})
    queries = [_q("a", value="i-1"), _q("b", value="i-quiet"), _q("c", value="i-denied")]

    got = fetch_metric_points(cw, queries, _T0, _ts(24), use_get_metric_data=batched)

    assert got == {"a": [(_ts(0), 3.0), (_ts(1), 1.0), (_ts(2), 2.0)], "b": [], "c": None}
    assert fetch_metric_values(cw, queries, _T0, _ts(24), use_get_metric_data=batched) == {
        "a": [3.0, 1.0, 2.0], "b": [], "c": None}


# ── the detectors ────────────────────────────────────────────────────────────

class _Metrics:
    """A CloudWatch account with some series in it, served through both read
    APIs, counting calls to each.

    `series` maps (MetricName, *dimension values) to the values CloudWatch
    holds, or to a StatusCode string for a series it refuses (an AccessDenied
    exception from GetMetricStatistics). Anything absent is a series with no
    datapoints, which is what CloudWatch answers for a metric nobody published.
    `latency` delays every GetMetricStatistics call.
    """

    def __init__(self, series: dict | None = None, latency: float = 0.0):
        self.series = series or {}
        self.latency = latency
        self.statistics_calls = 0
        self.data_calls = 0
        self._lock = threading.Lock()

    @staticmethod
    def _key(metric: str, dims: list[dict]) -> tuple:
        return (metric, *(d["Value"] for d in dims))

    def get_metric_statistics(self, **kw):
        with self._lock:
            self.statistics_calls += 1
        time.sleep(self.latency)
        got = self.series.get(self._key(kw["MetricName"], kw["Dimensions"]), [])
        if isinstance(got, str):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": got}},
                              "GetMetricStatistics")
        stat = kw["Statistics"][0]
        return {"Label": kw["MetricName"], "Datapoints": [
            {"Timestamp": _ts(i), stat: v, "Unit": "None"} for i, v in enumerate(got)
        ]}

    def get_metric_data(self, **kw):
        with self._lock:
            self.data_calls += 1
        results = []
        for q in kw["MetricDataQueries"]:
            metric = q["MetricStat"]["Metric"]
            got = self.series.get(self._key(metric["MetricName"], metric["Dimensions"]), [])
            if isinstance(got, str):
                results.append({"Id": q["Id"], "Timestamps": [], "Values": [],
                                "StatusCode": got})
            else:
                results.append({"Id": q["Id"], "Timestamps": [_ts(i) for i in range(len(got))],
                                "Values": list(got), "StatusCode": "Complete"})
        return {"MetricDataResults": results, "Messages": []}


class _Pages:
    """A describe_* client: one page set whatever the operation name."""

    def __init__(self, pages: list[dict]):
        self._pages = pages

    def get_paginator(self, _name: str):
        pages = self._pages

        class _P:
            def paginate(self, **_kw):
                return list(pages)
        return _P()


def _nat_pages(n: int) -> list[dict]:
    return [{"NatGateways": [
        {"NatGatewayId": f"nat-{i:04d}", "VpcId": "vpc-1", "SubnetId": "subnet-1", "Tags": []}
        for i in range(n)
    ]}]


def _nat_run():
    from finops.analyzers import waste

    busy = 40 * 1024 ** 3
    cw = _Metrics({
        ("BytesOutToDestination", "nat-0001"): [busy] * 7,
        ("BytesOutToDestination", "nat-0002"): "Forbidden",
    })
    findings = waste.check_nat_gateways(_Pages(_nat_pages(600)), cw, region="us-east-1")
    # nat-0001 carries 40 GB/day, nat-0002 could not be read, the other 598
    # read fine and empty.
    assert len(findings) == 598
    assert not {"nat-0001", "nat-0002"} & {f["resource_id"] for f in findings}
    return cw


def test_nat_gateways_stay_on_the_free_api_by_default():
    cw = _nat_run()
    assert (cw.data_calls, cw.statistics_calls) == (0, 600)


def test_nat_gateways_batch_when_opted_in(opted_in):
    cw = _nat_run()
    assert (cw.data_calls, cw.statistics_calls) == (math.ceil(600 / 500), 0)


def _ec2_pages(n: int) -> list[dict]:
    old = datetime.now(timezone.utc) - timedelta(days=120)
    return [{"Reservations": [{"Instances": [
        {"InstanceId": f"i-{i:04d}", "InstanceType": "m5.large", "LaunchTime": old,
         "State": {"Name": "running"}, "Tags": []}
        for i in range(n)
    ]}]}]


def _ec2_run(latency: float = 0.0, n: int = 200):
    from finops.analyzers import waste

    series = {("CPUUtilization", f"i-{i:04d}"): [1.0] * 336 for i in range(n)}
    series[("NetworkOut", "i-0007")] = [500 * 1024 ** 2] * 336  # busy on the wire
    series[("CPUUtilization", "i-0008")] = [60.0] * 336         # busy on CPU
    series[("NetworkOut", "i-0009")] = "Forbidden"              # guard unreadable
    cw = _Metrics(series, latency=latency)

    findings = waste.check_idle_ec2(_Pages(_ec2_pages(n)), cw, region="us-east-1")

    flagged = {f["resource_id"] for f in findings}
    assert len(flagged) == n - 3
    assert not {"i-0007", "i-0008", "i-0009"} & flagged
    return cw


def test_idle_ec2_stays_on_the_free_api_by_default():
    """The reads the per-instance loop made, no more: CPU for all 200, and
    NetworkOut for the 199 whose CPU came back low."""
    cw = _ec2_run()
    assert (cw.data_calls, cw.statistics_calls) == (0, 200 + 199)


def test_idle_ec2_batches_when_opted_in(opted_in):
    cw = _ec2_run()
    assert cw.statistics_calls == 0
    assert cw.data_calls == math.ceil(200 / 500) + math.ceil(199 / 500)


def test_idle_ec2_reads_concurrently():
    """40 instances, 79 reads at 50ms each: ~4s in sequence, ~0.5s eight at a
    time (ceil(40 / 8) CPU rounds, then ceil(39 / 8) NetworkOut rounds)."""
    t0 = time.monotonic()
    cw = _ec2_run(latency=0.05, n=40)
    elapsed = time.monotonic() - t0
    assert cw.statistics_calls == 79
    assert elapsed >= (math.ceil(40 / 8) + math.ceil(39 / 8)) * 0.05 * 0.9
    assert elapsed < 79 * 0.05 / 3, f"{elapsed:.2f}s is not concurrent"


def _lambda_pages(n: int) -> list[dict]:
    return [{"Functions": [
        {"FunctionName": f"fn-{i:04d}", "MemorySize": 1024, "Runtime": "python3.12",
         "CodeSize": 1024}
        for i in range(n)
    ]}]


def _lambda_run():
    from finops.analyzers import waste

    series = {("Invocations", f"fn-{i:04d}"): [10.0] for i in range(300)}
    series[("Invocations", "fn-0003")] = []                   # read, never invoked
    series[("memory_utilization", "fn-0004")] = [20.0]        # 205 MB of 1024
    series[("Invocations", "fn-0005")] = "InternalError"      # unread, not zero
    cw = _Metrics(series)

    findings = waste.check_lambda_memory(_Pages(_lambda_pages(300)), cw, region="us-east-1")

    assert {(f["resource_id"], f["waste_type"]) for f in findings} == {
        ("fn-0003", "lambda_zero_invocations"),
        ("fn-0004", "lambda_memory_overprovisioned"),
    }
    return cw


def test_lambda_stays_on_the_free_api_by_default():
    """Invocations for all 300, memory for the 299 not read as zero."""
    cw = _lambda_run()
    assert (cw.data_calls, cw.statistics_calls) == (0, 300 + 299)


def test_lambda_batches_when_opted_in(opted_in):
    cw = _lambda_run()
    assert cw.statistics_calls == 0
    assert cw.data_calls == math.ceil(300 / 500) + math.ceil(299 / 500)


def test_lambda_reads_ask_each_namespace_by_its_own_dimension_name():
    """AWS/Lambda publishes Invocations under FunctionName. Lambda Insights
    publishes memory_utilization in LambdaInsights under function_name. Asked by
    the wrong name, CloudWatch answers a successful read of nothing, so the
    memory finding never fired from real data. _Metrics matches on dimension
    values only, which is how that went unseen; this fake answers a series only
    when it is asked for by the name its publisher uses."""
    from finops.analyzers import waste

    published = {
        ("AWS/Lambda", "Invocations", (("FunctionName", "fn-0000"),)): 10.0,
        ("LambdaInsights", "memory_utilization", (("function_name", "fn-0000"),)): 20.0,
    }

    class _ByDimensionName:
        def __init__(self):
            self.asked: list[tuple] = []

        def get_metric_statistics(self, **kw):
            series = (kw["Namespace"], kw["MetricName"],
                      tuple((d["Name"], d["Value"]) for d in kw["Dimensions"]))
            self.asked.append(series)
            value = published.get(series)
            stat = kw["Statistics"][0]
            return {"Datapoints": [] if value is None else [{"Timestamp": _T0, stat: value}]}

    cw = _ByDimensionName()
    findings = waste.check_lambda_memory(_Pages(_lambda_pages(1)), cw, region="us-east-1")

    assert sorted(cw.asked) == sorted(published)
    assert [(f["resource_id"], f["waste_type"]) for f in findings] == [
        ("fn-0000", "lambda_memory_overprovisioned")]


def _rds_pages(n: int, db_class: str = "db.m5.xlarge") -> list[dict]:
    return [{"DBInstances": [
        {"DBInstanceIdentifier": f"db-{i:04d}", "DBInstanceClass": db_class,
         "Engine": "mysql", "DBInstanceStatus": "available", "MultiAZ": False}
        for i in range(n)
    ]}]


def _rds_rightsizing_run():
    from finops.analyzers import waste

    series = {("CPUUtilization", f"db-{i:04d}"): [4.0] * 48 for i in range(250)}
    series[("CPUUtilization", "db-0002")] = [80.0] * 48   # busy
    series[("CPUUtilization", "db-0003")] = [4.0] * 23    # too little data
    series[("CPUUtilization", "db-0004")] = "Forbidden"   # unread
    cw = _Metrics(series)

    findings = waste.check_rds_rightsizing(_Pages(_rds_pages(250)), cw, region="us-east-1")

    flagged = {f["resource_id"] for f in findings}
    assert len(flagged) == 247
    assert not {"db-0002", "db-0003", "db-0004"} & flagged
    # Same money as the per-resource path: (0.342 - 0.171) * 730.
    assert {f["estimated_monthly_savings"] for f in findings} == {124.83}
    return cw


def test_rds_rightsizing_stays_on_the_free_api_by_default():
    cw = _rds_rightsizing_run()
    assert (cw.data_calls, cw.statistics_calls) == (0, 250)


def test_rds_rightsizing_batches_when_opted_in(opted_in):
    cw = _rds_rightsizing_run()
    assert (cw.data_calls, cw.statistics_calls) == (math.ceil(250 / 500), 0)


def _rds_idle_run():
    from finops.analyzers import waste

    series = {("DatabaseConnections", f"db-{i:04d}"): [0.0] * 14 for i in range(250)}
    series[("DatabaseConnections", "db-0002")] = [3.0] * 14   # in use
    series[("DatabaseConnections", "db-0003")] = [0.0] * 6    # too little data
    series[("DatabaseConnections", "db-0004")] = "InternalError"
    cw = _Metrics(series)

    findings = waste.check_rds_idle(_Pages(_rds_pages(250)), cw, region="us-east-1")

    flagged = {f["resource_id"] for f in findings}
    assert len(flagged) == 247
    assert not {"db-0002", "db-0003", "db-0004"} & flagged
    return cw


def test_rds_idle_stays_on_the_free_api_by_default():
    cw = _rds_idle_run()
    assert (cw.data_calls, cw.statistics_calls) == (0, 250)


def test_rds_idle_batches_when_opted_in(opted_in):
    cw = _rds_idle_run()
    assert (cw.data_calls, cw.statistics_calls) == (math.ceil(250 / 500), 0)


class _Inventory:
    """elbv2 and elb describe clients, either of which can fail to list."""

    def __init__(self, pages=None, error: Exception | None = None):
        self._pages, self._error = pages or [], error

    def get_paginator(self, _name):
        pages, error = self._pages, self._error

        class _P:
            def paginate(self, **_kw):
                if error:
                    raise error
                return list(pages)
        return _P()


def _alb(i: int, lb_type: str = "application") -> dict:
    kind = "app" if lb_type == "application" else "net"
    return {"LoadBalancerName": f"lb-{i:04d}", "Type": lb_type,
            "LoadBalancerArn": f"arn:aws:elasticloadbalancing:us-east-1:1:"
                               f"loadbalancer/{kind}/lb-{i:04d}/abc",
            "State": {"Code": "active"}}


def _lb_run():
    from finops.analyzers import waste

    v2 = [_alb(i) for i in range(300)] + [_alb(i, "network") for i in range(300, 400)]
    classic = [{"LoadBalancerName": f"clb-{i:04d}"} for i in range(200)]
    # Quiet NLBs report zero new flows. An empty flow series is not read as
    # idle (see test_waste_lb_and_s3_reads.py), so each one has its zeros.
    quiet_nlbs = {("NewFlowCount", f"net/lb-{i:04d}/abc"): [0.0] * 14 for i in range(300, 400)}
    cw = _Metrics({
        **quiet_nlbs,
        ("RequestCount", "app/lb-0001/abc"): [1e6] * 14,       # busy ALB
        ("NewFlowCount", "net/lb-0301/abc"): [5000.0] * 14,    # busy NLB
        ("RequestCount", "clb-0001"): [1e6] * 14,              # busy classic
        ("RequestCount", "app/lb-0002/abc"): "Forbidden",      # unread
        ("RequestCount", "clb-0002"): "InternalError",         # unread
    })

    findings = waste.check_idle_load_balancers(
        _Inventory([{"LoadBalancers": v2}]),
        _Inventory([{"LoadBalancerDescriptions": classic}]), cw, region="us-east-1")

    flagged = {f["lb_name"] for f in findings}
    assert len(flagged) == 600 - 5
    assert not {"lb-0001", "lb-0301", "clb-0001", "lb-0002", "clb-0002"} & flagged
    return cw


def test_load_balancers_stay_on_the_free_api_by_default():
    cw = _lb_run()
    assert (cw.data_calls, cw.statistics_calls) == (0, 600)


def test_load_balancers_batch_when_opted_in(opted_in):
    cw = _lb_run()
    assert (cw.data_calls, cw.statistics_calls) == (math.ceil(600 / 500), 0)


def test_one_failed_lb_inventory_still_reports_the_other():
    from finops.analyzers import waste

    cw = _Metrics()
    findings = waste.check_idle_load_balancers(
        _Inventory(error=RuntimeError("AccessDenied")),
        _Inventory([{"LoadBalancerDescriptions": [{"LoadBalancerName": "clb-0"}]}]),
        cw, region="us-east-1")
    assert [f["lb_name"] for f in findings] == ["clb-0"]

    with pytest.raises(RuntimeError):
        waste.check_idle_load_balancers(
            _Inventory(error=RuntimeError("AccessDenied")),
            _Inventory(error=RuntimeError("AccessDenied")), cw, region="us-east-1")


class _ECS:
    """One cluster of Fargate services, each on a 1024 CPU unit task."""

    def __init__(self, n: int):
        self._arns = [f"arn:aws:ecs:us-east-1:1:service/prod/svc-{i:04d}" for i in range(n)]

    def get_paginator(self, name):
        arns = self._arns

        class _P:
            def paginate(self, **_kw):
                if name == "list_clusters":
                    return [{"clusterArns": ["arn:aws:ecs:us-east-1:1:cluster/prod"]}]
                return [{"serviceArns": arns}]
        return _P()

    def describe_services(self, cluster, services):
        return {"services": [
            {"serviceName": a.rsplit("/", 1)[-1], "serviceArn": a, "launchType": "FARGATE",
             "taskDefinition": "td:1", "desiredCount": 2}
            for a in services
        ]}

    def describe_task_definition(self, taskDefinition):
        return {"taskDefinition": {"cpu": "1024", "memory": "2048"}}


def _ecs_run():
    from finops.analyzers import waste

    series = {("CpuUtilized", "prod", f"svc-{i:04d}"): [50.0] * 48 for i in range(120)}
    series[("CpuUtilized", "prod", "svc-0001")] = [900.0] * 48   # busy
    series[("CpuUtilized", "prod", "svc-0002")] = [50.0] * 10    # too little data
    series[("CpuUtilized", "prod", "svc-0003")] = "Forbidden"    # unread
    cw = _Metrics(series)

    findings = waste.check_ecs_task_rightsizing(_ECS(120), cw, region="us-east-1")

    flagged = {f["service"] for f in findings}
    assert len(flagged) == 117
    assert not {"svc-0001", "svc-0002", "svc-0003"} & flagged
    # 512 units saved on 2 tasks: 0.5 vCPU * 0.04048 * 730 * 2.
    assert {f["estimated_monthly_savings"] for f in findings} == {29.55}
    return cw


def test_ecs_stays_on_the_free_api_by_default():
    cw = _ecs_run()
    assert (cw.data_calls, cw.statistics_calls) == (0, 120)


def test_ecs_batches_when_opted_in(opted_in):
    cw = _ecs_run()
    assert (cw.data_calls, cw.statistics_calls) == (math.ceil(120 / 500), 0)


_TB = 1000 * 1024 ** 3


class _S3:
    """list_buckets plus get_bucket_location, as the real client answers them:
    LocationConstraint is None for us-east-1 and 'EU' for old eu-west-1 buckets."""

    def __init__(self, locations: dict[str, str | None]):
        self.locations = locations
        self.location_calls = 0

    def list_buckets(self):
        return {"Buckets": [{"Name": n, "CreationDate": _T0} for n in self.locations]}

    def get_bucket_location(self, Bucket):
        self.location_calls += 1
        return {"LocationConstraint": self.locations[Bucket]}


class _S3Denied(_S3):
    def get_bucket_location(self, Bucket):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}},
                          "GetBucketLocation")


def _s3_series(names, size=_TB, gets=1.0, objects=1000.0) -> dict:
    out = {}
    for n in names:
        out[("BucketSizeBytes", n, "StandardStorage")] = [size] * 30
        out[("GetRequests", n, "AllRequests")] = [gets] * 30
        out[("NumberOfObjects", n, "AllStorageTypes")] = [objects] * 30
    return out


def _s3_run():
    from finops.analyzers import waste

    names = [f"bucket-{i:04d}" for i in range(200)]
    series = _s3_series(names)
    series[("GetRequests", "bucket-0001", "AllRequests")] = [5000.0] * 30   # hot
    series[("GetRequests", "bucket-0002", "AllRequests")] = "Forbidden"     # unread
    series[("BucketSizeBytes", "bucket-0003", "StandardStorage")] = "InternalError"
    cw = _Metrics(series)

    findings = waste.check_s3_storage_class(
        _S3({n: None for n in names}), cw, region="us-east-1")

    flagged = {f["resource_id"] for f in findings}
    assert len(flagged) == 197
    assert not {"bucket-0001", "bucket-0002", "bucket-0003"} & flagged
    # 1000 GB: $23.00 STANDARD - $12.50 IT storage - $0.0025 monitoring.
    assert {f["estimated_monthly_savings"] for f in findings} == {10.5}
    assert {f["recommendation"] for f in findings} == {"INTELLIGENT_TIERING"}
    return cw


def test_s3_storage_class_stays_on_the_free_api_by_default():
    """Size for all 200, GETs for the 199 with a readable size, object count
    for the 197 confirmed low access."""
    cw = _s3_run()
    assert (cw.data_calls, cw.statistics_calls) == (0, 200 + 199 + 197)


def test_s3_storage_class_batches_when_opted_in(opted_in):
    cw = _s3_run()
    assert cw.statistics_calls == 0
    assert cw.data_calls == 3 * math.ceil(200 / 500)


def _regional_cloudwatch() -> dict[str, _Metrics]:
    """Each bucket's storage metrics exist only in its own region, as in S3."""
    return {
        "us-east-1": _Metrics(_s3_series(["logs-virginia"])),
        "eu-west-1": _Metrics(_s3_series(["logs-ireland"])),
        "us-west-2": _Metrics(_s3_series(["logs-oregon", "logs-listed-region"])),
    }


def test_s3_storage_class_reads_each_bucket_in_its_own_region():
    """Before, every bucket was read from us-east-1 CloudWatch, which answers a
    bucket in another region with a successful read of nothing, so every bucket
    outside us-east-1 was silently skipped."""
    from finops.analyzers import waste

    cws = _regional_cloudwatch()
    s3 = _S3({"logs-virginia": None, "logs-ireland": "EU", "logs-oregon": "us-west-2"})
    # ListBuckets can carry the region itself; then no location call is needed.
    listed = s3.list_buckets()["Buckets"] + [
        {"Name": "logs-listed-region", "BucketRegion": "us-west-2"}]
    s3.list_buckets = lambda: {"Buckets": listed}
    built: list[str] = []

    def _cw_for(region):
        built.append(region)
        return cws[region]

    findings = waste.check_s3_storage_class(
        s3, cws["us-east-1"], region="us-east-1", cw_client_for_region=_cw_for)

    assert {(f["resource_id"], f["region"]) for f in findings} == {
        ("logs-virginia", "us-east-1"),
        ("logs-ireland", "eu-west-1"),
        ("logs-oregon", "us-west-2"),
        ("logs-listed-region", "us-west-2"),
    }
    assert s3.location_calls == 3
    # A client per region, the scan's own region reuses the client it has, and
    # every read went to the free API in the bucket's own region.
    assert set(built) == {"eu-west-1", "us-west-2"}
    assert [cws[r].statistics_calls for r in ("us-east-1", "eu-west-1", "us-west-2")] == [3, 3, 6]
    assert sum(cw.data_calls for cw in cws.values()) == 0


def test_an_unreadable_bucket_location_falls_back_to_the_scan_region():
    from finops.analyzers import waste

    cws = _regional_cloudwatch()
    findings = waste.check_s3_storage_class(
        _S3Denied({"logs-virginia": None}), cws["us-east-1"], region="us-east-1",
        cw_client_for_region=lambda r: cws[r])
    assert [(f["resource_id"], f["region"]) for f in findings] == [
        ("logs-virginia", "us-east-1")]


def test_the_deep_audit_hands_the_s3_check_a_client_per_region():
    from finops.analyzers import optimizer

    cws = _regional_cloudwatch()
    s3 = _S3({"logs-virginia": None, "logs-ireland": "EU"})

    class _Session:
        def client(self, service, region_name=None, **_kw):
            return {"s3": s3, "cloudwatch": cws.get(region_name)}[service]

    findings = optimizer._audit_region(_Session(), "us-east-1", frozenset({"s3"}))

    assert findings.checks_completed == {"s3"}
    assert {(f["resource_id"], f["region"]) for f in findings} == {
        ("logs-virginia", "us-east-1"), ("logs-ireland", "eu-west-1")}


class _S3WithTiering(_S3):
    def list_bucket_intelligent_tiering_configurations(self, Bucket):
        return {"IntelligentTieringConfigurationList": [{"Id": "default"}]}


def _it_series(names, objects=1_000_000.0, size=10 * 1024 ** 3) -> dict:
    out = {}
    for n in names:
        out[("NumberOfObjects", n, "AllStorageTypes")] = [objects]
        out[("BucketSizeBytes", n, "IntelligentTieringFAStorage")] = [size]
    return out


def _it_run():
    """60 Intelligent-Tiering buckets in us-east-1 and 80 in eu-west-1. Before,
    all seven series per bucket were read from us-east-1, so every bucket in any
    other region came back 'enable bucket metrics' although its metrics were
    there all along."""
    import asyncio
    from finops.recommendations.s3_intelligent_tiering import audit_s3_intelligent_tiering

    virginia = [f"it-va-{i:03d}" for i in range(60)]
    ireland = [f"it-ie-{i:03d}" for i in range(80)]
    cws = {"us-east-1": _Metrics(_it_series(virginia)),
           "eu-west-1": _Metrics(_it_series(ireland))}
    s3 = _S3WithTiering({**{n: None for n in virginia}, **{n: "EU" for n in ireland}})

    class _Session:
        def client(self, service, region_name=None, **_kw):
            return s3 if service == "s3" else cws[region_name]

    class _Connector:
        _session = _Session()

    results = asyncio.run(audit_s3_intelligent_tiering(_Connector()))

    assert len(results) == 140
    # 1M objects of 10 KB each: monitoring outweighs tiering, in both regions.
    assert {r["recommendation"] for r in results} == {
        "LIKELY_WASTE_monitoring_exceeds_savings"}
    return cws


def test_intelligent_tiering_audit_reads_each_bucket_in_its_region_for_free():
    cws = _it_run()
    assert [(cws[r].data_calls, cws[r].statistics_calls) for r in ("us-east-1", "eu-west-1")] \
        == [(0, 60 * 7), (0, 80 * 7)]


def test_intelligent_tiering_audit_batches_per_region_when_opted_in(opted_in):
    cws = _it_run()
    assert [(cws[r].data_calls, cws[r].statistics_calls) for r in ("us-east-1", "eu-west-1")] \
        == [(math.ceil(60 * 7 / 500), 0), (math.ceil(80 * 7 / 500), 0)]


@pytest.mark.parametrize("location, expected", [
    (None, "us-east-1"),        # us-east-1 buckets report no constraint
    ("", "us-east-1"),
    ("EU", "eu-west-1"),        # the legacy constraint for eu-west-1
    ("ap-south-1", "ap-south-1"),
])
def test_bucket_region_reads_location_constraint(location, expected):
    from finops.analyzers.cloudwatch import s3_bucket_region

    assert s3_bucket_region(_S3({"b": location}), {"Name": "b"}, "fallback") == expected


def test_bucket_region_prefers_the_listed_region_and_falls_back_when_unreadable():
    from unittest.mock import MagicMock
    from finops.analyzers.cloudwatch import s3_bucket_region

    s3 = _S3({"b": "EU"})
    assert s3_bucket_region(s3, {"Name": "b", "BucketRegion": "sa-east-1"}, "x") == "sa-east-1"
    assert s3.location_calls == 0
    assert s3_bucket_region(_S3Denied({"b": None}), {"Name": "b"}, "fallback") == "fallback"
    # A client that is not boto shaped answers with a mock, which is not a region.
    assert s3_bucket_region(MagicMock(), {"Name": "b"}, "fallback") == "fallback"
