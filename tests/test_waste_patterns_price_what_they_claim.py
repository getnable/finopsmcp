"""A cost-history waste pattern prices no more than its own premise gives.

ml.patterns' gp2 fallback, used when no volume metadata is in the context
(scan_waste_patterns never passes any), takes "~4% of EC2 is often EBS gp2"
and remediates with "gp3: 20% cost reduction", then priced the finding at
the whole 4%: the cost of the gp2 volumes, as if moving them to gp3 removed
them. A move to gp3 saves the 20% gap (AWS Price List, AmazonEC2 us-east-1
20261012193925: gp2 $0.10, gp3 $0.08 per GB-month, HY3BZPP2B6K8MSJF and
JG3KUJMBRGHV3N8G), so by its own premise the pattern is 0.04 x 0.20 = 0.8%
of EC2 spend. On a two-region fixture account it read $47.94/mo where the
deep audit, reading the volumes, measured $7.60/mo of gp2 to gp3 savings.
"""
from __future__ import annotations

import pytest

from finops.ml.patterns import PatternContext, _check_gp2_volumes


def test_the_gp2_fallback_prices_the_gp3_gap_not_the_gp2_bill():
    ec2 = {"Amazon Elastic Compute Cloud - Compute": [40.0] * 30}   # $1,200/mo
    m = _check_gp2_volumes(PatternContext(daily_costs=ec2, by_resource=[], snapshots=[]))
    assert m is not None
    # 4% of $1,200 is $48 of presumed gp2; gp3 is 20% under gp2, so $9.60.
    assert m.monthly_waste == pytest.approx(1200.0 * 0.04 * 0.20)
    assert m.monthly_waste == pytest.approx(9.60)
