# SPDX-License-Identifier: Apache-2.0
"""Commitment bounds: the policy schema and the post-filter over commitment
advice (finops.recommendations.commitment_bounds).

What has to stay true:
  - bounds only restrict: a recommendation is kept, cut down, or dropped,
    never enlarged
  - several packs' bounds combine to the strictest
  - a figure nable does not have (coverage, a region) never loosens a bound
  - a pack with policies that cannot be loaded withholds purchase advice
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from finops.packs import content
from finops.packs.errors import Problem
from finops.recommendations import commitment_bounds as cb
from finops.recommendations.commitments import _build_recommendations

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)

BOUNDS = """\
version: 1
commitment_bounds:
  - id: house-bounds
    description: At most 80% coverage, one year, no money up front.
    coverage_target_pct: 80
    max_term_months: 12
    payment_options: [no-upfront]
    blackouts:
      - id: eu-graviton-move
        start: "2027-01-01T00:00:00+00:00"
        end: "2027-04-01T00:00:00+00:00"
        reason: Moving eu-west-1 compute to Graviton.
        providers: [aws]
        regions: [eu-west-1]
"""


# The same bounds without the blackout.
NO_BLACKOUT = BOUNDS.split("    blackouts:")[0]


def _parse(text):
    problems: list[Problem] = []
    items = content.parse_policies(content.safe_load(text), "b.yaml", problems)
    return items, problems


def _bounds(text=BOUNDS, pack="io.github.example/p"):
    from dataclasses import replace
    items, problems = _parse(text)
    assert not problems, problems
    return cb.merge([replace(i, pack=pack) for i in items])


def _rec(coverage=40.0, baseline=10_000.0):
    [rec] = [r for r in _build_recommendations(coverage, baseline * 3, 90, 90,
                                               [baseline, baseline * 1.1, baseline * 1.2])
             if r["type"] == "savings_plan"]
    return rec


# ── the schema ────────────────────────────────────────────────────────────────

def test_bounds_parse_and_rules_still_parse_beside_them():
    items, problems = _parse(BOUNDS)
    assert not problems
    [b] = items
    assert (b.coverage_target_pct, b.max_term_months, b.payment_options) == \
        (80.0, 12, ("no-upfront",))
    assert b.blackouts[0].regions == ("eu-west-1",)
    both = BOUNDS + """\
rules:
  - id: flag-big
    description: A big purchase.
    applies_to: finding
    match: {all: [{field: monthly_usd, op: gt, value: 1000}]}
    effect: {action: flag, message: big}
