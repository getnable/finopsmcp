"""The account dashboard's month arithmetic.

  - Last month was requested with End = its last day. Cost Explorer's End is
    exclusive, so the last day of every previous month was dropped.
  - Month to date was compared with the whole previous month under "vs last
    month", so every dashboard before the last day of the month showed a large
    drop that was only the calendar.
  - remaining_days was 30 - today.day, wrong for every month that is not 30
    days long.
  - An AWS failure was swallowed and rendered "Spend this month: $0.00".
"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from finops.reporting import dashboard


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    from finops.storage import db

    saved_engine, saved_dir = db._ENGINE, db._DATA_DIR
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "finops.db"))
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    db._ENGINE, db._DATA_DIR = None, None
    yield
    db._ENGINE, db._DATA_DIR = saved_engine, saved_dir


class _AWS:
    """Answers get_costs at $100/day, End exclusive, like Cost Explorer."""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[date, date]] = []
        self._fail = fail

    async def get_costs(self, start, end, *a, **k):
        self.calls.append((start, end))
        if self._fail:
            raise RuntimeError("ExpiredTokenException: the security token has expired")
        total = 100.0 * (end - start).days
        return SimpleNamespace(total_usd=total, by_service={"Amazon EC2": total},
                               entries=[])

    async def list_accounts(self):
        return [{"id": "111122223333", "name": "111122223333"}]


def _run(monkeypatch, tmp_path, today: date, aws, forecast_daily: float | None = None):
    monkeypatch.setattr(dashboard, "_today", lambda: today)
    from finops.ml import forecasting
    if forecast_daily is None:
        monkeypatch.setattr(forecasting.Forecaster, "for_account",
                            staticmethod(lambda *a, **k: forecasting.Forecaster("x")))
    else:
        monkeypatch.setattr(
            forecasting.Forecaster, "for_account",
            staticmethod(lambda *a, **k: forecasting.Forecaster("x").fit([forecast_daily] * 5)))
    return asyncio.run(dashboard.generate_account_dashboard(
        aws_connector=aws, account_id="111122223333",
        output_path=str(tmp_path / "dash.html")))


def test_last_month_includes_its_last_day(monkeypatch, tmp_path):
    aws = _AWS()
    out = _run(monkeypatch, tmp_path, date(2026, 3, 15), aws)
    assert (date(2026, 2, 1), date(2026, 3, 1)) in aws.calls, aws.calls
    assert out["last_month_usd"] == pytest.approx(2800.0)   # all 28 days of February


def test_month_to_date_is_compared_with_the_same_days_last_month(monkeypatch, tmp_path):
    aws = _AWS()
    out = _run(monkeypatch, tmp_path, date(2026, 3, 15), aws)
    # 14 complete days of March against the first 14 of February: flat spend,
    # so no change. Against all of February it read as -$1,400.
    assert out["this_month_usd"] == pytest.approx(1400.0)
    assert out["last_month_same_period_usd"] == pytest.approx(1400.0)
    assert "+$0.00" in out["summary"], out["summary"]
    assert "-$1,400" not in out["summary"]


def test_comparison_is_capped_at_the_length_of_last_month(monkeypatch, tmp_path):
    aws = _AWS()
    out = _run(monkeypatch, tmp_path, date(2026, 3, 31), aws)
    assert out["comparison_days"] == 28
    assert out["last_month_same_period_usd"] == pytest.approx(2800.0)


def test_projection_uses_the_real_length_of_the_month(monkeypatch, tmp_path):
    # 10 Feb 2026: 9 complete days (900) plus 19 remaining days of a 28-day
    # month forecast at $100/day. 30 - today.day forecast 20 days.
    out = _run(monkeypatch, tmp_path, date(2026, 2, 10), _AWS(), forecast_daily=100.0)
    assert out["projected_usd"] == pytest.approx(900.0 + 19 * 100.0)


def test_projection_for_a_31_day_month(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, date(2026, 1, 31), _AWS(), forecast_daily=100.0)
    assert out["projected_usd"] == pytest.approx(3000.0 + 1 * 100.0)


def test_an_aws_failure_is_not_rendered_as_zero_spend(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, date(2026, 3, 15), _AWS(fail=True))
    assert out["this_month_usd"] is None
    assert out["aws_error"] and "ExpiredToken" in out["aws_error"]
    assert "$0.00" not in out["summary"], out["summary"]
    assert "unavailable" in out["summary"]
    html = (tmp_path / "dash.html").read_text()
    assert "unavailable" in html.lower()
