"""Databricks costs parse the real usage CSV, stay in range, and fail loudly.

- The billable usage download is the usage log CSV (workspaceId, timestamp,
  clusterId, ..., clusterCustomTags, sku, dbus, ...). Its tag columns are quoted
  JSON containing commas, and the connector split lines on ",", looked for
  system-table column names that the CSV does not have, and so priced every
  row at 0 DBU. It also kept whole months when a few days were asked for.
- jobs/runs/list was asked for 100 runs per page (the API allows 25), and any
  non-200 from runs/list or clusters/list ended the read with nothing, which
  get_costs reported as $0.
- The cluster/run fallback is a node-type guess at a flat DBU rate and was
  indistinguishable from billed usage.
"""
from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest

from finops.connectors.databricks import DatabricksConnector

START, END = date(2026, 9, 10), date(2026, 9, 20)
HOST = "https://adb-1234567890123456.7.azuredatabricks.net"

USAGE_CSV = (
    "workspaceId,timestamp,clusterId,clusterName,clusterNodeType,clusterOwnerUserId,"
    "clusterCustomTags,sku,dbus,machineHours,clusterOwnerUserName,tags\n"
    '1234567890123456,2026-09-09T09:59:59.999Z,0909-a,etl,Standard_DS3_v2,111,'
    '"{""team"":""data"",""env"":""prod""}",STANDARD_JOBS_COMPUTE,50.0,4.0,a@x.com,'
    '"{""Creator"":""a@x.com""}"\n'
    '1234567890123456,2026-09-12T09:59:59.999Z,0912-b,etl,Standard_DS3_v2,111,'
    '"{""team"":""data"",""env"":""prod""}",STANDARD_JOBS_COMPUTE,10.0,4.0,a@x.com,'
    '"{""Creator"":""a@x.com""}"\n'
    '1234567890123456,2026-09-20T23:59:59.999Z,0920-c,bi,Standard_DS4_v2,222,'
    '"{""team"":""bi""}",STANDARD_ALL_PURPOSE_COMPUTE,5.0,2.0,b@x.com,"{}"\n'
    '1234567890123456,2026-09-21T00:59:59.999Z,0921-d,bi,Standard_DS4_v2,222,'
    '"{""team"":""bi""}",STANDARD_ALL_PURPOSE_COMPUTE,70.0,2.0,b@x.com,"{}"\n'
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", HOST)
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-test")
    monkeypatch.setenv("DATABRICKS_DBU_PRICE", "0.5")
    monkeypatch.delenv("DATABRICKS_ACCOUNT_ID", raising=False)


def _mock(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def _h(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(_h), **kw))
    return seen


def test_billable_usage_csv_is_parsed_and_bounded_to_the_requested_days(monkeypatch):
    monkeypatch.setenv("DATABRICKS_ACCOUNT_ID", "acct-1")
    _mock(monkeypatch, lambda req: httpx.Response(200, text=USAGE_CSV))

    s = asyncio.run(DatabricksConnector().get_costs(START, END))

    # Only the Sep 12 and Sep 20 rows are in range: (10 + 5) DBU at $0.50.
    assert s.total_usd == pytest.approx(7.5)
    assert s.by_service == {"STANDARD_JOBS_COMPUTE": pytest.approx(5.0),
                            "STANDARD_ALL_PURPOSE_COMPUTE": pytest.approx(2.5)}
    assert s.by_account == {"1234567890123456": pytest.approx(7.5)}


def _workspace_api(runs_status: int = 200):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/2.0/clusters/list"):
            return httpx.Response(200, json={"clusters": []})
        if req.url.path.endswith("/2.1/jobs/runs/list"):
            if runs_status != 200:
                return httpx.Response(runs_status, json={"error_code": "PERMISSION_DENIED"})
            if int(req.url.params.get("limit", 20)) > 25:
                return httpx.Response(400, json={"error_code": "INVALID_PARAMETER_VALUE"})
            return httpx.Response(200, json={"runs": [{
                "job_id": 7, "run_id": 70, "run_name": "nightly",
                "execution_duration": 3_600_000,
                "cluster_spec": {"new_cluster": {"node_type_id": "Standard_DS3_v2",
                                                 "num_workers": 1}},
            }], "has_more": False})
        return httpx.Response(404)
    return handler


def test_runs_are_listed_within_the_page_limit_and_flagged_estimated(monkeypatch):
    seen = _mock(monkeypatch, _workspace_api())
    s = asyncio.run(DatabricksConnector().get_costs(START, END))
    runs_calls = [r for r in seen if r.url.path.endswith("/runs/list")]
    assert all(int(r.url.params["limit"]) <= 25 for r in runs_calls)
    assert s.total_usd > 0
    assert list(s.by_service) == ["Jobs (estimated)"]
    assert all(e.metadata.get("estimated") for e in s.entries)


def test_a_failed_runs_list_raises_instead_of_zero(monkeypatch):
    _mock(monkeypatch, _workspace_api(runs_status=403))
    with pytest.raises(RuntimeError, match="runs/list failed: HTTP 403"):
        asyncio.run(DatabricksConnector().get_costs(START, END))


def test_unconfigured_is_an_error_not_a_zero_summary(monkeypatch):
    monkeypatch.delenv("DATABRICKS_TOKEN")
    with pytest.raises(RuntimeError, match="not configured"):
        asyncio.run(DatabricksConnector().get_costs(START, END))
