"""Snapshot writes go to the database in one transaction per batch, not one per row.

store_snapshot opens its own transaction for every row: a DELETE, an INSERT and
a commit. The daily snapshot and the Cost Explorer backfill both called it once
per row, so a 1,500 row backfill paid for 1,500 commits. store_snapshots writes
the same rows, with the same upsert result, in one.
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from datetime import date, timedelta

import pytest
from sqlalchemy import event, select

import finops.storage.db as db_mod


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A brand new SQLite database, with the engine singleton restored after."""
    prev_engine, prev_dir = db_mod._ENGINE, db_mod._DATA_DIR
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "finops.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    db_mod._ENGINE = None
    db_mod._DATA_DIR = None
    yield db_mod
    if db_mod._ENGINE is not None and db_mod._ENGINE is not prev_engine:
        try:
            db_mod._ENGINE.dispose()
        except Exception:
            pass
    db_mod._ENGINE = prev_engine
    db_mod._DATA_DIR = prev_dir


def _commits() -> list[int]:
    """Count every commit on the current engine from here on."""
    seen: list[int] = []
    event.listen(db_mod.get_engine(), "commit", lambda conn: seen.append(1))
    return seen


def _stored() -> list[tuple]:
    t = db_mod.cost_snapshots
    with db_mod.get_engine().connect() as conn:
        rows = conn.execute(select(
            t.c.provider, t.c.service, t.c.account_id, t.c.region,
            t.c.snapshot_date, t.c.amount_usd, t.c.granularity, t.c.category,
        )).fetchall()
    return sorted(tuple(r) for r in rows)


def _row(service: str, day: date, amount: float, **extra) -> dict:
    return {"provider": "aws", "service": service, "account_id": "111",
            "region": "us-east-1", "snapshot_date": day, "amount_usd": amount,
            **extra}


def test_a_bulk_write_stores_exactly_what_per_row_writes_store(tmp_path, monkeypatch, fresh_db):
    """Same upsert semantics: a stored key is replaced, and within one batch the
    later row for a key wins, just as it did when each row was its own call."""
    from finops.storage.snapshots import store_snapshot, store_snapshots

    d1, d2 = date(2026, 9, 1), date(2026, 9, 2)
    existing = [_row("EC2", d1, 5.0), _row("S3", d1, 1.0)]
    batch = [
        _row("EC2", d1, 10.0),                       # replaces a stored row
        _row("EC2", d2, 11.0, category="compute"),
        _row("RDS", d2, 7.0, granularity="MONTHLY"),
        _row("RDS", d2, 8.0),                        # same key again: this one wins
    ]

    for r in existing + batch:
        store_snapshot(**r)
    per_row = _stored()

    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "bulk.db"))
    db_mod._ENGINE.dispose()
    db_mod._ENGINE = None
    for r in existing:
        store_snapshot(**r)
    assert store_snapshots(batch) == 3

    assert _stored() == per_row
    assert ("aws", "RDS", "111", "us-east-1", "2026-09-02", 8.0, "DAILY", None) in per_row


def test_an_empty_batch_writes_nothing(fresh_db):
    from finops.storage.snapshots import store_snapshots

    commits = _commits()
    assert store_snapshots([]) == 0
    assert commits == []


