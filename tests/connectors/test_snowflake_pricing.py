"""Snowflake credits without a contract price are not $0, and storage is not dropped.

Without SNOWFLAKE_CREDIT_PRICE the connector contributed 0 USD per warehouse,
so 15,000 credits rendered as $0.00; the "credits only" note it built was then
discarded. With a price set, the storage query ran and its result was thrown
away by an `if ...: pass`.
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from finops.connectors.saas.snowflake import SnowflakeConnector

START, END = date(2026, 9, 1), date(2026, 9, 10)


class _Cursor:
    """snowflake.connector cursor: execute(sql, params) then fetchall()."""

    def __init__(self, wh_rows, storage_rows):
        self._wh, self._st = wh_rows, storage_rows
        self._last: list = []
        self.sql: list[str] = []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        self._last = self._st if "STORAGE_USAGE" in sql else self._wh

    def fetchall(self):
        return list(self._last)

    def fetchone(self):
        return self._last[0] if self._last else None


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def _connector(monkeypatch, *, credit=None, storage=None, wh=None, st=None):
    for k, v in (("SNOWFLAKE_CREDIT_PRICE", credit), ("SNOWFLAKE_STORAGE_PRICE_PER_TB", storage)):
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    c = SnowflakeConnector()
    cursor = _Cursor(wh or [("ETL_WH", 12000.0), ("BI_WH", 3000.0)], st or [])
    monkeypatch.setattr(c, "_connect", lambda: _Conn(cursor))
    return c, cursor


def test_unpriced_credits_raise_instead_of_zero(monkeypatch):
    c, _ = _connector(monkeypatch)
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(c.get_costs(START, END))
    msg = str(ei.value)
    assert "SNOWFLAKE_CREDIT_PRICE" in msg
    assert "15,000.00 credits" in msg


def test_unpriced_credits_surface_as_provider_error(monkeypatch):
    from finops import cache, server

    monkeypatch.setattr(cache, "_DISK_DISABLED", True)
    cache.clear()
    c, _ = _connector(monkeypatch)
    _, by_provider, _ = asyncio.run(server._gather_costs({"snowflake": c}, START, END))
    assert "error" in by_provider["snowflake"]
    assert "total_usd" not in by_provider["snowflake"]


def test_priced_credits_are_converted(monkeypatch):
    c, cursor = _connector(monkeypatch, credit="3")
    s = asyncio.run(c.get_costs(START, END))
    assert s.total_usd == pytest.approx(45000.0)
    assert s.by_service["Warehouse: ETL_WH"] == pytest.approx(36000.0)
    # No storage rate configured: the storage view is not queried at all.
    assert not any("STORAGE_USAGE" in q for q in cursor.sql)


def test_storage_is_included_when_a_storage_rate_is_set(monkeypatch):
    # 10 days at 3 TB in a 30-day month at $23/TB-month = 10 * 3 * 23 / 30 = 23.
    storage_rows = [(date(2026, 9, d), 3.0) for d in range(1, 11)]
    c, _ = _connector(monkeypatch, credit="3", storage="23", st=storage_rows)
    s = asyncio.run(c.get_costs(START, END))
    assert s.by_service["Storage"] == pytest.approx(23.0)
    assert s.total_usd == pytest.approx(45000.0 + 23.0)
