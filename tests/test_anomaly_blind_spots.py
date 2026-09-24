"""Anomaly detector blind spots: shapes of spend change the detector never flagged.

Each test is a series a customer would call an obvious anomaly. The detector
used to return None for all of them, so get_anomalies reported an all-clear on
the exact day the bill went sideways.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

import finops.anomaly.seasonality as seasonality
import finops.storage.db as db_mod
from finops.anomaly.detector import detect_for_series
from finops.anomaly.seasonality import detect_with_seasonality

TODAY = date(2026, 9, 23)


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


def _rows(amounts_by_days_ago: dict[int, float]) -> list[dict]:
    return [
        {"snapshot_date": (TODAY - timedelta(days=n)).isoformat(), "amount_usd": amt}
        for n, amt in sorted(amounts_by_days_ago.items(), reverse=True)
    ]


# ── a perfectly flat baseline has stdev 0 ────────────────────────────────────


def test_flat_baseline_then_huge_spike_is_flagged_by_rolling_mean():
    """28 days at exactly $100, then $10,000. stdev is 0, which used to force
    z=0 and fail the z gate no matter how large the jump."""
    out = detect_for_series("aws", "EC2", "123", TODAY, 10_000.0, [100.0] * 28)
    assert out is not None
    assert out.direction == "spike"
    assert out.severity == "high"


def test_flat_baseline_then_huge_spike_is_flagged_by_same_weekday(monkeypatch):
    history = _rows({n: 100.0 for n in range(1, 57)})
    monkeypatch.setattr(seasonality, "get_history", lambda *a, **kw: history)
    out = detect_with_seasonality("aws", "EC2", "123", TODAY, 10_000.0)
    assert out is not None
    assert out.direction == "spike"
    assert out.metadata["detection_method"].startswith("seasonality-aware")


def test_flat_baseline_small_wobble_is_still_quiet():
    """The floor must not turn a flat series into a hair trigger."""
    assert detect_for_series("aws", "EC2", "123", TODAY, 110.0, [100.0] * 28) is None


# ── spend falling to (nearly) nothing ────────────────────────────────────────


def test_drop_from_4000_a_day_to_zero_is_flagged(monkeypatch):
    """The small-spend noise floor belongs on the baseline side. Applied to
    today's amount it hid the drop that matters most: a pipeline or a backup
    job that stopped running."""
    history = _rows({n: 4_000.0 for n in range(1, 57)})
    monkeypatch.setattr(seasonality, "get_history", lambda *a, **kw: history)
    out = detect_with_seasonality("aws", "EC2", "123", TODAY, 0.0)
    assert out is not None
    assert out.direction == "drop"
    assert out.severity == "high"


def test_tiny_baseline_is_still_ignored(monkeypatch):
    """A service that never cost more than a few dollars stays below the noise floor."""
    history = _rows({n: 2.0 for n in range(1, 57)})
    monkeypatch.setattr(seasonality, "get_history", lambda *a, **kw: history)
    assert detect_with_seasonality("aws", "S3", "123", TODAY, 0.0) is None


# ── one series per region ────────────────────────────────────────────────────


def _seed_two_regions(days: int = 28) -> None:
    """Azure-shaped history: the same service billing in two regions, one big
    and one small, both perfectly steady."""
    from finops.storage.snapshots import store_snapshot

    for n in range(1, days + 1):
        d = date.today() - timedelta(days=n)
        store_snapshot("azure", "Virtual Machines", "sub-1", "eastus", d, 1_000.0)
        store_snapshot("azure", "Virtual Machines", "sub-1", "westus", d, 10.0)


def test_history_is_filtered_by_region_when_one_is_given(fresh_db):
    from finops.storage.snapshots import get_history

    _seed_two_regions()
    east = get_history("azure", "Virtual Machines", "sub-1", days=28, region="eastus")
    assert east and {r["region"] for r in east} == {"eastus"}
    # No region keeps the old, all-regions answer for callers that want it.
    both = get_history("azure", "Virtual Machines", "sub-1", days=28)
    assert {r["region"] for r in both} == {"eastus", "westus"}


def test_a_spike_in_one_region_is_not_hidden_by_the_other(fresh_db, monkeypatch):
    """Blending regions put the baseline at ~$505 with a stdev of ~$530, so a
    real 50% jump in the $1,000 region scored z of about 1.9 and was never
    flagged. Against its own region's history it is plainly a spike."""
    from finops.scheduler import jobs
    from finops.storage.snapshots import store_snapshot

    _seed_two_regions()
    persisted: list = []
    monkeypatch.setattr("finops.anomaly.detector.persist_anomaly",
                        lambda a: persisted.append(a) or (len(persisted), False))
    yesterday = date.today() - timedelta(days=1)
    store_snapshot("azure", "Virtual Machines", "sub-1", "eastus", yesterday, 1_500.0)

    asyncio.run(jobs._detect_and_alert())

    assert [(a.direction, a.baseline_mean) for a in persisted] == [("spike", 1_000.0)]


