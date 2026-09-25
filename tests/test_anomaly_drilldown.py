"""The resource drill-down behind a flagged service (anomaly/drilldown.py).

Every Cost Explorer and Athena answer goes through botocore's Stubber on a
real client (the `billed_stub` fixture lets a stubbed billed client past the
suite's no-spend block), so request parameters and response shapes are
checked against the service models.

Invariants under test:
  - one daily GetCostAndUsage over both windows, grouped by usage type and
    region, names the usage type behind the delta, its dollars, and the day
    it started
  - resource ids come from the CUR when one is configured, else from Cost
    Explorer's resource-level data, and the answer says when that is not
    enabled or does not reach back far enough
  - every billed request is counted, and nothing raises: what could not be
    read is listed instead
"""
from __future__ import annotations

from datetime import date, timedelta

import boto3
import pytest
from botocore.stub import ANY, Stubber

from finops.anomaly import drilldown as dd

TODAY = date(2026, 9, 25)
EC2 = "Amazon Elastic Compute Cloud - Compute"
P4D = "BoxUsage:p4d.24xlarge"
T3 = "BoxUsage:t3.micro"


def _ce():
    return boto3.client("ce", region_name="us-east-1", aws_access_key_id="testing",
                        aws_secret_access_key="testing")  # pragma: allowlist secret


def _day(d: date, groups: list[tuple[list[str], float, float]]) -> dict:
    return {"TimePeriod": {"Start": d.isoformat(), "End": (d + timedelta(days=1)).isoformat()},
            "Estimated": False, "Total": {},
            "Groups": [{"Keys": keys,
                        "Metrics": {"UnblendedCost": {"Amount": str(usd), "Unit": "USD"},
                                    "UsageQuantity": {"Amount": str(qty), "Unit": "Hrs"}}}
                       for keys, usd, qty in groups]}


def _p4d_series(current, baseline, *, from_day: date, per_day: float = 786.43, n: int = 8):
    """t3.micro flat all along; 8 p4d.24xlarge from `from_day` on."""
    days = baseline.dates() + current.dates()
    out = []
    for d in days:
        g = [([T3, "us-east-1"], 2.5, 240.0)]
        if d >= from_day:
            g.append(([P4D, "us-east-1"], per_day, 24.0 * n))
        out.append(_day(d, g))
    return out


def _usage_params(service, current, baseline, account=None):
    flt = {"Dimensions": {"Key": "SERVICE", "Values": [service]}}
    if account:
        flt = {"And": [flt, {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account]}}]}
    return {"TimePeriod": {"Start": baseline.start.isoformat(), "End": current.end.isoformat()},
            "Granularity": "DAILY", "Filter": flt,
            "Metrics": ["UnblendedCost", "UsageQuantity"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"},
                        {"Type": "DIMENSION", "Key": "REGION"}]}


def _resource_params(service, start, end, usage_types):
    return {"TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": "DAILY",
            "Filter": {"And": [{"Dimensions": {"Key": "SERVICE", "Values": [service]}},
                               {"Dimensions": {"Key": "USAGE_TYPE", "Values": usage_types}}]},
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "RESOURCE_ID"},
                        {"Type": "DIMENSION", "Key": "USAGE_TYPE"}]}


@pytest.fixture
def no_cur(monkeypatch):
    for v in ("CUR_S3_BUCKET", "CUR_ATHENA_DATABASE", "CUR_ATHENA_TABLE",
              "CUR_ATHENA_RESULTS_BUCKET"):
        monkeypatch.delenv(v, raising=False)


# ── windows and parsing ───────────────────────────────────────────────────────

def test_windows():
    cur, base = dd.windows_for_day(date(2026, 9, 21))
    assert (cur.start, cur.end, cur.days) == (date(2026, 9, 21), date(2026, 9, 22), 1)
    assert (base.start, base.end, base.days) == (date(2026, 9, 14), date(2026, 9, 21), 7)
    cur, base = dd.windows_for_period(7, TODAY)
    assert (cur.start, cur.end) == (date(2026, 9, 18), TODAY)
    assert (base.start, base.end) == (date(2026, 9, 11), date(2026, 9, 18))


