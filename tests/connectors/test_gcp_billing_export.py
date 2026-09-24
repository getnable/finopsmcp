"""GCP spend comes from the BigQuery billing export, and nowhere else.

With Application Default Credentials but no GCP_BQ_BILLING_TABLE the connector
used to fall back to a Billing API helper that returned no rows, so gcp showed
$0.00 with no error. The empty summary was then cached for 12 hours under a key
that did not include the table, so setting the table kept serving the zero.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date

import pytest

from finops import cache
from finops.billing_access import BillingAccessError
from finops.connectors.gcp import GCPConnector

START, END = date(2026, 9, 1), date(2026, 9, 20)
TABLE = "proj.billing.gcp_billing_export_v1_0000"


@pytest.fixture(autouse=True)
def _cold_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cache, "_DISK_DISABLED", True)
    monkeypatch.delenv("GCP_BQ_BILLING_TABLE", raising=False)
    cache.clear()
    yield
    cache.clear()


def _connector() -> GCPConnector:
    c = GCPConnector()
    c._billing_account_ids = ["01ABCD-EF2345-678901"]
    return c


def test_no_export_table_raises_instead_of_zero():
    with pytest.raises(BillingAccessError) as ei:
        asyncio.run(_connector().get_costs(START, END))
    assert "GCP_BQ_BILLING_TABLE" in str(ei.value)


def test_no_export_table_surfaces_as_provider_error_not_zero():
    from finops import server

    _, by_provider, _ = asyncio.run(
        server._gather_costs({"gcp": _connector()}, START, END))
    assert "error" in by_provider["gcp"]
    assert "total_usd" not in by_provider["gcp"]


def test_setting_the_table_is_not_masked_by_a_cached_result(monkeypatch):
    calls = {"n": 0}

    def _fake_bq(self, billing_account_id, start_date, end_date):
        calls["n"] += 1
        amount = 100.0 if os.environ["GCP_BQ_BILLING_TABLE"] == TABLE else 7.0
        return [{"service": "Compute Engine", "region": "us-central1",
                 "total_cost": amount, "currency": "USD"}]

    monkeypatch.setattr(GCPConnector, "_query_bigquery", _fake_bq)

    monkeypatch.setenv("GCP_BQ_BILLING_TABLE", "proj.other.export")
    first = asyncio.run(_connector().get_costs(START, END))
    monkeypatch.setenv("GCP_BQ_BILLING_TABLE", TABLE)
    second = asyncio.run(_connector().get_costs(START, END))

    assert first.total_usd == 7.0
    assert second.total_usd == 100.0
    assert calls["n"] == 2
