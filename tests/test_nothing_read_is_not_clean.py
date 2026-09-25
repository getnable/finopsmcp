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


# ── 3. Cost queries: "no rows returned" is not "read, and zero" ───────────────

def _cost_env(monkeypatch, targets):
    import finops.server as server
    from finops import cache

    cache.clear()
    monkeypatch.delenv("FINOPS_DEMO", raising=False)

    async def _active(subset=None):
        return dict(targets)

    async def _no_credit(*a, **k):
        return None

    monkeypatch.setattr(server, "_active", _active)
    monkeypatch.setattr(server, "_credit_context", _no_credit)
    return server


class _RowsOnlyAfter(_NoRowsConnector):
    """No rows before `cutoff`, $120 of EC2 from it on."""

    def __init__(self, cutoff):
        self.cutoff = cutoff

    async def get_costs(self, start, end, granularity="MONTHLY", **kw):
        if start < self.cutoff:
            return _empty_summary(start, end)
        from finops.connectors.base import CostEntry
        e = CostEntry(provider="aws", account_id="1", account_name="1",
                      service="Amazon EC2", region="us-east-1", amount=120.0)
        return CostSummary(provider="aws", start_date=start, end_date=end, total_usd=120.0,
                           by_service={"Amazon EC2": 120.0}, by_account={"1": 120.0},
                           by_region={"us-east-1": 120.0}, entries=[e])


class _ZeroSpendConnector(_NoRowsConnector):
    """Read, and a real $0 bill: AWS returned periods with nothing billed."""

    async def get_costs(self, start, end, granularity="MONTHLY", **kw):
        s = _empty_summary(start, end)
        s._zero_spend_account = True
        return s


def test_cost_summary_with_no_rows_says_so(monkeypatch):
    server = _cost_env(monkeypatch, {"aws": _NoRowsConnector()})
    out = asyncio.run(server.get_cost_summary())

    assert out.get("no_cost_rows") is True, out
    assert "no cost rows" in out["no_rows_note"]
    assert "not a finding of zero spend" in out["no_rows_note"]
    assert out["grand_total_formatted"] != "$0.00"


def test_cost_summary_read_and_zero_is_not_no_rows(monkeypatch):
    server = _cost_env(monkeypatch, {"aws": _ZeroSpendConnector()})
    out = asyncio.run(server.get_cost_summary())

    assert "no_cost_rows" not in out
    assert out["grand_total_formatted"] == "$0.00"
    assert "$0.00 in spend" in out["by_provider"]["aws"]["note"]


def test_cost_trends_with_no_rows_says_so(monkeypatch):
    server = _cost_env(monkeypatch, {"aws": _NoRowsConnector()})
    out = asyncio.run(server.get_cost_trends(days=14))

    assert out.get("no_cost_rows") is True, out
    assert "no cost rows" in out["no_rows_note"]


def test_recent_cost_drivers_with_no_rows_is_not_a_zero_dollar_change(monkeypatch):
    server = _cost_env(monkeypatch, {"aws": _NoRowsConnector()})
    out = asyncio.run(server.explain_recent_cost_drivers(days=7))

    assert "Costs increased by $0" not in str(out)
    assert out["error"] == "no_cost_data"
    assert "no cost rows" in out["message"]


def test_recent_cost_drivers_with_no_prior_rows_is_not_a_change(monkeypatch):
    from datetime import date, timedelta

    server = _cost_env(monkeypatch, {
        "aws": _RowsOnlyAfter(date.today() - timedelta(days=7))})
    out = asyncio.run(server.explain_recent_cost_drivers(days=7))

    assert "N/A%" not in out["summary"], out["summary"]
    assert "no cost rows" in out["summary"]
    assert out.get("comparison_unavailable") is True


# ── 4. Day one: no history is not "wait a week" and not "connect AWS" ────────

def test_anomalies_on_day_one_point_at_a_tool_that_reads_cost_explorer(monkeypatch):
    import finops.server as server
    from finops.anomaly import detector

    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    monkeypatch.setattr(detector, "get_active_anomalies", lambda **k: [])
    monkeypatch.setattr(detector, "has_enough_history", lambda *a: False)
    monkeypatch.setattr(detector, "latest_snapshot_date", lambda *a: None)
    out = server.get_anomalies()
    if asyncio.iscoroutine(out):   # sync tools are offloaded behind a wrapper
        out = asyncio.run(out)

    assert out["anomalies"] == []
    assert "explain_recent_cost_drivers" in out["message"]
    assert out.get("next_tool") == "explain_recent_cost_drivers"


