"""A scorecard dimension with no data says so; it does not get a grade.

Two ways "we could not measure this" turned into a confident letter:

  - get_efficiency_scorecard passed `combined_coverage_pct or 0.0` into the
    commitment dimension, so an account whose coverage could not be read (a
    missing ce:GetSavingsPlansCoverage is enough) was scored 0/100 and graded F,
    with "Only 0% of compute is under commitments" as the finding.
  - _score_waste_reduction scored 75/B ("No significant waste detected") when
    it had been handed no waste inputs at all, and 100/A when it also had a
    spend figure: absence of data read as absence of waste.

Both now come back data_available=False, the shape the other dimensions
already use for "no data yet".
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    from finops.storage import db

    saved_engine, saved_dir = db._ENGINE, db._DATA_DIR
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "finops.db"))
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    db._ENGINE, db._DATA_DIR = None, None
    yield
    db._ENGINE, db._DATA_DIR = saved_engine, saved_dir


def _dim(card: dict, name: str) -> dict:
    return next(d for d in card["dimensions"] if d["name"] == name)


def test_unreadable_commitment_coverage_is_not_graded_f(monkeypatch):
    import finops.server  # noqa: F401  (registers the tools module's server)
    from finops.recommendations import commitments
    from finops.recommendations.commitments import CommitmentAnalysis
    from finops.tools import attribution

    unreadable = CommitmentAnalysis(
        savings_plan_coverage_pct=None, savings_plan_utilization_pct=0.0,
        savings_plan_unused_usd=0.0, ri_coverage_pct=None, ri_utilization_pct=0.0,
        ri_unused_usd=0.0, uncovered_on_demand_usd=30000.0, recommendations=[],
    )
    monkeypatch.setattr(commitments, "analyze_commitments",
                        lambda tag_filter=None: unreadable)

    card = asyncio.run(attribution.get_efficiency_scorecard())

    dim = _dim(card, "commitment_coverage")
    assert dim["data_available"] is False, dim
    assert dim["grade"] != "F" and dim["score"] > 0, (
        f"coverage that could not be read was graded {dim['grade']} "
        f"({dim['score']}/100): {dim['findings']}")
    assert not any("0% of compute" in f for f in dim["findings"]), dim["findings"]


def test_commitment_dimension_treats_none_coverage_as_unknown():
    from finops.scoring.scorecard import _score_commitment_coverage

    dim = _score_commitment_coverage({"coverage_pct": None, "on_demand_usd": 1000.0,
                                      "potential_savings_usd": 0.0})
    assert dim.data_available is False
    assert dim.raw_score > 0


def test_measured_zero_coverage_is_still_graded():
    from finops.scoring.scorecard import _score_commitment_coverage

    dim = _score_commitment_coverage({"coverage_pct": 0.0, "coverage_known": True,
                                      "on_demand_usd": 1000.0,
                                      "potential_savings_usd": 0.0})
    assert dim.data_available is True
    assert dim.grade == "F"


@pytest.mark.parametrize("total_spend", [0.0, 50_000.0])
def test_waste_with_no_inputs_is_no_data_not_a_b(total_spend):
    from finops.scoring.scorecard import _score_waste_reduction

    dim = _score_waste_reduction(None, None, None, total_spend=total_spend)
    assert dim.data_available is False, (
        f"no waste inputs at all scored {dim.raw_score}/{dim.grade}: {dim.findings}")
    assert dim.metadata.get("total_waste_usd") == 0.0


def test_waste_with_inputs_that_found_nothing_is_still_scored():
    from finops.scoring.scorecard import _score_waste_reduction

    dim = _score_waste_reduction([], None, None, total_spend=50_000.0)
    assert dim.data_available is True
    assert dim.grade == "A"


# ── the trend compares like with like ─────────────────────────────────────────

def _week_old_score(total: float, details: dict) -> None:
    import json
    from datetime import date, timedelta

    from finops.storage.db import get_engine, scorecard_history
    day = (date.today() - timedelta(days=8)).isoformat()  # noqa: DTZ011 - as _persist_score dates it
    with get_engine().begin() as conn:
        conn.execute(scorecard_history.insert().values(
            scope="overall", score_date=day, total_score=total, grade="C",
            details=json.dumps(details), captured_at=f"{day}T00:00:00Z"))


def test_a_trend_over_the_same_dimensions_is_reported():
    from finops.scoring import scorecard
    _week_old_score(60.0, {"available_dimensions": ["anomaly_response", "waste_reduction"]})
    trend, delta = scorecard._get_score_trend(
        "overall", 70.0, ["waste_reduction", "anomaly_response"])
    assert trend == "improving" and delta == 10.0


def test_a_trend_over_different_dimensions_is_not_comparable():
    from finops.scoring import scorecard
    _week_old_score(60.0, {"available_dimensions": ["anomaly_response"]})
    assert scorecard._get_score_trend("overall", 90.0, ["anomaly_response", "tag_hygiene"]) \
        == ("not_comparable", 0.0)


def test_a_score_saved_without_its_dimensions_is_not_compared():
    from finops.scoring import scorecard
    _week_old_score(60.0, {"waste_reduction": 50.0})
    assert scorecard._get_score_trend("overall", 90.0, ["waste_reduction"]) \
        == ("not_comparable", 0.0)
    assert scorecard._get_score_trend("overall", 90.0)[0] == "improving"   # old callers


def test_the_scorecard_persists_what_it_measured_and_says_when_it_cannot_compare():
    import json

    from sqlalchemy import select

    from finops.scoring import scorecard
    from finops.storage.db import get_engine, scorecard_history
    _week_old_score(40.0, {"available_dimensions": ["anomaly_response"]})
    card = scorecard.build_scorecard(idle_resources=[], tag_coverage={"team": 90.0},
                                     required_tags=["team"])
    assert card.grade != "N/A"
    assert card.trend == "not_comparable" and "no trend" in card.summary
    with get_engine().connect() as conn:
        rows = conn.execute(select(scorecard_history.c.details)
                            .order_by(scorecard_history.c.score_date.desc())).fetchall()
    saved = json.loads(rows[0][0])
    assert saved["available_dimensions"] == sorted(
        d.name for d in card.dimensions if d.data_available)