@pytest.mark.parametrize("usage_type, expected", [
    ("BoxUsage:p4d.24xlarge", "p4d.24xlarge"),
    ("USE2-BoxUsage:m5.large", "m5.large"),
    ("EU-BoxUsage:m5.large", "m5.large"),
    ("USE1-SpotUsage:g5.xlarge", "g5.xlarge"),
    ("InstanceUsage:db.r5.large", "db.r5.large"),
    ("USW2-Multi-AZUsage:db.r6g.xlarge", "db.r6g.xlarge"),
    ("NodeUsage:cache.r6g.large", "cache.r6g.large"),
    ("USE1-Host:ml.g5.xlarge", "ml.g5.xlarge"),
    ("EBS:VolumeUsage.gp3", None),
    ("NatGateway-Hours", None),
    ("USE1-DataTransfer-Out-Bytes", None),
])
def test_instance_type_is_read_from_the_usage_type(usage_type, expected):
    assert dd.instance_type(usage_type) == expected


def test_onset_is_the_first_day_of_the_rise():
    cur, base = dd.windows_for_period(7, TODAY)
    start = date(2026, 9, 21)
    series = {d: (800.0 if d >= start else 0.0) for d in base.dates() + cur.dates()}
    assert dd.onset(series, base, cur) == start
    flat = {d: 10.0 for d in base.dates() + cur.dates()}
    assert dd.onset(flat, base, cur) is None


# ── usage types ───────────────────────────────────────────────────────────────

def test_the_usage_type_behind_the_delta_with_its_onset(billed_stub, no_cur):
    """Resource-level data answers too: two p4d instances, one new."""
    cur, base = dd.windows_for_period(7, TODAY)
    ce = _ce()
    resources = []
    for d in base.dates() + cur.dates():
        g = [(["i-0old", P4D], 10.0, 0.0)]
        if d >= date(2026, 9, 21):
            g.append((["i-0p4d1", P4D], 786.43, 0.0))
        resources.append(_day(d, g))
    with billed_stub(ce) as stub:
        stub.add_response("get_cost_and_usage",
                          {"ResultsByTime": _p4d_series(cur, base, from_day=date(2026, 9, 21))},
                          _usage_params(EC2, cur, base))
        stub.add_response("get_cost_and_usage_with_resources", {"ResultsByTime": resources},
                          _resource_params(EC2, base.start, cur.end, [P4D]))
        r = dd.drill_down(EC2, cur, base, meter=dd.Meter(ce), today=TODAY)
        stub.assert_no_pending_responses()
    [row] = r["rows"]
    assert row["usage_type"] == P4D and row["region"] == "us-east-1"
    assert row["onset"] == "2026-09-21" and row["instance_type"] == "p4d.24xlarge"
    # 4 days at $786.43 against a baseline of zero, scaled to 7 days.
    assert row["delta_usd"] == pytest.approx(4 * 786.43, abs=0.01)
    assert row["delta_per_day_usd"] == pytest.approx(4 * 786.43 / 7, abs=0.01)
    assert row["monthly_run_rate_usd"] == pytest.approx(4 * 786.43 / 7 * 30, abs=0.1)
    assert row["usage_delta_per_day"] == pytest.approx(4 * 192 / 7, abs=0.01)
    assert [x["resource_id"] for x in row["resources"]] == ["i-0p4d1", "i-0old"]
    assert row["resources"][0]["onset"] == "2026-09-21"
    assert row["resources"][1]["delta_usd"] == 0
    assert r["resource_source"] == "ce_resources"
    assert r["cost_explorer_requests"] == 2
    assert any("No CUR source" in n for n in r["not_read"])


def test_a_flat_usage_type_is_not_a_driver(billed_stub, no_cur):
    cur, base = dd.windows_for_day(date(2026, 9, 21))
    ce = _ce()
    flat = [_day(d, [([T3, "us-east-1"], 2.5, 24.0)]) for d in base.dates() + cur.dates()]
    with billed_stub(ce) as stub:
        stub.add_response("get_cost_and_usage", {"ResultsByTime": flat},
                          _usage_params(EC2, cur, base, account="123456789012"))
        r = dd.drill_down(EC2, cur, base, meter=dd.Meter(ce), today=TODAY,
                          account_id="123456789012")
    assert r["rows"] == [] and r["cost_explorer_requests"] == 1


def test_resource_level_data_not_enabled_is_said_and_the_usage_types_kept(billed_stub, no_cur):
    cur, base = dd.windows_for_period(7, TODAY)
    ce = _ce()
    with billed_stub(ce) as stub:
        stub.add_response("get_cost_and_usage",
                          {"ResultsByTime": _p4d_series(cur, base, from_day=date(2026, 9, 21))},
                          _usage_params(EC2, cur, base))
        stub.add_client_error("get_cost_and_usage_with_resources",
                              service_error_code="DataUnavailableException",
                              service_message="Resource-level data is not available",
                              http_status_code=400)
        r = dd.drill_down(EC2, cur, base, meter=dd.Meter(ce), today=TODAY)
    assert [row["usage_type"] for row in r["rows"]] == [P4D]
    assert r["rows"][0]["resources"] == [] and r["resource_source"] is None
    assert any("not enabled" in n and "14 days" in n for n in r["not_read"])
    assert r["cost_explorer_requests"] == 2