def test_forecast_on_a_connected_account_with_no_history_does_not_say_connect(monkeypatch):
    import finops.server as server
    from finops.ml import forecasting

    class _Empty:
        _series: list = []

    monkeypatch.setattr(server, "require_pro", lambda *a, **k: None)

    async def _acct(account_id=None):
        return "123456789012"

    monkeypatch.setattr(server, "_resolve_account_id", _acct)
    monkeypatch.setitem(server.CLOUD_CONNECTORS, "aws", _NoRowsConnector())
    monkeypatch.setattr(forecasting.Forecaster, "for_account",
                        classmethod(lambda cls, *a, **k: _Empty()))
    out = asyncio.run(server.forecast_costs())

    text = str(out)
    assert "Connect your AWS account" not in text and "finops setup aws" not in text, text
    assert "no cost history yet" in out["error"].lower()
    assert "take_snapshot_now" in out["hint"] or "explain_recent_cost_drivers" in out["hint"]


# ── 6. SaaS guidance: a Snowflake question is not answered with connect_aws ───

def test_saas_summary_with_nothing_connected_points_at_saas_setup(monkeypatch):
    server = _cost_env(monkeypatch, {})
    out = asyncio.run(server.get_saas_spend_summary())

    text = str(out)
    assert "connect_aws" not in text, text
    assert "sample data" not in text
    assert "finops setup snowflake" in text
    assert "finops setup databricks" in text
    assert "not a finding of zero spend" in text


def test_saas_summary_with_real_saas_data_carries_no_sample_data_hint(monkeypatch):
    import finops.demo_data as demo_data

    class _SaaS(_NoRowsConnector):
        async def get_costs(self, start, end, granularity="MONTHLY", **kw):
            from finops.connectors.base import CostEntry
            e = CostEntry(provider="snowflake", account_id="a", account_name="a",
                          service="Snowflake Compute", region="", amount=40.0)
            return CostSummary(provider="snowflake", start_date=start, end_date=end,
                               total_usd=40.0, by_service={"Snowflake Compute": 40.0},
                               by_account={"a": 40.0}, by_region={}, entries=[e])

    server = _cost_env(monkeypatch, {"snowflake": _SaaS()})
    monkeypatch.setattr(demo_data, "_real_provider_connected", lambda: False)
    out = asyncio.run(server.get_saas_spend_summary())

    assert out["grand_total_usd"] == 40.0
    assert "_connect_hint" not in out, "real Snowflake spend was labelled sample data"


def test_snowflake_extra_hint_gives_the_uvx_form(monkeypatch):
    import builtins
    from finops.connectors.saas.snowflake import SnowflakeConnector

    real_import = builtins.__import__

    def _no_snowflake(name, *a, **k):
        if name.startswith("snowflake"):
            raise ImportError("no snowflake")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_snowflake)
    try:
        SnowflakeConnector()._connect()
    except RuntimeError as e:
        msg = str(e)
    assert "pip install 'finops-mcp[snowflake]'" in msg
    assert "uvx --from 'finops-mcp[snowflake]'" in msg


def test_list_connected_providers_does_not_call_env_presence_connected(monkeypatch):
    import finops.server as server

    class _Configured:
        async def is_configured(self):
            return True

    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    monkeypatch.setitem(server.SAAS_CONNECTORS, "snowflake", _Configured())
    out = server.list_connected_providers()
    if asyncio.iscoroutine(out):
        out = asyncio.run(out)

    assert out["snowflake"]["configured"] is True
    assert out["snowflake"]["status"] == "configured (not yet verified)"
    assert "check_connector_health" in out["_note"]


# ── 7. AI spend: LLM providers are first-class, not "connect_aws" ─────────────

