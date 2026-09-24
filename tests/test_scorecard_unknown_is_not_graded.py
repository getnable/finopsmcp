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
