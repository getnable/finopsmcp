"""A multi-account rollup keeps the billing currency its accounts reported.

AWS and GCP built each account's summary with the right currency and then
merged into a fresh CostSummary that defaulted to USD, so two JPY accounts came
out as a USD total. Azure never read the Currency column at all.
"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from finops import cache
from finops.connectors.aws import AWSConnector
from finops.connectors.azure import AzureConnector
from finops.connectors.gcp import GCPConnector

START, END = date(2026, 9, 1), date(2026, 9, 20)


@pytest.fixture(autouse=True)
def _cold_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cache, "_DISK_DISABLED", True)
    cache.clear()
    yield
    cache.clear()


class _FakeCE:
    """ce:GetCostAndUsage response shape, one page."""

    def __init__(self, unit: str, amount: str):
        self._unit, self._amount = unit, amount

    def get_cost_and_usage(self, **kwargs):
        return {
            "ResultsByTime": [{
                "TimePeriod": {"Start": START.isoformat(), "End": END.isoformat()},
                "Total": {},
                "Groups": [{
                    "Keys": ["Amazon Elastic Compute Cloud - Compute"],
                    "Metrics": {"UnblendedCost": {"Amount": self._amount, "Unit": self._unit}},
                }],
                "Estimated": False,
            }],
            "DimensionValueAttributes": [],
        }


def _aws(monkeypatch, units: dict[str, str]) -> AWSConnector:
    arns = [f"arn:aws:iam::{acct}:role/nable" for acct in units]
    monkeypatch.setenv("AWS_ROLE_ARNS", ",".join(arns))
    c = AWSConnector()

    def _client_and_account(role_arn):
        acct = role_arn.split(":")[4]
        return _FakeCE(units[acct], "1000"), acct

    monkeypatch.setattr(c, "_client_and_account", _client_and_account)
    return c


def test_aws_rollup_of_jpy_accounts_is_labelled_jpy(monkeypatch):
    c = _aws(monkeypatch, {"111111111111": "JPY", "222222222222": "JPY"})
    s = asyncio.run(c.get_costs(START, END))
    assert s.total_usd == 2000.0
    assert s.currency == "JPY"
    assert {e.currency for e in s.entries} == {"JPY"}


def test_aws_rollup_across_currencies_is_mixed(monkeypatch):
    c = _aws(monkeypatch, {"111111111111": "JPY", "222222222222": "EUR"})
    assert asyncio.run(c.get_costs(START, END)).currency == "MIXED"


def test_gcp_rollup_keeps_account_currency(monkeypatch):
    monkeypatch.setenv("GCP_BQ_BILLING_TABLE", "proj.billing.export")
    c = GCPConnector()
    c._billing_account_ids = ["01AAAA-000000-000000", "01BBBB-000000-000000"]

    def _fake_bq(billing_account_id, start_date, end_date):
        return [{"service": "Compute Engine", "region": "asia-northeast1",
                 "total_cost": 5000.0, "currency": "JPY"}]

    monkeypatch.setattr(c, "_query_bigquery", _fake_bq)
    s = asyncio.run(c.get_costs(START, END))
    assert s.currency == "JPY"
    assert {e.currency for e in s.entries} == {"JPY"}


def _azure_result(currency: str):
    """QueryResult shape: columns with names, rows in column order."""
    cols = ["Cost", "BillingMonth", "ServiceName", "ResourceLocation", "Currency"]
    return SimpleNamespace(
        columns=[SimpleNamespace(name=n) for n in cols],
        rows=[[120.5, "2026-09-01T00:00:00", "Virtual Machines", "westeurope", currency]],
        next_link=None,
    )


def test_azure_reads_the_currency_column(monkeypatch):
    c = AzureConnector()
    c._subscription_ids = ["sub-a", "sub-b"]
    monkeypatch.setattr(c, "_query_costs", lambda *a, **k: _azure_result("EUR"))
    s = asyncio.run(c.get_costs(START, END))
    assert s.total_usd == pytest.approx(241.0)
    assert s.currency == "EUR"


def test_currency_reaches_the_tool_output(monkeypatch):
    from finops import server

    c = AzureConnector()
    c._subscription_ids = ["sub-a"]
    monkeypatch.setattr(c, "_query_costs", lambda *a, **k: _azure_result("EUR"))
    _, by_provider, _ = asyncio.run(server._gather_costs({"azure": c}, START, END))
    assert by_provider["azure"]["currency"] == "EUR"
    assert "currency_warning" in by_provider["azure"]
