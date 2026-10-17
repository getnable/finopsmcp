"""The scorecard headline uses the commitment saving the card body shows.

build_scorecard added the commitment dimension's potential_savings_usd to
the headline "Estimated $X/month recoverable" times 0.7, a haircut with no
source in the code, while the commitment card's own action line printed the
whole figure. On the fixture account the card said "estimated savings:
$240/month" and the headline said $168. The plan saving is already sized to
the floor month of uncovered spend at the published plan rate; the headline
now carries the same number.
"""
from __future__ import annotations

import pytest

from finops.scoring.scorecard import build_scorecard


def test_the_headline_is_the_card_figure():
    card = build_scorecard(
        scope="overall", label="Overall",
        commitment_data={"coverage_pct": 18.6, "on_demand_usd": 942.52,
                         "potential_savings_usd": 239.73},
        total_monthly_spend=6000.0,
    )
    [commits] = [d for d in card.dimensions if d.name == "commitment_coverage"]
    assert "estimated savings: $240/month" in " ".join(commits.actions), commits.actions
    assert card.potential_savings_usd == pytest.approx(239.73)
    assert "$240/month recoverable" in card.summary, card.summary
