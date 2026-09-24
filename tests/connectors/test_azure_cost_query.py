"""Azure cost reads work for `az login` users, read every page, and fail loudly.

- AzureConnector built ClientSecretCredential from os.environ unconditionally,
  so a DefaultAzureCredential user (is_configured True) hit KeyError
  'AZURE_TENANT_ID' on every cost query.
- The connector's query had no explicit aggregation, read columns with a
  positional fallback, and never followed nextLink.
- azure_detail followed nextLink with GET, which the POST-only Query API
  rejects, and any HTTP error ended the loop with the rows read so far; a
  failure on the first page came back as [] and was reported as $0.
- The tag breakdown read TagKey (the key's own name) instead of TagValue.

Fakes sit at the HTTP and azure.identity boundary; response bodies follow the
documented Query API shape (properties.columns / rows / nextLink).
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from datetime import date

import httpx
import pytest

from finops import cache
from finops.connectors import azure_detail as det
from finops.connectors.azure import AzureConnector

START, END = date(2026, 9, 1), date(2026, 9, 20)
SUB = "00000000-0000-0000-0000-000000000001"
COST_COLS = [
    {"name": "Cost", "type": "Number"},
    {"name": "BillingMonth", "type": "Datetime"},
    {"name": "ServiceName", "type": "String"},
    {"name": "ResourceLocation", "type": "String"},
    {"name": "Currency", "type": "String"},
]


class _Resp:
    def __init__(self, status: int, body: dict | None = None, headers: dict | None = None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        self.text = str(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


def _page(columns, rows, next_link=None) -> dict:
    return {
        "id": f"subscriptions/{SUB}/providers/Microsoft.CostManagement/query/q1",
        "name": "q1",
        "type": "Microsoft.CostManagement/query",
        "properties": {"nextLink": next_link, "columns": columns, "rows": rows},
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cache, "_DISK_DISABLED", True)
    for v in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    cache.clear()
    yield
    cache.clear()


def _fake_identity(monkeypatch) -> dict:
    """azure.identity with only the default chain usable, as after `az login`."""
    used: dict = {}

    class _Token:
        token = "arm-token"

    class _Default:
        def __init__(self, **kw):
            used["default"] = kw

        def get_token(self, *scopes, **kw):
            used["scopes"] = scopes
            return _Token()

    class _Secret:
        def __init__(self, **kw):
            raise AssertionError("service principal credential built without SP env")

    mod = types.ModuleType("azure.identity")
    mod.DefaultAzureCredential = _Default
    mod.ClientSecretCredential = _Secret
    pkg = types.ModuleType("azure")
    pkg.identity = mod
    monkeypatch.setitem(sys.modules, "azure", pkg)
    monkeypatch.setitem(sys.modules, "azure.identity", mod)
    return used


def _fake_http(monkeypatch, responses: list[_Resp]) -> list[tuple]:
    calls: list[tuple] = []

    def _post(url, json=None, headers=None, timeout=None):
        calls.append(("POST", url, json))
        return responses.pop(0)

    def _get(url, headers=None, timeout=None):
        calls.append(("GET", url, None))
        return _Resp(405, {"error": {"code": "MethodNotAllowed"}})

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(httpx, "get", _get)
    return calls


def _connector() -> AzureConnector:
    c = AzureConnector()
    c._subscription_ids = [SUB]
    return c


def test_az_login_user_gets_costs_via_the_default_chain(monkeypatch):
    used = _fake_identity(monkeypatch)
    calls = _fake_http(monkeypatch, [_Resp(200, _page(COST_COLS, [
        [120.5, "2026-09-01T00:00:00", "Virtual Machines", "westeurope", "EUR"],
    ]))])

    s = asyncio.run(_connector().get_costs(START, END))

    assert "default" in used
    assert used["scopes"] == ("https://management.azure.com/.default",)
    assert s.total_usd == pytest.approx(120.5)
    assert s.currency == "EUR"
    body = calls[0][2]
    assert body["dataset"]["aggregation"] == {"totalCost": {"name": "Cost", "function": "Sum"}}


def test_connector_follows_next_link_with_post(monkeypatch):
    _fake_identity(monkeypatch)
    nxt = (f"https://management.azure.com/subscriptions/{SUB}/providers/"
           f"Microsoft.CostManagement/query?api-version=2023-11-01&$skiptoken=AQAAAA%3D%3D")
    calls = _fake_http(monkeypatch, [
        _Resp(200, _page(COST_COLS, [[100.0, "2026-09-01T00:00:00", "Storage", "eastus", "USD"]], nxt)),
        _Resp(200, _page(COST_COLS, [[50.0, "2026-09-01T00:00:00", "Bandwidth", "eastus", "USD"]])),
    ])

    s = asyncio.run(_connector().get_costs(START, END))

    assert s.total_usd == pytest.approx(150.0)
    assert [c[0] for c in calls] == ["POST", "POST"]
    assert calls[1][1] == nxt
    assert calls[1][2] == calls[0][2]  # same body on every page


def test_a_missing_cost_column_is_an_error_not_column_zero(monkeypatch):
    _fake_identity(monkeypatch)
    cols = [{"name": "BillingMonth", "type": "Datetime"},
            {"name": "PreTaxCost", "type": "Number"},
            {"name": "ServiceName", "type": "String"},
            {"name": "ResourceLocation", "type": "String"}]
    _fake_http(monkeypatch, [_Resp(200, _page(cols, [[20260901, 9.0, "Storage", "eastus"]]))])
    with pytest.raises(RuntimeError, match="Cost column"):
        asyncio.run(_connector().get_costs(START, END))


def test_detail_query_retries_429_then_succeeds(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    _fake_http(monkeypatch, [
        _Resp(429, {"error": {"code": "429"}},
              {"x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "7"}),
        _Resp(200, _page([{"name": "Cost", "type": "Number"}], [[3.0]])),
    ])
    rows = det._query_cost_management("tok", SUB, {"type": "ActualCost"})
    assert rows == [{"Cost": 3.0}]
    assert slept == [7.0]


def test_detail_query_gives_up_on_endless_throttling(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    _fake_http(monkeypatch, [_Resp(429, {}, {"retry-after": "600"}) for _ in range(10)])
    with pytest.raises(RuntimeError, match="429|throttled"):
        det._query_cost_management("tok", SUB, {"type": "ActualCost"})
    assert len(slept) == det._MAX_THROTTLE_RETRIES
    assert max(slept) <= det._MAX_THROTTLE_WAIT_S


def test_detail_query_raises_on_http_error(monkeypatch):
    _fake_http(monkeypatch, [_Resp(403, {"error": {"code": "RBACAccessDenied"}})])
    with pytest.raises(RuntimeError, match="RBACAccessDenied"):
        det._query_cost_management("tok", SUB, {"type": "ActualCost"})


def _detail_env(monkeypatch, subs: str):
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", subs)
    monkeypatch.setenv("AZURE_CLIENT_ID", "c")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "s")
    monkeypatch.setenv("AZURE_TENANT_ID", "t")
    monkeypatch.setattr(det, "_get_access_token", lambda: "tok")


def test_resource_costs_that_could_not_be_read_are_an_error_not_zero(monkeypatch):
    _detail_env(monkeypatch, SUB)
    _fake_http(monkeypatch, [_Resp(403, {"error": {"code": "RBACAccessDenied"}})])
    out = det.get_resource_costs(START, END)
    assert out.get("error")
    assert "total_cost" not in out


def test_resource_costs_missing_a_subscription_are_partial(monkeypatch):
    _detail_env(monkeypatch, f"{SUB},sub-2")
    cols = [{"name": "Cost", "type": "Number"}, {"name": "ResourceId", "type": "String"}]
    _fake_http(monkeypatch, [
        _Resp(200, _page(cols, [[40.0, "/subscriptions/x/vm1"]])),
        _Resp(403, {"error": {"code": "RBACAccessDenied"}}),
    ])
    out = det.get_resource_costs(START, END)
    assert out["total_cost"] == 40.0
    assert out["partial"] is True
    assert list(out["failed_subscriptions"]) == ["sub-2"]


def test_tag_breakdown_groups_by_tag_value(monkeypatch):
    _detail_env(monkeypatch, SUB)
    cols = [{"name": "Cost", "type": "Number"}, {"name": "TagKey", "type": "String"},
            {"name": "TagValue", "type": "String"}, {"name": "Currency", "type": "String"}]
    _fake_http(monkeypatch, [_Resp(200, _page(cols, [
        [100.0, "team", "payments", "USD"],
        [60.0, "team", "search", "USD"],
        [25.0, "", "", "USD"],
    ]))])
    out = det.get_tag_cost_breakdown("team", START, END)
    assert out["by_tag"] == {"payments": 100.0, "search": 60.0, "__untagged__": 25.0}
