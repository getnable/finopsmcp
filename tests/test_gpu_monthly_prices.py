"""GPU node prices are hourly list price x 730, not x 1000.

The monthly tables in connectors/kubernetes.py and pr_comments/estimator.py had
g4dn and g5 entries that were the hourly rate times 1000 (g4dn.xlarge $0.526/hr
stored as $526/mo, g5.xlarge $1.006/hr as $1,006/mo), and g4dn.2xlarge and
4xlarge as multiples of the xlarge figure rather than their own rates. A GPU
node group read 37% high or worse, and so did every "idle node" saving on it.
The p3 rows were right, which is how it looked plausible.
"""
from __future__ import annotations

import pytest

from finops.aws_prices import HOURS_PER_MONTH
from finops.connectors import kubernetes
from finops.connectors.terraform_estimate import _EC2_HOURLY
from finops.pr_comments import estimator

_GPU = ["p3.2xlarge", "p3.8xlarge", "g4dn.xlarge", "g4dn.2xlarge",
        "g4dn.4xlarge", "g5.xlarge", "g5.2xlarge"]


@pytest.mark.parametrize("itype", _GPU)
def test_kubernetes_gpu_node_is_hourly_times_730(itype):
    expected = round(_EC2_HOURLY[itype] * HOURS_PER_MONTH, 2)
    assert kubernetes._node_monthly_cost(itype) == pytest.approx(expected)


@pytest.mark.parametrize("itype", [t for t in _GPU if t in estimator._EC2_MONTHLY])
def test_pr_estimator_gpu_is_hourly_times_730(itype):
    expected = round(_EC2_HOURLY[itype] * HOURS_PER_MONTH, 2)
    assert estimator._ec2_monthly(itype) == pytest.approx(expected)


def test_the_headline_case():
    # $0.526/hr x 730 = $383.98/mo, not $526.
    assert kubernetes._node_monthly_cost("g4dn.xlarge") == pytest.approx(383.98)
    assert estimator._ec2_monthly("g4dn.xlarge") == pytest.approx(383.98)