# ── a service that stops billing leaves no row to evaluate ───────────────────


class _FakeConnector:
    """Stands in for every connector _snapshot_all builds. Only the provider named
    in `entries` is configured; the rest report not configured."""

    entries: dict[str, list] = {}
    _name = ""

    def __init__(self, *a, **kw):
        pass

    async def is_configured(self):
        return self._name in self.entries

    async def get_costs(self, start, end, granularity="DAILY"):
        from finops.connectors.base import CostSummary

        rows = self.entries[self._name]
        return CostSummary(provider=self._name, start_date=start, end_date=end,
                           total_usd=sum(e.amount for e in rows), by_service={},
                           by_account={}, by_region={}, entries=list(rows))


def _patch_connectors(monkeypatch, entries: dict[str, list]) -> None:
    import finops.connectors.aws as aws
    import finops.connectors.azure as azure
    import finops.connectors.gcp as gcp
    import finops.connectors.saas.datadog as datadog
    import finops.connectors.saas.mongodb_atlas as atlas
    import finops.connectors.saas.twilio as twilio

    _FakeConnector.entries = entries
    for mod, cls, name in ((aws, "AWSConnector", "aws"), (azure, "AzureConnector", "azure"),
                           (gcp, "GCPConnector", "gcp"),
                           (datadog, "DatadogConnector", "datadog"),
                           (atlas, "MongoDBAtlasConnector", "mongodb_atlas"),
                           (twilio, "TwilioConnector", "twilio")):
        monkeypatch.setattr(mod, cls, type(cls, (_FakeConnector,), {"_name": name}))


def _entry(service: str, amount: float):
    from finops.connectors.base import CostEntry

    return CostEntry(provider="azure", account_id="sub-1", account_name="sub-1",
                     service=service, region="eastus", amount=amount)


def _yesterday_rows(service: str) -> list[float]:
    from finops.storage.snapshots import get_history

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    return [r["amount_usd"] for r in get_history("azure", service, "sub-1", days=2)
            if r["snapshot_date"] == yesterday]


def test_a_service_that_stops_billing_is_flagged_as_a_drop(fresh_db, monkeypatch):
    """Snapshots stored only amount > 0, so a service going from $4,000 a day to
    nothing simply had no row for yesterday, and the detector, which walks
    yesterday's rows, never looked at it."""
    from finops.scheduler import jobs
    from finops.storage.snapshots import store_snapshot

    for n in range(2, 30):
        d = date.today() - timedelta(days=n)
        store_snapshot("azure", "SQL Database", "sub-1", "eastus", d, 4_000.0)
        store_snapshot("azure", "Storage", "sub-1", "eastus", d, 50.0)
    # Last billed three weeks ago: outside the recent window, so no zero row.
    store_snapshot("azure", "Retired Service", "sub-1", "eastus",
                   date.today() - timedelta(days=21), 900.0)
    _patch_connectors(monkeypatch, {"azure": [_entry("Storage", 50.0)]})

    asyncio.run(jobs._snapshot_all())

    assert _yesterday_rows("SQL Database") == [0.0]
    assert _yesterday_rows("Retired Service") == []

    persisted: list = []
    monkeypatch.setattr("finops.anomaly.detector.persist_anomaly",
                        lambda a: persisted.append(a) or (len(persisted), False))
    asyncio.run(jobs._detect_and_alert())
    assert [(a.service, a.direction) for a in persisted] == [("SQL Database", "drop")]


def test_an_empty_fetch_writes_no_zero_rows(fresh_db, monkeypatch):
    """A provider that returned nothing at all for the day is far more likely to
    be late than to have stopped every service at once. Zero-filling it would
    page a drop for every service on the account."""
    from finops.scheduler import jobs
    from finops.storage.snapshots import store_snapshot

    for n in range(2, 30):
        store_snapshot("azure", "SQL Database", "sub-1", "eastus",
                       date.today() - timedelta(days=n), 4_000.0)
    _patch_connectors(monkeypatch, {"azure": []})

    asyncio.run(jobs._snapshot_all())

    assert _yesterday_rows("SQL Database") == []