def test_resource_level_data_older_than_14_days_is_not_asked_for(billed_stub, no_cur):
    cur, base = dd.windows_for_day(date(2026, 8, 20))
    ce = _ce()
    with billed_stub(ce) as stub:
        stub.add_response("get_cost_and_usage",
                          {"ResultsByTime": _p4d_series(cur, base, from_day=date(2026, 8, 20))},
                          _usage_params(EC2, cur, base))
        r = dd.drill_down(EC2, cur, base, meter=dd.Meter(ce), today=TODAY)
        stub.assert_no_pending_responses()
    assert r["cost_explorer_requests"] == 1
    assert any("last 14 days" in n and "older" in n for n in r["not_read"])


def test_resource_data_that_reaches_part_way_back_says_so(billed_stub, no_cur):
    cur, base = dd.windows_for_period(10, TODAY)       # baseline starts 20 days back
    ce = _ce()
    earliest = TODAY - timedelta(days=14)
    with billed_stub(ce) as stub:
        stub.add_response("get_cost_and_usage",
                          {"ResultsByTime": _p4d_series(cur, base, from_day=date(2026, 9, 21))},
                          _usage_params(EC2, cur, base))
        stub.add_response("get_cost_and_usage_with_resources", {"ResultsByTime": []},
                          _resource_params(EC2, earliest, cur.end, [P4D]))
        r = dd.drill_down(EC2, cur, base, meter=dd.Meter(ce), today=TODAY)
    assert r["resource_source"] == "ce_resources"
    assert any(earliest.isoformat() in n for n in r["notes"])


def test_cost_explorer_denied_is_reported_not_raised(billed_stub, no_cur):
    cur, base = dd.windows_for_day(date(2026, 9, 21))
    ce = _ce()
    with billed_stub(ce) as stub:
        stub.add_client_error("get_cost_and_usage", service_error_code="AccessDeniedException",
                              service_message="no", http_status_code=400)
        r = dd.drill_down(EC2, cur, base, meter=dd.Meter(ce), today=TODAY)
    assert r["rows"] == [] and "ce:GetCostAndUsage" in r["error"]
    assert r["not_read"] == [r["error"]] and r["cost_explorer_requests"] == 1


def test_pages_are_followed_and_capped(billed_stub, no_cur, monkeypatch):
    monkeypatch.setattr(dd, "MAX_PAGES", 2)
    cur, base = dd.windows_for_day(date(2026, 9, 21))
    ce = _ce()
    series = _p4d_series(cur, base, from_day=date(2026, 9, 21))
    with billed_stub(ce) as stub:
        stub.add_response("get_cost_and_usage",
                          {"ResultsByTime": series[:4], "NextPageToken": "p2"},
                          _usage_params(EC2, cur, base))
        stub.add_response("get_cost_and_usage",
                          {"ResultsByTime": series[4:], "NextPageToken": "p3"},
                          {**_usage_params(EC2, cur, base), "NextPageToken": "p2"})
        stub.add_client_error("get_cost_and_usage_with_resources",
                              service_error_code="DataUnavailableException",
                              service_message="x", http_status_code=400)
        r = dd.drill_down(EC2, cur, base, meter=dd.Meter(ce), today=TODAY)
    assert [row["usage_type"] for row in r["rows"]] == [P4D]
    assert r["cost_explorer_requests"] == 3
    assert any("more than 2 pages" in n for n in r["not_read"])


def test_the_meter_says_what_it_cost():
    m = dd.Meter(object())
    m.requests = 3
    assert "3 Cost Explorer requests, about $0.03" in m.note()


# ── the CUR ───────────────────────────────────────────────────────────────────