"""
    items, problems = _parse(both)
    assert not problems and [type(i).__name__ for i in items] == ["PolicyRule",
                                                                  "CommitmentBounds"]


@pytest.mark.parametrize("change, words", [
    ("coverage_target_pct: 80", "coverage_target_pct: 180"),
    ("max_term_months: 12", "max_term_months: 0"),
    ("payment_options: [no-upfront]", "payment_options: [pay-later]"),
    ('start: "2027-01-01T00:00:00+00:00"', 'start: "2027-01-01T00:00:00"'),
    ("providers: [aws]", "providers: [oracle]"),
    ("    max_term_months: 12\n", "    max_term_months: 12\n    min_term_months: 1\n"),
])
def test_a_bad_bound_is_a_problem(change, words):
    _, problems = _parse(BOUNDS.replace(change, words))
    assert problems


def test_a_bound_that_bounds_nothing_is_refused():
    _, problems = _parse("version: 1\ncommitment_bounds:\n  - id: x\n    description: y\n")
    assert any("sets no bound" in str(p) for p in problems)


# ── merge: the strictest wins ─────────────────────────────────────────────────

def test_bounds_from_two_packs_combine_to_the_strictest():
    from dataclasses import replace
    a, _ = _parse(BOUNDS)
    b, _ = _parse(BOUNDS.replace("house-bounds", "other").replace("80", "60")
                  .replace("max_term_months: 12", "max_term_months: 36")
                  .replace("[no-upfront]", "[no-upfront, partial-upfront]"))
    m = cb.merge([replace(a[0], pack="p1"), replace(b[0], pack="p2")])
    assert m.coverage_target_pct == 60 and m.by["coverage_target_pct"] == "p2:other"
    assert m.max_term_months == 12 and m.payment_options == ("no-upfront",)
    assert len(m.blackouts) == 2


# ── judge and the post-filter ─────────────────────────────────────────────────

def test_a_purchase_over_the_coverage_target_is_cut_to_it():
    rec = _rec(coverage=40.0)
    kept, changed = cb.apply_compute([rec], _bounds(NO_BLACKOUT), now=NOW)
    [cut] = kept
    f = cut["bounds"]["factor"]
    # 40% covered: covering the whole baseline would reach 100%. To stop at
    # 80%, keep (0.8 * total - covered) / baseline = 2/3 of it.
    assert f == pytest.approx(2 / 3, abs=1e-3)
    assert cut["commitment_per_month"] == pytest.approx(rec["commitment_per_month"] * 2 / 3, abs=0.01)
    assert cut["monthly_savings"] < rec["monthly_savings"]
    assert "80% target" in cut["description"] and "io.github.example/p:house-bounds" in \
        cut["description"]
    assert changed[0]["dropped"] is False


def test_at_or_over_the_target_the_purchase_is_dropped():
    kept, changed = cb.apply_compute([_rec(coverage=59.0)], _bounds(
        NO_BLACKOUT.replace("80", "55")), now=NOW)
    assert kept == [] and changed[0]["dropped"] and "at or over the 55% target" in \
        changed[0]["why"]


def test_warnings_pass_through_untouched():
    recs = _build_recommendations(40.0, 30_000, 50, 50, [10_000, 11_000])
    kept, _ = cb.apply_compute(recs, _bounds(), now=NOW)
    assert [r for r in kept if r["type"] == "warning"] == \
        [r for r in recs if r["type"] == "warning"]


def test_term_and_payment_outside_the_bounds_drop_it():
    b = _bounds()
    rec = {**_rec(), "term": "3-year"}
    j = cb.judge(b, cb.compute_view(rec), now=NOW)
    assert j.action == "drop" and "36-month term is over the 12-month maximum" in j.reasons[0]
    rec = {**_rec(), "payment": "all-upfront"}
    j = cb.judge(b, cb.compute_view(rec), now=NOW)
    assert j.action == "drop" and "all-upfront is not one of those allowed" in j.reasons[0]


def test_a_blackout_the_term_would_run_into_drops_it_and_a_region_nable_cannot_pin_counts():
    b = _bounds()
    # A 12-month Compute SP bought today runs into January: it spans regions,
    # so the eu-west-1 blackout overlaps it.
    j = cb.judge(b, cb.compute_view(_rec()), now=NOW)
    assert j.action == "drop" and "eu-graviton-move" in " ".join(j.reasons)
    # A purchase pinned to another region, or bought after the window, does not.
    view = {**cb.compute_view(_rec()), "region": "us-east-1"}
    assert cb.judge(b, view, now=NOW).action == "cut"
    later = datetime(2027, 5, 1, tzinfo=UTC)
    assert cb.judge(b, cb.compute_view(_rec()), now=later).action == "cut"


def test_no_coverage_figure_never_loosens_a_coverage_target():
    view = {**cb.compute_view(_rec()), "coverage_pct_now": None}
    j = cb.judge(_bounds(NO_BLACKOUT), view, now=NOW)
    assert j.action == "drop" and "unknown" in j.reasons[0]


def test_an_unloadable_pack_with_policies_withholds_purchase_advice():
    b = cb.Bounds(unreadable=["io.github.example/p is not loaded: files changed"])
    kept, changed = cb.apply_compute([_rec()], b, now=NOW)
    assert kept == [] and "could not be loaded" in changed[0]["why"]


def test_the_database_plan_and_the_projection_are_bounded_too():
    result = {"current_monthly_rds_spend": 5000.0, "current_sp_coverage_pct": 20.0,
              "uncovered_monthly_spend": 4000.0, "recommended_sp_hourly_commitment": 3.8356,
              "estimated_monthly_savings": 1200.0, "estimated_annual_savings": 14400.0,
              "finding": {"title": "x", "metadata": {"recommended_sp_hourly_commitment": 3.8356}}}
    out = cb.apply_database(result, _bounds(NO_BLACKOUT), now=NOW)
    assert out["bounds"]["cut"] and out["estimated_monthly_savings"] < 1200
    assert out["finding"]["metadata"]["recommended_sp_hourly_commitment"] == \
        out["recommended_sp_hourly_commitment"]
    dropped = cb.apply_database(result, _bounds(), now=NOW)
    assert dropped["bounds"]["dropped"] and dropped["finding"] is None
    assert dropped["estimated_monthly_savings"] == 0
    actionable = {"combined_coverage_pct": 40.0, "coverage_target_pct": 80.0,
                  "monthly_uncovered_on_demand_usd": 10_000.0,
                  "if_you_bought_more": {"additional_monthly_commitment_usd": 5000.0,
                                         "projected_monthly_savings_usd": 1700.0,
                                         "projected_annual_savings_usd": 20400.0,
                                         "description": "buy"}}
    capped = cb.cap_projection(actionable, coverage_pct=40.0,
                               bounds=_bounds(NO_BLACKOUT.replace("80", "50")), now=NOW)
    assert capped["coverage_target_pct"] == 50
    assert capped["if_you_bought_more"]["additional_monthly_commitment_usd"] < 5000
    assert "if_you_bought_more_withheld" in cb.cap_projection(
        actionable, coverage_pct=40.0, bounds=_bounds(), now=NOW)


def test_no_bounds_changes_nothing():
    rec = _rec()
    assert cb.apply_compute([rec], None) == ([rec], [])
    assert cb.merge([]) is None


def test_bounds_never_enlarge_a_recommendation():
    b = _bounds(NO_BLACKOUT)
    for cov in (0.0, 10.0, 35.5, 59.9):
        for base in (600.0, 5000.0, 123456.0):
            rec = _rec(coverage=cov, baseline=base) if cov < 60 else None
            if rec is None:
                continue
            kept, _ = cb.apply_compute([rec], b, now=NOW)
            for r in kept:
                assert r["commitment_per_month"] <= rec["commitment_per_month"]
                assert r["monthly_savings"] <= rec["monthly_savings"]


def test_now_defaults_to_the_clock():
    j = cb.judge(_bounds(), cb.compute_view(_rec()))
    assert j.action in ("cut", "drop")
    assert datetime.now(UTC) - NOW < timedelta(days=36500)
