"""Commitment coverage never averages two percentages of different things.

Savings Plans coverage is a share of eligible SPEND (GetSavingsPlansCoverage:
SpendCoveredBySavingsPlans / TotalCost). RI coverage is a share of running
HOURS (GetReservationCoverage: CoverageHoursPercentage). combined_coverage_pct
averaged the two. On a fixture account with a Compute Savings Plan covering
$280.32 of $1,503.48 eligible spend (18.6%) and no Reserved Instances (RI
coverage 0%), get_commitment_analysis reported combined coverage 9.3% and the
scorecard said "Only 9% of compute is under commitments".

The rule now: a spend-weighted combination needs spend for both, and the RI
read reports hours, so when both instruments cover something the two are
reported apart and the Savings Plans figure is the headline. When only one
covers anything (no RIs, or no plans), or only one could be read, that one is
the figure.
"""
from __future__ import annotations

import asyncio

import pytest

from finops.recommendations.commitments import CommitmentAnalysis, _build_recommendations


def _analysis(sp, ri) -> CommitmentAnalysis:
    return CommitmentAnalysis(
        savings_plan_coverage_pct=sp, savings_plan_utilization_pct=100.0,
        savings_plan_unused_usd=0.0, ri_coverage_pct=ri, ri_utilization_pct=0.0,
        ri_unused_usd=0.0, uncovered_on_demand_usd=2827.57,
        recommendations=_build_recommendations(
            sp, 2827.57, 100.0, 0.0, monthly_uncovered_series=[952.76, 952.76, 922.03]))


@pytest.mark.parametrize(("sp", "ri", "combined", "basis"), [
    (18.6, 0.0, 18.6, "no RI"),           # the fixture: a plan, no RIs
    (0.0, 40.0, 40.0, "no Savings Plans"),
    (55.0, 25.0, 55.0, "separately"),     # both: SP headlined, RI reported apart
    (None, 40.0, 40.0, "not read"),
    (30.0, None, 30.0, "not read"),
    (None, None, None, "not read"),
])
def test_combined_coverage_is_never_an_average(sp, ri, combined, basis):
    a = _analysis(sp, ri)
    assert a.combined_coverage_pct == combined
    assert basis in a.coverage_basis, a.coverage_basis


def test_the_scorecard_says_what_the_plan_covers(monkeypatch, tmp_path):
    import finops.server  # noqa: F401  (registers the tools module's server)
    from finops.recommendations import commitments
    from finops.storage import db
    from finops.tools import attribution

    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(db, "_ENGINE", None)
    a = _analysis(18.6, 0.0)
    monkeypatch.setattr(commitments, "analyze_commitments", lambda tag_filter=None: a)
    card = asyncio.run(attribution.get_efficiency_scorecard())
    monkeypatch.setattr(db, "_ENGINE", None)
    [dim] = [d for d in card["dimensions"] if d["name"] == "commitment_coverage"]
    said = " ".join(dim["findings"])
    assert "18.6%" in said, said
    assert "9%" not in said and "9.3%" not in said, said
    assert dim["metadata"]["coverage_pct"] == pytest.approx(18.6)


def test_get_commitment_analysis_reports_the_basis(monkeypatch):
    from finops import server
    from finops.recommendations import commitments

    a = _analysis(18.6, 0.0)
    monkeypatch.setattr(commitments, "analyze_commitments", lambda *x, **k: a)
    out = server.get_commitment_analysis()
    if asyncio.iscoroutine(out):
        out = asyncio.run(out)
    act = out["actionable_analysis"]
    assert act["combined_coverage_pct"] == 18.6
    assert "no RI" in act["combined_coverage_basis"]
    assert act["coverage_gap_pct"] == pytest.approx(80.0 - 18.6)