def test_resource_ids_come_from_the_cur_when_one_is_configured(billed_stub, monkeypatch):
    from finops.connectors import cur as cur_mod
    for k, v in {"CUR_S3_BUCKET": "b", "CUR_ATHENA_DATABASE": "cur_db",
                 "CUR_ATHENA_TABLE": "cur_report", "CUR_ATHENA_RESULTS_BUCKET": "r"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cur_mod.time, "sleep", lambda s: None)
    cur, base = dd.windows_for_period(7, TODAY)
    ce = _ce()
    athena = boto3.client("athena", region_name="us-east-1", aws_access_key_id="testing",
                          aws_secret_access_key="testing")  # pragma: allowlist secret
    monkeypatch.setattr(boto3, "client", lambda *a, **k: athena)

    def cell(v):
        return {"VarCharValue": v}

    header = {"Data": [cell(c) for c in ("line_item_resource_id", "line_item_usage_type",
                                          "product_region", "usage_day", "unblended_cost")]}
    rows = [{"Data": [cell("i-0p4d1"), cell(P4D), cell("us-east-1"), cell(d.isoformat()),
                      cell("786.43")]}
            for d in cur.dates() if d >= date(2026, 9, 21)]
    with billed_stub(ce) as s1, billed_stub(athena) as s2:
        s1.add_response("get_cost_and_usage",
                        {"ResultsByTime": _p4d_series(cur, base, from_day=date(2026, 9, 21))},
                        _usage_params(EC2, cur, base))
        s2.add_response("start_query_execution", {"QueryExecutionId": "q1"}, {
            "QueryString": ANY, "QueryExecutionContext": {"Database": "cur_db"},
            "ResultConfiguration": ANY, "WorkGroup": "primary",
            "ResultReuseConfiguration": ANY})
        s2.add_response("get_query_execution",
                        {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}},
                        {"QueryExecutionId": "q1"})
        s2.add_response("get_query_results", {"ResultSet": {"Rows": [header] + rows}},
                        {"QueryExecutionId": "q1"})
        r = dd.drill_down(EC2, cur, base, meter=dd.Meter(ce), today=TODAY)
        s1.assert_no_pending_responses()
        s2.assert_no_pending_responses()
    assert r["resource_source"] == "cur" and r["cost_explorer_requests"] == 1
    [res] = r["rows"][0]["resources"]
    assert res["resource_id"] == "i-0p4d1" and res["onset"] == "2026-09-21"
    assert res["delta_usd"] == pytest.approx(4 * 786.43, abs=0.01)
    assert not any("CUR" in n for n in r["not_read"])
    assert any("Athena bills" in n for n in r["notes"])


def test_the_cur_query_filters_on_the_usage_types(monkeypatch):
    from finops.connectors import cur as cur_mod
    for k in ("CUR_S3_BUCKET", "CUR_ATHENA_DATABASE", "CUR_ATHENA_TABLE",
              "CUR_ATHENA_RESULTS_BUCKET"):
        monkeypatch.setenv(k, "x")
    seen = []
    monkeypatch.setattr(cur_mod, "_athena_query", lambda sql, **k: seen.append(sql) or [])
    got = cur_mod.get_resource_daily_costs(date(2026, 9, 11), date(2026, 9, 24),
                                           [P4D, "it's"], account_id="123456789012")
    assert got == {"rows": [], "truncated": False, "source": "cur_athena"}
    [sql] = seen
    assert "line_item_resource_id" in sql
    assert "line_item_usage_type IN ('BoxUsage:p4d.24xlarge', 'it''s')" in sql
    assert "line_item_usage_account_id = '123456789012'" in sql
    assert "line_item_usage_start_date < DATE '2026-09-25'" in sql


def test_a_cur_failure_is_reported_not_raised(billed_stub, monkeypatch):
    from finops.connectors import cur as cur_mod
    monkeypatch.setattr(cur_mod, "is_configured", lambda: True)
    monkeypatch.setattr(cur_mod, "get_resource_daily_costs",
                        lambda *a, **k: {"error": "table not found"})
    cur, base = dd.windows_for_period(7, TODAY)
    ce = _ce()
    with billed_stub(ce) as stub:
        stub.add_response("get_cost_and_usage",
                          {"ResultsByTime": _p4d_series(cur, base, from_day=date(2026, 9, 21))},
                          _usage_params(EC2, cur, base))
        r = dd.drill_down(EC2, cur, base, meter=dd.Meter(ce), today=TODAY)
    assert r["rows"] and r["resource_source"] is None
    assert any("CUR (Athena) read failed: table not found" in n for n in r["not_read"])


def test_the_suite_still_blocks_an_unstubbed_billed_call():
    with pytest.raises(AssertionError, match="spends real money"):
        _ce().get_cost_and_usage(TimePeriod={"Start": "2026-09-01", "End": "2026-09-02"},
                                 Granularity="DAILY", Metrics=["UnblendedCost"])


def test_a_stubbed_client_outside_billed_stub_is_still_blocked():
    ce = _ce()
    with Stubber(ce) as stub:
        stub.add_response("get_cost_and_usage", {"ResultsByTime": []})
        with pytest.raises(AssertionError, match="spends real money"):
            ce.get_cost_and_usage(TimePeriod={"Start": "2026-09-01", "End": "2026-09-02"},
                                  Granularity="DAILY", Metrics=["UnblendedCost"])
