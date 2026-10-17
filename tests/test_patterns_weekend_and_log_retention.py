"""Two waste patterns that priced spend they had not measured.

Weekend waste picked "weekend" days by list position (i % 7 in (5, 6), the
code assumed the series starts on a Monday) and priced 80% of all EC2 weekend
spend as dev/staging left running. The cost history it reads has no
environment, so production and untagged spend were called "dev": on a flat
$1,000/day EC2 account, all of it production, it said $6,400/mo of
"Dev/Staging Compute Running on Weekends".

Log retention took 40% of the CloudWatch bill ("Heuristic: 40% of CW cost is
often storage from unbounded log groups") as the saving. No byte was read.

Now weekend days come from date.weekday() and only spend whose environment is
known to be non-production counts. The log-retention saving is priced from
the stored bytes of never-expire groups older than their retention window (the
analyzers.waste rule); without the bytes it is an investigation row with no
dollar figure.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest

from finops.ml.patterns import (
    PatternContext,
    _check_cloudwatch_log_retention,
    _check_weekend_waste,
    scan_dict,
)

EC2 = "Amazon Elastic Compute Cloud - Compute"
CW = "Amazon CloudWatch"
WEDNESDAY = date(2026, 9, 2)


def _rows(env: str, weekday_usd: float, weekend_usd: float, days: int = 28,
          start: date = WEDNESDAY, service: str = EC2) -> list[dict]:
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        out.append({"date": d, "service": service, "environment": env,
                    "amount_usd": weekend_usd if d.weekday() >= 5 else weekday_usd})
    return out


def _ctx(**kw) -> PatternContext:
    kw.setdefault("daily_costs", {EC2: [1000.0] * 28})
    return PatternContext(by_resource=[], snapshots=[], **kw)


def test_prod_and_untagged_spend_is_never_called_dev():
    # The cost history alone: no environment is known, so nothing is counted.
    assert _check_weekend_waste(_ctx()) is None
    # Known prod and untagged (environment "") running all week: not dev either.
    rows = _rows("prod", 1000.0, 1000.0) + _rows("", 500.0, 500.0)
    assert _check_weekend_waste(_ctx(env_daily_costs=rows)) is None


def test_weekend_days_come_from_the_calendar_not_the_list_position():
    # Starts on a Wednesday. Dev runs $100 on weekdays and $10 on the real
    # Saturday and Sunday: weekends are already low, nothing to schedule.
    # Positions 5 and 6 of each week are Monday and Tuesday here, which the
    # old rule took for the weekend and read as 100% of weekday spend.
    rows = _rows("dev", 100.0, 10.0)
    assert _check_weekend_waste(_ctx(env_daily_costs=rows)) is None


def test_only_known_nonprod_spend_is_priced():
    rows = (_rows("Dev", 100.0, 100.0) + _rows("STAGING", 50.0, 50.0)
            + _rows("prod", 1000.0, 1000.0) + _rows("", 400.0, 400.0))
    m = _check_weekend_waste(_ctx(env_daily_costs=rows))
    # Non-prod weekend day: $150. 8 weekend days x $150 x 80% = $960, not the
    # $1,550/day of everything.
    assert m.monthly_waste == pytest.approx(960.0)
    said = " ".join(m.evidence)
    assert "Dev" in said and "STAGING" in said, said
    assert "prod" not in said.replace("non-prod", ""), said


def _group(name: str, gb: float, age_days: int, retention: int | None = None) -> dict:
    created = datetime.now(UTC) - timedelta(days=age_days)
    g = {"logGroupName": name, "storedBytes": int(gb * 1024 ** 3),
         "creationTime": int(created.timestamp() * 1000)}
    if retention is not None:
        g["retentionInDays"] = retention
    return g


def test_log_retention_without_stored_bytes_has_no_dollar_figure():
    ctx = _ctx(daily_costs={CW: [20.0] * 30})   # $600/mo of CloudWatch
    m = _check_cloudwatch_log_retention(ctx)
    assert m is not None and m.investigation
    assert m.monthly_waste == 0.0
    d = m.to_dict()
    assert d["monthly_waste"] is None and d["annual_waste"] is None, d
    assert "240" not in " ".join(m.evidence)
    out = scan_dict(ctx, min_monthly_waste=20.0)
    [row] = [f for f in out["findings"] if f["pattern_id"] == "log-retention"]
    assert row["investigation"] is True
    assert out["total_monthly_waste"] == 0.0


def test_log_retention_prices_measured_bytes_past_the_window():
    groups = [
        _group("/aws/ecs/api", 100, 400),            # never-expire, older than 30 d
        _group("/aws/lambda/fn", 200, 10),           # younger than 14 d: saves nothing yet
        _group("/aws/rds/db", 500, 400, retention=30),   # has a policy
    ]
    m = _check_cloudwatch_log_retention(_ctx(daily_costs={CW: [20.0] * 30}, log_groups=groups))
    assert not m.investigation
    # 100 GB x $0.03, an upper bound: the API does not say how much is older.
    assert m.monthly_waste == pytest.approx(3.0)
    assert "up to" in " ".join(m.evidence)
    assert m.resources == ["/aws/ecs/api"]


def test_scan_waste_patterns_says_what_it_did_not_check(monkeypatch, tmp_path):
    from finops import server  # noqa: F401  (loads the tools modules in order)
    from finops.storage import db as db_mod
    from finops.storage.snapshots import store_snapshots
    from finops.tools import aws_waste

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(db_mod, "_ENGINE", None)
    today = datetime.now(UTC).date()
    store_snapshots([
        {"provider": "aws", "service": svc, "account_id": "111122223333", "region": "",
         "snapshot_date": today - timedelta(days=d), "amount_usd": usd}
        for d in range(1, 31) for svc, usd in ((EC2, 1000.0), (CW, 20.0))])
    out = asyncio.run(getattr(aws_waste.scan_waste_patterns, "fn",
                              aws_waste.scan_waste_patterns)(account_id="111122223333"))
    monkeypatch.setattr(db_mod, "_ENGINE", None)
    ids = {f["pattern_id"] for f in out["findings"]}
    assert "weekend-waste" not in ids, out
    [lr] = [f for f in out["findings"] if f["pattern_id"] == "log-retention"]
    assert lr["monthly_waste"] is None and lr["investigation"] is True
    assert "weekend" in out["note"].lower() and "environment" in out["note"].lower(), out["note"]
