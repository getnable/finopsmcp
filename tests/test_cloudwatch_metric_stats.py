"""get_metric_stats reads on the free API.

It backs the per-instance and per-database utilization profiles, five reads
each, and used GetMetricData, which bills every metric requested with no free
tier. The Stubber checks each request against botocore's real service model.
"""
from __future__ import annotations

from datetime import datetime, timezone

import boto3
from botocore.stub import ANY, Stubber

from finops.analyzers.cloudwatch import get_metric_stats

DIMS = [{"Name": "InstanceId", "Value": "i-0abc"}]
T1 = datetime(2026, 9, 20, tzinfo=timezone.utc)
T2 = datetime(2026, 9, 21, tzinfo=timezone.utc)


def _cw():
    return boto3.client("cloudwatch", region_name="us-east-1",
                        aws_access_key_id="x", aws_secret_access_key="y")  # pragma: allowlist secret


def _params(**stat_kw):
    return {"Namespace": "AWS/EC2", "MetricName": "CPUUtilization", "Dimensions": DIMS,
            "StartTime": ANY, "EndTime": ANY, "Period": 86400, **stat_kw}


def test_standard_stats_are_one_free_call():
    cw = _cw()
    with Stubber(cw) as st:
        st.add_response("get_metric_statistics", {"Label": "CPUUtilization", "Datapoints": [
            {"Timestamp": T2, "Average": 30.0, "Maximum": 80.0, "Minimum": 2.0, "Unit": "Percent"},
            {"Timestamp": T1, "Average": 10.0, "Maximum": 40.0, "Minimum": 1.0, "Unit": "Percent"},
        ]}, _params(Statistics=["Average", "Maximum", "Minimum"]))
        out = get_metric_stats(cw, "AWS/EC2", "CPUUtilization", DIMS)
        st.assert_no_pending_responses()
    assert out == {"average": 20.0, "maximum": 80.0, "minimum": 1.0,
                   "datapoints": 2, "unit": "Percent"}


def test_percentiles_are_a_second_call():
    cw = _cw()
    with Stubber(cw) as st:
        st.add_response("get_metric_statistics", {"Datapoints": [
            {"Timestamp": T1, "Average": 10.0, "Maximum": 40.0, "Minimum": 1.0, "Unit": "Percent"},
        ]}, _params(Statistics=["Average", "Maximum", "Minimum"]))
        st.add_response("get_metric_statistics", {"Datapoints": [
            {"Timestamp": T1, "ExtendedStatistics": {"p99": 35.0, "p95": 30.0}, "Unit": "Percent"},
            {"Timestamp": T2, "ExtendedStatistics": {"p99": 60.0, "p95": 50.0}, "Unit": "Percent"},
        ]}, _params(ExtendedStatistics=["p95", "p99"]))
        out = get_metric_stats(cw, "AWS/EC2", "CPUUtilization", DIMS, extended_stats=["p95", "p99"])
        st.assert_no_pending_responses()
    assert out["p95"] == 50.0 and out["p99"] == 60.0


def test_a_failed_read_is_unread_not_zero():
    cw = _cw()
    with Stubber(cw) as st:
        st.add_client_error("get_metric_statistics", "AccessDenied")
        out = get_metric_stats(cw, "AWS/EC2", "CPUUtilization", DIMS)
    assert out["average"] is None and out["datapoints"] == 0


def test_never_calls_get_metric_data():
    class _CW:
        def get_metric_data(self, **kw):
            raise AssertionError("billed API called")

        def get_metric_statistics(self, **kw):
            return {"Datapoints": []}
    assert get_metric_stats(_CW(), "AWS/RDS", "CPUUtilization", DIMS)["datapoints"] == 0
