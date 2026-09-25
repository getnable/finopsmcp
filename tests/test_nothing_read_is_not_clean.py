"""Nothing read is not $0, not "0 flagged" and not "all clean".

Each test here is a dogfood finding from persona runs of 0.8.216: a tool that
could not read its source answered as if it had read it and found nothing. The
rule every test pins is the same: when nothing was read, say what was not read
and how to fix it.
"""
from __future__ import annotations

import asyncio
from datetime import date as _date

from finops.connectors.base import CostSummary


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


# ── 2. Board summary, exported report, dashboard: no $0 when nothing was read ─

def _empty_summary(start=None, end=None) -> CostSummary:
    """What a connector returns when the provider answered with no rows."""
    return CostSummary(provider="aws", start_date=start or _date(2026, 9, 1),
                       end_date=end or _date(2026, 9, 25), total_usd=0.0,
                       by_service={}, by_account={}, by_region={}, entries=[])


class _NoRowsConnector:
    async def is_configured(self):
        return True

    async def get_costs(self, start, end, granularity="MONTHLY", **kw):
        return _empty_summary(start, end)

    async def list_accounts(self):
        return [{"id": "123456789012", "name": "123456789012"}]


class _FailingConnector(_NoRowsConnector):
    async def get_costs(self, start, end, granularity="MONTHLY", **kw):
        raise RuntimeError("AWS credentials are invalid. Run: finops setup aws")


def _board_env(monkeypatch, tmp_path, targets):
    import finops.server as server
    from finops import cache
    from finops.connectors import business_metrics, llm_costs

    cache.clear()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    metrics = {"mrr_usd": 50000.0, "paying_customers": 100, "metric_date": "2026-09-01",
               "_source": "stored"}

    async def _resolve(*a, **k):
        return dict(metrics)

    monkeypatch.setattr(business_metrics, "resolve_business_metrics", _resolve)
    monkeypatch.setattr(business_metrics, "get_latest_metrics", lambda n=1: [dict(metrics)])
    monkeypatch.setattr(business_metrics, "get_metrics_history", lambda days=90: [dict(metrics)])
    monkeypatch.setattr(llm_costs, "get_all_llm_costs", lambda **k: {"total_usd": 0.0})

    async def _active(subset=None):
        return dict(targets)

    monkeypatch.setattr(server, "_active", _active)
    return server


def _exports(tmp_path):
    d = tmp_path / ".finops" / "exports"
    return list(d.iterdir()) if d.exists() else []


def test_board_summary_refuses_when_no_provider_is_connected(monkeypatch, tmp_path):
    server = _board_env(monkeypatch, tmp_path, {})
    out = asyncio.run(server.export_board_summary(period_days=30))

    assert "markdown" not in out, out.get("markdown")
    assert out["error"] == "no_cost_data"
    assert "This is not a finding of zero spend" in out["note"]
    assert _exports(tmp_path) == []


def test_board_summary_refuses_when_every_provider_failed(monkeypatch, tmp_path):
    server = _board_env(monkeypatch, tmp_path, {"aws": _FailingConnector()})
    out = asyncio.run(server.export_board_summary(period_days=30))

    assert "markdown" not in out, out.get("markdown")
    assert out["error"] == "no_cost_data"
    assert "credentials are invalid" in out["message"]
    assert _exports(tmp_path) == []


def test_board_summary_refuses_when_the_provider_returned_no_rows(monkeypatch, tmp_path):
    """The dogfood case: Cost Explorer answered with nothing and the board
    markdown said "Costs went down $0.00 (unknown %)"."""
    server = _board_env(monkeypatch, tmp_path, {"aws": _NoRowsConnector()})
    out = asyncio.run(server.export_board_summary(period_days=30))

    assert "markdown" not in out, out.get("markdown")
    assert out["error"] == "no_cost_data"
    assert "no cost rows" in out["message"]
    assert "This is not a finding of zero spend" in out["note"]
    assert _exports(tmp_path) == []


def test_exported_report_does_not_print_total_spend_zero_when_nothing_was_read(
        monkeypatch, tmp_path):
    import finops.server as server
    from finops.reporting import exporter

    monkeypatch.setattr(exporter, "_EXPORT_DIR", tmp_path)

    async def _no_data(**k):
        return {"error": "No cloud accounts connected yet. Call connect_aws."}

    monkeypatch.setattr(server, "get_cost_summary", _no_data)
    monkeypatch.setattr(server, "get_costs_by_service", _no_data)
    out = asyncio.run(server.export_cost_report(
        sections=["cost_summary", "services"], formats=["html"], open_file=False))

    html = open(out["files"]["html"]).read()
    assert "Total Spend" not in html, "an unread bill was printed as Total Spend $0"
    assert "No cost data was read" in html
    assert "not a finding of zero spend" in html
    assert out.get("cost_data_read") is False
    assert "No cost data was read" in out["message"]


def test_export_filename_keeps_the_whole_period(monkeypatch, tmp_path):
    from finops.reporting import exporter

    monkeypatch.setattr(exporter, "_EXPORT_DIR", tmp_path)
    out = exporter.write_report(
        title="Cloud Cost Report, 2026-08-25 to 2026-09-25",
        period_start="2026-08-25", period_end="2026-09-25",
        sections={"budgets": {"budgets": []}}, formats=["html"])
    name = out["html"].rsplit("/", 1)[-1]
    assert name.startswith("cloud_cost_report_2026-08-25_to_2026-09-25_"), name


def test_dashboard_with_no_cost_rows_is_unavailable_not_zero(tmp_path):
    from finops.reporting.dashboard import generate_account_dashboard

    out = asyncio.run(generate_account_dashboard(
        aws_connector=_NoRowsConnector(), account_id="123456789012",
        output_path=str(tmp_path / "d.html")))

    assert out["this_month_usd"] is None, out["summary"]
    assert "$0.00" not in out["summary"]
    assert "no cost rows" in out["summary"]
