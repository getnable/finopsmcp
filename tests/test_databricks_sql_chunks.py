"""system.billing.usage over the Statement Execution API: every chunk of an
INLINE result is read, and a truncated result is refused rather than summed
as if it were the whole bill."""
from __future__ import annotations

import asyncio

import pytest

from finops.connectors import databricks as dbx


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.text = body, status, ""

    def json(self):
        return self._body


class _Client:
    def __init__(self, first, chunks):
        self.first, self.chunks, self.gets = first, chunks, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        return _Resp(self.first)

    async def get(self, url, headers=None):
        self.gets.append(url)
        return _Resp(self.chunks[url.rsplit("/", 1)[-1]])


def _conn(monkeypatch, client):
    monkeypatch.setenv("DATABRICKS_HOST", "https://ws.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "t")
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "w1")
    monkeypatch.setattr(dbx.httpx, "AsyncClient", lambda timeout=None: client)
    return dbx.DatabricksConnector()


def test_every_chunk_is_read(monkeypatch):
    first = {"status": {"state": "SUCCEEDED"},
             "manifest": {"schema": {"columns": [{"name": "sku"}, {"name": "dbus"}]}},
             "result": {"data_array": [["A", 1]], "next_chunk_internal_link": "/api/2.0/sql/statements/s1/result/chunks/1"}}
    chunks = {"1": {"data_array": [["B", 2]], "next_chunk_internal_link": "/api/2.0/sql/statements/s1/result/chunks/2"},
              "2": {"data_array": [["C", 3]]}}
    client = _Client(first, chunks)
    cols, rows = asyncio.run(_conn(monkeypatch, client)._sql("SELECT 1"))
    assert cols == ["sku", "dbus"] and rows == [["A", 1], ["B", 2], ["C", 3]]
    assert [u.rsplit("/", 1)[-1] for u in client.gets] == ["1", "2"]


def test_a_truncated_result_is_refused(monkeypatch):
    first = {"status": {"state": "SUCCEEDED"}, "manifest": {"truncated": True, "schema": {"columns": []}},
             "result": {"data_array": [["A", 1]]}}
    with pytest.raises(RuntimeError, match="truncated"):
        asyncio.run(_conn(monkeypatch, _Client(first, {}))._sql("SELECT 1"))
