"""get_anomalies must not say "No active anomalies." when it cannot know.

An all-clear is a claim. It is only true when there is recent history to judge
against, and only about the account that was asked for. Each test here is a
state where the tool used to make that claim anyway. Real SQLite, real tool,
no provider calls.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

import finops.accounts as accounts
import finops.storage.db as db_mod

_YAML = """\
default_account: prod
accounts:
  - name: prod
    account_id: "111111111111"
  - name: staging
    account_id: "222222222222"
  - name: sandbox
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    prev_engine, prev_dir = db_mod._ENGINE, db_mod._DATA_DIR
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "finops.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    monkeypatch.delenv("FINOPS_DEMO_FORCE", raising=False)
    db_mod._ENGINE = None
    db_mod._DATA_DIR = None
    path = tmp_path / "accounts.yaml"
    path.write_text(_YAML)
    monkeypatch.setattr(accounts, "_ACCOUNTS_FILE", path)

    import finops.demo_data as demo
    import finops.server as srv

    monkeypatch.setattr(demo, "is_demo", lambda: False)
    monkeypatch.setattr(srv, "_load_alert_policies", lambda: [])
    monkeypatch.setattr(srv, "_team_nudge", lambda *a, **kw: None)
    yield srv
    if db_mod._ENGINE is not None and db_mod._ENGINE is not prev_engine:
        try:
            db_mod._ENGINE.dispose()
        except Exception:
            pass
    db_mod._ENGINE = prev_engine
    db_mod._DATA_DIR = prev_dir


def _seed_snapshots(account_id: str, last_day: date, days: int = 14) -> None:
    from finops.storage.snapshots import store_snapshot

    for n in range(days):
        store_snapshot("aws", "EC2", account_id, "us-east-1",
                       last_day - timedelta(days=n), 100.0)


def _seed_anomaly(account_id: str, service: str, detected_at: datetime) -> None:
    with db_mod.get_engine().begin() as conn:
        conn.execute(db_mod.anomalies.insert().values(
            provider="aws", service=service, account_id=account_id,
            detected_at=detected_at, snapshot_date=detected_at.date().isoformat(),
            severity="high", direction="spike", pct_change=150.0, z_score=4.0,
            baseline_mean=100.0, current_amount=250.0,
            acknowledged=False, notified=False,
        ))


def test_stale_history_is_not_reported_as_all_clear(env):
    """Two weeks of snapshots that ended a month ago. Nothing has been checked
    since, so "No active anomalies." would be a guess dressed as a finding."""
    _seed_snapshots("111111111111", date.today() - timedelta(days=30))
    out = asyncio.run(env.get_anomalies())
    assert out["anomalies"] == []
    assert out["message"] != "No active anomalies."
    assert "stale" in out["message"].lower()


def test_fresh_history_is_a_real_all_clear(env):
    _seed_snapshots("111111111111", date.today() - timedelta(days=1))
    out = asyncio.run(env.get_anomalies())
    assert out["message"] == "No active anomalies."


def test_account_filter_is_applied_before_the_limit(env):
    """25 newer anomalies on prod, one older on staging. The filter ran in
    Python after LIMIT 20, so staging's anomaly was never fetched and the tool
    said staging was clear."""
    _seed_snapshots("222222222222", date.today() - timedelta(days=1))
    now = datetime.now(timezone.utc)
    for i in range(25):
        _seed_anomaly("111111111111", f"svc-{i}", now - timedelta(minutes=i))
    _seed_anomaly("222222222222", "RDS", now - timedelta(days=1))

    out = asyncio.run(env.get_anomalies(account="staging"))
    assert [a["service"] for a in out["anomalies"]] == ["RDS"]
    assert out["account"] == "staging"


def test_unknown_account_is_an_error_not_the_default_account(env):
    _seed_snapshots("111111111111", date.today() - timedelta(days=1))
    _seed_anomaly("111111111111", "EC2", datetime.now(timezone.utc))
    out = asyncio.run(env.get_anomalies(account="prodd"))
    assert "anomalies" not in out or out["anomalies"] == []
    assert out["valid_accounts"] == ["prod", "staging", "sandbox"]


def test_account_without_an_id_is_not_answered_with_every_account(env):
    """No account_id means there is nothing to filter on. Applying no filter
    returned every account's anomalies labelled as the one asked about."""
    _seed_anomaly("111111111111", "EC2", datetime.now(timezone.utc))
    out = asyncio.run(env.get_anomalies(account="sandbox"))
    assert "error" in out
    assert not out.get("anomalies")
