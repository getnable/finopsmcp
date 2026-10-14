"""Regression tests from an audit of the MCP tool surface.

Each test is one reproduced tool call: the input a client sent, and the answer
that was wrong. Calls go through the real MCP dispatch (the in-memory client),
so the instrumentation wrapper, argument validation and output serialisation
are all on the path, exactly as for Claude or any other MCP client.
"""
from __future__ import annotations

import pytest

from tests.mcp_support import call as _call


@pytest.fixture
def db(monkeypatch, tmp_path):
    """A fresh local database and data dir for the tools that keep state."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    import finops.storage.db as db_mod
    db_mod._ENGINE = None
    yield db_mod
    db_mod._ENGINE = None


# ── check_budget_status: no budget checked is not "all on track" ──────────────

def test_budget_status_with_no_budgets_is_not_all_on_track(db):
    """check_budget_status() on a machine with no budget answered
    "✅ All budgets on track." about zero budgets."""
    out = _call("check_budget_status")

    assert "on track" not in out["alert"], out
    assert out["summary"]["total_budgets"] == 0
    assert out.get("no_budgets") is True
    assert "set_budget" in out["alert"]


def test_budget_status_for_a_name_that_matches_nothing_says_so(db):
    """check_budget_status(budget_name="platform") with only a "data" budget
    answered "✅ All budgets on track." for a budget that does not exist."""
    _call("set_budget", {"name": "data", "limit_usd": 500.0})
    out = _call("check_budget_status", {"budget_name": "platform"})

    assert "on track" not in out.get("alert", ""), out
    assert "platform" in out["error"]
    assert out["budgets_available"] == ["data"]


# ── write tools: an argument they cannot honour is refused, nothing saved ─────

@pytest.mark.parametrize(("args", "word"), [
    # Saved with the alert above the block: a budget that blocks before it warns.
    ({"alert_at_pct": 150, "block_at_pct": 50}, "alert_at_pct"),
    # Saved as "fortnightly", then checked over a rolling 30 days.
    ({"period": "fortnightly"}, "period"),
    ({"alert_at_pct": 0}, "alert_at_pct"),
])
def test_set_budget_refuses_thresholds_or_periods_it_cannot_enforce(db, args, word):
    out = _call("set_budget", {"name": "x", "limit_usd": 100.0, **args})

    assert word in out.get("error", ""), out
    assert _call("list_budgets")["count"] == 0, "the budget was saved anyway"


def test_set_ai_budget_refuses_an_unknown_mode(db, monkeypatch, tmp_path):
    """set_ai_budget(mode="galaxy") answered "Saved." and stored mode ""."""
    monkeypatch.setenv("NABLE_HOME", str(tmp_path / "nable"))
    out = _call("set_ai_budget", {"mode": "galaxy", "spend_cap": 50.0})

    assert "mode" in out.get("error", ""), out
    assert "Nothing was saved" in out["error"]


def test_subscribe_to_report_refuses_an_unknown_frequency(db):
    """frequency="hourly" was saved as the weekly cron, with no word about it."""
    out = _call("subscribe_to_report", {"name": "x", "sections": ["spend"],
                                        "frequency": "hourly"})

    assert "frequency" in out.get("error", ""), out
    assert _call("list_report_subscriptions")["count"] == 0


@pytest.mark.parametrize(("args", "word"), [
    ({"formats": ["exe"]}, "formats"),     # "Report generated" with no file written
    ({"sections": ["nope"]}, "sections"),  # "Make sure a provider is configured"
])
def test_export_cost_report_names_the_argument_it_cannot_use(db, args, word):
    out = _call("export_cost_report", {"open_file": False, **args})

    assert word in out.get("error", ""), out
    assert "files" not in out


# ── slice_costs: total_shown is what the shown rows add up to ─────────────────

def _focus(service: str, cost: float):
    from datetime import UTC, datetime

    from finops.focus.schema import FocusRecord
    dt = datetime(2026, 9, 20, tzinfo=UTC)
    return FocusRecord(
        BilledCost=cost, EffectiveCost=cost, ListCost=cost,
        ResourceId="r", ResourceName=None, ResourceType="Instance",
        ServiceName=service, ServiceCategory="Compute",
        ProviderName="AWS", PublisherName="AWS",
        RegionId="us-east-1", RegionName="us-east-1",
        BillingPeriodStart=dt, BillingPeriodEnd=dt,
        ChargePeriodStart=dt, ChargePeriodEnd=dt,
        ChargeCategory="Usage", ChargeDescription=None,
        CommitmentDiscountId=None, CommitmentDiscountType=None,
        Tags={}, SubAccountId="1", SubAccountName=None,
    )


@pytest.mark.parametrize(("args", "shown"), [
    ({}, 1280.0),                                              # one KPI row
    ({"dimensions": ["ServiceName"]}, 1280.0),                 # every row shown
    ({"dimensions": ["ServiceName"], "limit": 1}, 1200.0),     # top row only
])
def test_slice_total_shown_is_the_sum_of_the_rows_shown(monkeypatch, args, shown):
    """slice_costs() answered rows=[{"metric": 1280.0}], total 1280.0 and
    total_shown 0.0: the FOCUS engine summed a key the rows do not have."""
    from finops import server

    async def _records(sd, ed, provider=None):
        return [_focus("Amazon EC2", 1200.0), _focus("Amazon S3", 80.0)], {}, ["aws"]

    monkeypatch.setattr(server, "_fetch_focus_records", _records)
    out = _call("slice_costs", args)["result"]

    assert out["total"] == pytest.approx(1280.0)
    assert out["total_shown"] == pytest.approx(shown)
    assert out["total_shown"] == pytest.approx(sum(r["metric"] for r in out["rows"]))


# ── get_commitment_analysis: unknown coverage is not 0% coverage ──────────────

@pytest.mark.parametrize("ri_coverage", [None, 0.0])
def test_commitment_projection_is_not_made_on_unread_savings_plan_coverage(
        monkeypatch, ri_coverage):
    """ce:GetSavingsPlansCoverage denied, EC2 spend readable: the tool reported
    combined_coverage_pct 0.0, a coverage gap of 80 points and "Buying a 1-year
    Compute Savings Plan at $X/mo would save ~$Y/mo", a purchase advised off a
    coverage figure nobody read."""
    from finops.recommendations import commitments as C

    def _analysis(*a, **k):
        return C.CommitmentAnalysis(
            savings_plan_coverage_pct=None, savings_plan_utilization_pct=0.0,
            savings_plan_unused_usd=0.0, ri_coverage_pct=ri_coverage,
            ri_utilization_pct=0.0, ri_unused_usd=0.0,
            uncovered_on_demand_usd=30000.0)

    monkeypatch.setattr(C, "analyze_commitments", _analysis)
    out = _call("get_commitment_analysis")
    actionable = out["actionable_analysis"]

    assert "if_you_bought_more" not in actionable, actionable
    assert actionable["coverage_gap_pct"] is None
    assert "Savings Plans coverage could not be read" in actionable["note"]


# ── forecast_costs: the horizon is bounded ────────────────────────────────────

@pytest.mark.parametrize("args", [
    {"horizon_days": 100000},   # answered with 100,000 daily points, 10.6 MB of JSON
    {"horizon_days": 0},        # answered monthly_projection 0.0 and no points
    {"horizon_days": -10},      # same, a $0 projection from a negative horizon
    {"history_days": 3},        # below the 14 days the docstring says it needs
])
def test_forecast_rejects_a_horizon_or_history_it_cannot_honour(monkeypatch, args):
    from finops.ml import forecasting

    def _never(*a, **k):
        raise AssertionError("forecast_costs fitted a model for an invalid request")

    monkeypatch.setattr(forecasting.Forecaster, "for_account", _never)
    out = _call("forecast_costs", {"account_id": "111122223333", **args})

    assert "error" in out, f"{args} was answered: {str(out)[:200]}"
    assert "monthly_projection" not in out
    key = next(iter(args))
    assert key in out["error"]
