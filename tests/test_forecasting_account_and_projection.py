"""Cost forecasting: whose bill it reads, which thread it runs on, what a month is.

  - The Cost Explorer fallback built boto3.client("ce") from default
    credentials, so forecast_costs(account_id="B") on a machine whose default
    profile is account A forecast A's bill and labelled it B. It must read
    through the connector's session for the requested account, or return
    nothing.
  - It also called asyncio.get_event_loop() (unused), which raises in a worker
    thread, so the fallback returned [] exactly where it is meant to run.
  - monthly_projection was sum(point[:30]): a 7-day horizon reported a week of
    spend as the monthly projection.
  - forecast_costs ran the blocking fit (CE calls plus a parameter grid search)
    on the event loop.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from finops.ml.forecasting import Forecaster

_DAYS = [{"TimePeriod": {"Start": f"2026-09-{d:02d}", "End": f"2026-09-{d + 1:02d}"},
          "Total": {"UnblendedCost": {"Amount": "100.0", "Unit": "USD"}},
          "Groups": [], "Estimated": False} for d in range(1, 21)]


class _CE:
    def __init__(self, amount: str = "100.0"):
        self.calls: list[dict] = []
        self._amount = amount

    def get_cost_and_usage(self, **kw):
        self.calls.append(kw)
        return {"ResultsByTime": [
            {**d, "Total": {"UnblendedCost": {"Amount": self._amount, "Unit": "USD"}}}
            for d in _DAYS]}


class _Connector:
    """The two AWSConnector internals the fallback relies on."""

    def __init__(self, account: str, role_arns: list[str] | None = None):
        self._account = account
        self._role_arns = role_arns or []
        self.ce = _CE()
        self.made_for: list[str | None] = []

    def _account_id(self, role_arn=None):
        return role_arn.split(":")[4] if role_arn else self._account

    def _make_client(self, role_arn=None):
        self.made_for.append(role_arn)
        return self.ce


@pytest.fixture
def default_creds_ce(monkeypatch):
    """What boto3.client("ce") would return: the DEFAULT profile's bill."""
    import boto3
    other = _CE(amount="999.0")
    monkeypatch.setattr(boto3, "client", lambda *_a, **_k: other)
    return other


def test_fallback_reads_the_requested_account_through_the_connector(default_creds_ce):
    conn = _Connector(account="111122223333")
    series = Forecaster("111122223333")._load_series_from_ce(conn, 20)
    assert series == [100.0] * 20
    assert conn.ce.calls, "the connector's client was never used"
    assert not default_creds_ce.calls, "read the default credentials' bill"


def test_fallback_picks_the_role_for_the_requested_account(default_creds_ce):
    conn = _Connector(account="111122223333", role_arns=[
        "arn:aws:iam::444455556666:role/finops",
        "arn:aws:iam::777788889999:role/finops",
    ])
    series = Forecaster("777788889999")._load_series_from_ce(conn, 20)
    assert series == [100.0] * 20
    assert conn.made_for == ["arn:aws:iam::777788889999:role/finops"]


def test_fallback_returns_nothing_rather_than_another_accounts_bill(default_creds_ce):
    conn = _Connector(account="111122223333")
    series = Forecaster("999999999999")._load_series_from_ce(conn, 20)
    assert series == [], (
        "asked for account 999999999999 and got a series from credentials that "
        "belong to another account")
    assert not default_creds_ce.calls and not conn.ce.calls


def test_fallback_works_in_a_worker_thread(default_creds_ce):
    conn = _Connector(account="111122223333")
    out: dict = {}

    def _run():
        out["series"] = Forecaster("111122223333")._load_series_from_ce(conn, 20)

    t = threading.Thread(target=_run)
    t.start()
    t.join()
    assert out["series"] == [100.0] * 20


@pytest.mark.parametrize("horizon", [1, 7, 14, 30, 60])
def test_monthly_projection_is_a_month_whatever_the_horizon(horizon):
    f = Forecaster("111122223333").fit([100.0] * 5)   # naive: flat $100/day
    result = f.predict(horizon)
    assert result.monthly_projection == pytest.approx(3000.0), (
        f"a flat $100/day forecast over {horizon} days projects "
        f"${result.monthly_projection:,.2f}/month")


def test_zero_horizon_projects_nothing():
    assert Forecaster("111122223333").fit([100.0] * 5).predict(0).monthly_projection == 0.0


def test_forecast_tool_fits_off_the_event_loop(monkeypatch):
    import finops.server as srv
    from finops.ml import forecasting
    from finops.tools import forecast as forecast_tool

    class _AWS:
        async def is_configured(self):
            return True

    async def _resolve(account_id):
        return "111122223333"

    monkeypatch.setattr(srv, "require_pro", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_resolve_account_id", _resolve)
    monkeypatch.setitem(srv.CLOUD_CONNECTORS, "aws", _AWS())

    seen: dict = {}

    def _for_account(account_id, service=None, days=90, aws_connector=None):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return Forecaster(account_id).fit([100.0] * 5)

    monkeypatch.setattr(forecasting.Forecaster, "for_account", staticmethod(_for_account))

    out = asyncio.run(forecast_tool.forecast_costs(horizon_days=7))
    assert "error" not in out, out
    assert seen["on_loop"] is False, "the blocking fit ran on the event loop"
