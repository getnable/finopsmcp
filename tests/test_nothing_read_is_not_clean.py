"""Nothing read is not $0, not "0 flagged" and not "all clean".

Each test here is a dogfood finding from persona runs of 0.8.216: a tool that
could not read its source answered as if it had read it and found nothing. The
rule every test pins is the same: when nothing was read, say what was not read
and how to fix it.
"""
from __future__ import annotations

import asyncio



def _fake_boto3_clients(monkeypatch, **by_service):
    """Route boto3.client to the given fakes; any other service is an error."""
    import boto3

    def _client(service, *args, **kwargs):
        fake = by_service.get(service)
        if fake is None:
            raise AssertionError(f"test tried to build a real boto3 {service} client")
        return fake

    monkeypatch.setattr(boto3, "client", _client)


# ── 1. Rightsizing: Compute Optimizer not read is not "all clean" ─────────────

class _STS:
    def get_caller_identity(self):
        return {"Account": "111122223333"}


class _CONotOptedIn:
    def get_enrollment_status(self):
        return {"status": "Inactive"}


class _CODenied:
    def get_enrollment_status(self):
        from botocore.exceptions import ClientError
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
            "GetEnrollmentStatus")


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return iter(self._pages)


class _EC2:
    def __init__(self, instances=()):
        self._instances = list(instances)

    def describe_regions(self, **kwargs):
        return {"Regions": [{"RegionName": "us-east-1"}]}

    def get_paginator(self, name):
        return _Paginator([{"Reservations": [{"Instances": self._instances}]}])


class _CWNoData:
    def get_metric_statistics(self, **kwargs):
        return {"Datapoints": []}


def _run_rightsizing(monkeypatch, co, ec2, cw=None):
    from finops.recommendations import effective_savings
    import finops.server as server

    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    monkeypatch.setattr(effective_savings, "detect_savings_context", lambda *a, **k: None)
    _fake_boto3_clients(monkeypatch, sts=_STS(), **{"compute-optimizer": co},
                        ec2=ec2, cloudwatch=cw or _CWNoData())
    return asyncio.run(server.get_rightsizing_recommendations())


def test_rightsizing_not_opted_in_and_no_instances_is_not_all_clean(monkeypatch):
    out = _run_rightsizing(monkeypatch, _CONotOptedIn(), _EC2())

    note = out["source"]["note"]
    assert "All recommendations sourced from AWS Compute Optimizer" not in note, (
        "zero results from a Compute Optimizer that was never read were "
        f"reported as sourced from it: {note!r}")
    assert out["coverage"]["compute_optimizer"]["status"] == "not_opted_in"
    assert out["status"] == "not_evaluated"
    assert "Nothing was evaluated" in out["message"]
    assert "compute-optimizer" in out["message"]  # the opt-in link


def test_rightsizing_compute_optimizer_error_carries_its_class(monkeypatch):
    out = _run_rightsizing(monkeypatch, _CODenied(), _EC2())

    co = out["coverage"]["compute_optimizer"]
    assert co["status"] == "error"
    assert co["error_class"] == "AccessDeniedException"
    assert "AccessDeniedException" in out["source"]["note"]


def test_rightsizing_instances_without_metrics_are_skipped_not_downsized(monkeypatch):
    """No CPU datapoints used to read as 0% CPU, the strongest downsize signal."""
    inst = {"InstanceId": "i-1", "InstanceType": "m5.2xlarge", "Tags": []}
    out = _run_rightsizing(monkeypatch, _CONotOptedIn(), _EC2([inst]))

    assert out["total_instances_flagged"] == 0, out["recommendations"]
    cw = out["coverage"]["cloudwatch_fallback"]
    assert (cw["instances_found"], cw["instances_evaluated"], cw["instances_skipped"]) == (1, 0, 1)
    assert out["status"] == "not_evaluated"
    assert "evaluated 0 of 1" in out["source"]["note"]


def test_rightsizing_read_and_nothing_found_says_it_was_read(monkeypatch):
    class _CW:
        def get_metric_statistics(self, **kwargs):
            return {"Datapoints": [{"Average": 70.0, "Maximum": 90.0}]}

    inst = {"InstanceId": "i-1", "InstanceType": "m5.2xlarge", "Tags": []}
    out = _run_rightsizing(monkeypatch, _CONotOptedIn(), _EC2([inst]), _CW())

    assert out["total_instances_flagged"] == 0
    assert out["evaluated"] is True
    assert "status" not in out
    assert "evaluated 1 of 1" in out["source"]["note"]