def test_cost_summary_for_an_llm_provider_reads_the_llm_path(monkeypatch):
    server = _cost_env(monkeypatch, {})
    seen = {}

    async def _by_model(days=30, provider=None):
        seen.update(days=days, provider=provider)
        return {"provider": provider, "total_usd": 12.5, "by_model": {"claude-x": 12.5},
                "period": {"start": "2026-08-26", "end": "2026-09-25"}}

    monkeypatch.setattr(server, "get_llm_cost_by_model", _by_model)
    out = asyncio.run(server.get_cost_summary(provider="anthropic"))

    assert "connect_aws" not in str(out), out
    assert seen["provider"] == "anthropic"
    assert out["total_usd"] == 12.5
    assert out["source_tool"] == "get_llm_cost_by_model"


def test_connector_health_includes_the_llm_connectors(monkeypatch):
    import finops.server as server
    from finops.connectors.saas import anthropic_usage, openai_usage

    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    monkeypatch.setattr(server, "_ALL_CONNECTORS", {})

    async def _yes():
        return True

    async def _no():
        return False

    monkeypatch.setattr(openai_usage, "is_configured", _yes)
    monkeypatch.setattr(openai_usage, "get_costs",
                        lambda s, e: {"source": "costs_api", "total_usd": 1.0, "by_model": {}})
    monkeypatch.setattr(anthropic_usage, "is_configured", _no)
    out = asyncio.run(server.check_connector_health())

    by_name = {c["name"]: c for c in out["connectors"]}
    assert by_name["openai"]["healthy"] is True
    assert by_name["anthropic"]["configured"] is False
    assert "finops setup anthropic" in by_name["anthropic"]["fix"]


def test_connector_health_reports_an_unreadable_llm_key_as_broken(monkeypatch):
    import finops.server as server
    from finops.connectors.saas import openai_usage

    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    monkeypatch.setattr(server, "_ALL_CONNECTORS", {})

    async def _yes():
        return True

    monkeypatch.setattr(openai_usage, "is_configured", _yes)
    monkeypatch.setattr(openai_usage, "get_costs",
                        lambda s, e: {"source": "none", "reason": "api_error: 401",
                                      "total_usd": 0.0})
    out = asyncio.run(server.check_connector_health())

    broken = {b["name"]: b for b in out["broken"]}
    assert "openai" in broken, out
    assert "401" in broken["openai"]["error"]


def _welcome_text(monkeypatch, capsys):
    from finops import welcome

    monkeypatch.setattr(welcome, "_is_first_run", lambda: True)
    monkeypatch.setattr(welcome, "_is_interactive_install", lambda: True)
    monkeypatch.setattr(welcome, "_mark_welcomed", lambda: None)
    monkeypatch.setattr(welcome, "_fire_telemetry", lambda *a, **k: None)
    monkeypatch.setattr(welcome, "_agent_usage_teaser", lambda: None)
    welcome.show_welcome()
    return capsys.readouterr().out


def test_welcome_does_not_list_sources_as_connected(monkeypatch, capsys):
    out = _welcome_text(monkeypatch, capsys)
    assert "Connected sources" not in out
    assert "Supported sources" in out


def test_welcome_telemetry_copy_matches_opt_in(monkeypatch, capsys):
    out = _welcome_text(monkeypatch, capsys)
    assert "sends anonymous usage pings" not in out
    assert "Opt out: NABLE_NO_TELEMETRY" not in out
    assert "off unless" in out
    assert "NABLE_TELEMETRY=1" in out


def test_llm_connect_with_no_key_entered_does_not_say_done(monkeypatch, capsys, tmp_path):
    from finops import setup_wizard

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(setup_wizard, "_prompt", lambda *a, **k: "")
    monkeypatch.setattr(setup_wizard, "_configure_claude_desktop", lambda: False)
    monkeypatch.setattr("finops.welcome.show_welcome", lambda: None)
    try:
        setup_wizard.main(["setup", "openai"])
    except SystemExit:
        pass
    out = capsys.readouterr().out

    assert "Nothing stored" in out
    assert "Done." not in out, out
    assert "finops setup openai" in out or "setup openai" in out


def test_setup_saas_api_key_reports_whether_it_stored_anything(monkeypatch, tmp_path):
    from finops import setup_wizard

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(setup_wizard, "_prompt", lambda *a, **k: "")
    assert setup_wizard.setup_saas_api_key("OpenAI", [("OPENAI_API_KEY", "k", True)]) is False
