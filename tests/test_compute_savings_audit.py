"""Compute and commitment savings, each figure held to its primary source.

Every test here pins a number an audit of the compute and commitment engines
found wrong, with the inputs, the wrong figure and the right one. Where a
figure comes from the public AWS Price List, the offer, region and version it
was read from are named next to it.
"""
from __future__ import annotations

import pytest


# ── the sweep reports a percentage once, not times a hundred ─────────────────
#
# graviton._compute_savings and spot_adoption both hand over savings_pct as a
# percentage (15.0 for fifteen percent). sweep.normalise multiplied it by 100
# again, so run_full_cost_audit and the report exports printed an m5.xlarge to
# m7g.xlarge move as "1500% saving" and a spot conversion as "7200% saving".

def test_the_sweep_prints_a_graviton_saving_as_the_percentage_it_is():
    from finops.recommendations.graviton import _compute_savings
    from finops.recommendations.sweep import normalise

    current, saving, pct = _compute_savings("m5.xlarge", "m7g.xlarge")
    # us-east-1 Linux on-demand: m5.xlarge 0.192, m7g.xlarge 0.1632 a hour.
    assert (current, saving, pct) == (140.16, 21.02, 15.0)
    [f] = normalise("graviton", [{
        "instance_id": "i-1", "instance_type": "m5.xlarge",
        "graviton_equivalent": "m7g.xlarge", "savings_estimate": saving,
        "savings_pct": pct, "region": "us-east-1",
        "current_monthly_cost_estimate": current}])
    assert f["detail"].startswith("15% saving")
    assert "1500%" not in f["detail"]


def test_the_sweep_prints_a_spot_saving_as_the_percentage_it_is():
    from finops.recommendations.sweep import normalise

    [f] = normalise("spot", [{
        "instance_id": "i-2", "instance_type": "m5.large", "monthly_savings": 41.35,
        "savings_pct": 59.0, "recommendation": "RECOMMENDED"}])
    assert f["detail"] == "59% saving"
