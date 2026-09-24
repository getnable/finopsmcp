"""CloudWatch reads are batched, and a batch never turns a failed read into zero.

The waste detectors used to call get_metric_statistics once per resource per
metric, inside the describe loop. 200 instances with two metrics each was 400
sequential round trips for one check in one region. GetMetricData carries 500
series per call, so the same read is one call.

Batching changes what a failure looks like. One throttled call now covers every
resource in the chunk, and CloudWatch can also answer a single series with
Forbidden, InternalError or PartialData while the rest come back Complete. Each
of those has to reach the detector as "unread", the same thing an exception from
get_metric_statistics was, and never as an empty series that reads as idle.

The fakes answer with the real GetMetricData response shape (MetricDataResults
with Id, Timestamps, Values, StatusCode, and a top-level NextToken), and the
botocore Stubber tests validate the request against the real service model.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

from finops.analyzers.cloudwatch import MetricQuery, fetch_metric_values


_T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
_ID_PATTERN = re.compile(r"^[a-z][a-zA-Z0-9_]*$")


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
    """Answers every series Complete with one datapoint, and counts calls."""

    def __init__(self, fail_call: int | None = None):
        self.calls: list[list[dict]] = []
        self._fail_call = fail_call

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


# ── the helper ───────────────────────────────────────────────────────────────

def test_the_request_matches_the_service_model():
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
        got = fetch_metric_values(client, [_q("i-1")], _T0, _ts(24))
    stub.assert_no_pending_responses()
    # Oldest first, whatever order CloudWatch returned them in.
    assert got == {"i-1": [2.0, 3.0]}


def test_one_call_carries_up_to_500_series():
    cw = _CountingCloudWatch()
    queries = [_q(("i", n), value=f"i-{n}") for n in range(1001)]

    got = fetch_metric_values(cw, queries, _T0, _ts(24))

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
            client, [_q("a", value="i-a"), _q("b", value="i-b")], _T0, _ts(24))
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
        got = fetch_metric_values(client, [_q("a")], _T0, _ts(24))
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
            client, [_q("a", value="i-a"), _q("b", value="i-b")], _T0, _ts(24))
    # a failed; b was read and genuinely had nothing, which is a real answer.
    assert got == {"a": None, "b": []}


def test_a_series_missing_from_the_response_is_unread():
    client, stub = _stubbed_cloudwatch()
    stub.add_response(
        "get_metric_data", {"MetricDataResults": []},
        {"MetricDataQueries": ANY, "StartTime": ANY, "EndTime": ANY},
    )
    with stub:
        assert fetch_metric_values(client, [_q("a")], _T0, _ts(24)) == {"a": None}


def test_a_throttled_call_marks_only_its_own_chunk_unread():
    cw = _CountingCloudWatch(fail_call=2)
    queries = [_q(n, value=f"i-{n}") for n in range(600)]

    got = fetch_metric_values(cw, queries, _T0, _ts(24))

    assert all(got[n] == [1.0] for n in range(500))
    assert all(got[n] is None for n in range(500, 600))


def test_no_queries_means_no_calls():
    cw = _CountingCloudWatch()
    assert fetch_metric_values(cw, [], _T0, _ts(24)) == {}
    assert cw.calls == []