def test_a_large_batch_is_one_transaction_and_fast(fresh_db):
    """1,500 rows took 1.6s through per-row store_snapshot and 0.06s in one
    transaction. The bound is generous; per-row writes miss it on any machine."""
    from finops.storage.snapshots import store_snapshots

    rows = [
        {"provider": "aws", "service": f"svc{i % 50}", "account_id": f"a{i // 50 % 3}",
         "region": "us-east-1", "snapshot_date": date(2026, 9, 1) - timedelta(days=i // 150),
         "amount_usd": float(i)}
        for i in range(1500)
    ]
    commits = _commits()
    started = time.perf_counter()
    assert store_snapshots(rows) == 1500
    elapsed = time.perf_counter() - started

    assert len(commits) == 1
    assert elapsed < 0.75, f"1,500 snapshot rows took {elapsed:.2f}s"
    assert len(_stored()) == 1500


# ── the two call sites ───────────────────────────────────────────────────────


class _Connector:
    entries: list = []

    def __init__(self, *a, **kw):
        pass

    async def is_configured(self):
        return bool(self.entries)

    async def get_costs(self, start, end, granularity="DAILY"):
        from finops.connectors.base import CostSummary

        return CostSummary(provider="azure", start_date=start, end_date=end,
                           total_usd=sum(e.amount for e in self.entries), by_service={},
                           by_account={}, by_region={}, entries=list(self.entries))


class _Unconfigured(_Connector):
    entries: list = []


def test_the_daily_snapshot_writes_a_fetch_in_one_transaction(fresh_db, monkeypatch):
    import finops.connectors.aws as aws
    import finops.connectors.azure as azure
    import finops.connectors.gcp as gcp
    import finops.connectors.saas.datadog as datadog
    import finops.connectors.saas.mongodb_atlas as atlas
    import finops.connectors.saas.twilio as twilio
    from finops.connectors.base import CostEntry
    from finops.scheduler import jobs
    from finops.storage.snapshots import store_snapshot

    yesterday = date.today() - timedelta(days=1)
    # A series that billed last week and is missing from this fetch still gets
    # its $0 row, in the same pass.
    store_snapshot("azure", "Stopped", "sub-1", "eastus", yesterday - timedelta(days=2), 40.0)
    entries = [CostEntry(provider="azure", account_id="sub-1", account_name="sub-1",
                         service=f"svc{i}", region="eastus", amount=float(i + 1))
               for i in range(300)]
    monkeypatch.setattr(azure, "AzureConnector",
                        type("AzureConnector", (_Connector,), {"entries": entries}))
    for mod, cls in ((aws, "AWSConnector"), (gcp, "GCPConnector"),
                     (datadog, "DatadogConnector"), (atlas, "MongoDBAtlasConnector"),
                     (twilio, "TwilioConnector")):
        monkeypatch.setattr(mod, cls, type(cls, (_Unconfigured,), {}))
    monkeypatch.setattr("finops.anomaly.backfill.backfill_from_cost_explorer",
                        lambda *a, **k: {"skipped": "test"})

    commits = _commits()
    out = asyncio.run(jobs._snapshot_all())

    assert out["azure"].startswith("ok")
    day_rows = [r for r in _stored() if r[4] == yesterday.isoformat()]
    assert len(day_rows) == 301
    assert ("azure", "Stopped", "sub-1", "eastus", yesterday.isoformat(), 0.0,
            "DAILY", None) in day_rows
    # One for the fetch, one for the zero rows. It was one per entry.
    assert len(commits) <= 2, f"{len(commits)} commits for one fetch"


def test_the_cost_explorer_backfill_writes_in_one_transaction(fresh_db, monkeypatch):
    import boto3

    import finops.billing_access as ba
    from finops.anomaly import backfill

    monkeypatch.setattr(backfill, "needs_backfill", lambda: True)
    absent = types.ModuleType("finops.connectors.cur_s3")
    absent.is_configured = lambda: False
    monkeypatch.setitem(sys.modules, "finops.connectors.cur_s3", absent)

    start = date.today() - timedelta(days=14)
    pages = [
        {"ResultsByTime": [
            {"TimePeriod": {"Start": (start + timedelta(days=d)).isoformat()},
             "Groups": [{"Keys": [f"svc{s}"],
                         "Metrics": {"UnblendedCost": {"Amount": str(s + 1)}}}
                        for s in range(40)]}
            for d in range(p * 7, p * 7 + 7)],
         **({"NextPageToken": "next"} if p == 0 else {})}
        for p in range(2)
    ]

    class _CE:
        def get_cost_and_usage(self, **kwargs):
            return pages[1 if kwargs.get("NextPageToken") else 0]

    class _STS:
        def get_caller_identity(self):
            return {"Account": "111"}

    monkeypatch.setattr(ba, "ce_client", lambda **k: _CE())
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: _STS())

    commits = _commits()
    out = backfill.backfill_from_cost_explorer(explicit=True)

    assert out == {"backfilled_days": 14, "rows": 560}
    assert len(_stored()) == 560
    assert len(commits) == 1, f"{len(commits)} commits for one backfill"
