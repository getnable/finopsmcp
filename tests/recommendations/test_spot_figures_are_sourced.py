"""Every Spot saving says which Spot Advisor figure it is, and none is a guess.

spot_adoption priced every instance its us-east-1 Spot Advisor snapshot does
not cover at "_default": 0.65, a figure the module itself calls "Not from Spot
Advisor: an assumed figure". The nine Spot Advisor savings it holds
(us-east-1 Linux, file Last-Modified 2026-03-20) are 0.58, 0.59, 0.59, 0.61,
0.61, 0.62, 0.64, 0.68 and 0.70; their median is 0.61. On the fixture account
the two POSSIBLE rows (a c5.2xlarge in us-east-1, a c5.xlarge in eu-west-1)
were $161.33 and $80.67 at 65%.

The Kubernetes connector priced every spot node at 0.35 of on-demand
("spots ~65% cheaper on average"); for an EKS c5.xlarge node in us-east-1 the
same table says 58% off, 0.42 of on-demand.

The RECOMMENDED threshold (interruption under 5%) is unchanged.
"""
from __future__ import annotations

import statistics
from datetime import UTC, datetime
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

from finops.recommendations import spot_adoption as sa

_HELD = [0.58, 0.59, 0.59, 0.61, 0.61, 0.62, 0.64, 0.68, 0.70]


def test_the_default_is_the_median_of_the_figures_held():
    held = sorted(v for k, v in sa.SPOT_DISCOUNT.items() if k != "_default")
    assert held == _HELD
    assert sa.SPOT_DISCOUNT["_default"] == statistics.median(_HELD) == 0.61


def test_a_covered_type_in_us_east_1_uses_its_own_figure():
    saving, basis = sa.spot_saving("c5.xlarge", "us-east-1")
    assert saving == 0.58
    assert "us-east-1" in basis and "c5.xlarge" in basis, basis


@pytest.mark.parametrize(("itype", "region"), [("c5.xlarge", "eu-west-1"),
                                               ("c5.2xlarge", "us-east-1")])
def test_anything_else_uses_the_median_and_says_whose_figures(itype, region):
    saving, basis = sa.spot_saving(itype, region)
    assert saving == 0.61
    assert "median" in basis and "us-east-1" in basis, basis
    if region != "us-east-1":
        assert f"no {region} figure" in basis, basis


def test_each_row_carries_its_basis():
    ec2, cw = MagicMock(), MagicMock()
    ec2.get_paginator.return_value.paginate.return_value = [{"Reservations": [{"Instances": [{
        "InstanceId": "i-1", "InstanceType": "m5.large",
        "Tags": [{"Key": "Name", "Value": "web"}, {"Key": "env", "Value": "staging"}]}]}]}]
    now = datetime.now(UTC)
    cw.get_metric_statistics.return_value = {"Datapoints": [
        {"Timestamp": now, "Average": 5.0}, {"Timestamp": now, "Average": 6.0}]}
    [row] = sa._analyze_region(ec2, cw, {"i-1"}, "eu-west-1", [])
    assert row["savings_pct"] == 61.0
    assert row["savings_basis"] == "spot_advisor_median"
    assert "no eu-west-1 figure" in row["savings_basis_note"], row


def _k8s_nodes(itype: str, zone: str, provider_labels: dict):
    from finops.connectors.kubernetes import KubernetesConnector

    labels = {"node.kubernetes.io/instance-type": itype, "topology.kubernetes.io/zone": zone,
              **provider_labels}
    node = NS(metadata=NS(name="n1", labels=labels),
              status=NS(allocatable={"cpu": "4", "memory": "8Gi"},
                        capacity={"cpu": "4", "memory": "8Gi"}))
    client = NS(CoreV1Api=lambda: NS(list_node=lambda: NS(items=[node])))
    return KubernetesConnector()._get_nodes(client, "aws")


def test_an_eks_spot_node_is_priced_from_the_spot_advisor_figure():
    from finops.connectors.kubernetes import _node_monthly_cost

    [n] = _k8s_nodes("c5.xlarge", "us-east-1a", {"eks.amazonaws.com/capacityType": "SPOT"})
    assert n.monthly_cost == pytest.approx(_node_monthly_cost("c5.xlarge", "aws") * 0.42)
    assert "Spot Advisor" in n.cost_basis and "c5.xlarge" in n.cost_basis, n.cost_basis


def test_a_spot_factor_with_no_source_is_labelled_an_assumption():
    from finops.connectors.kubernetes import _spot_factor

    factor, basis = _spot_factor("n2-standard-4", "us-central1-a", "gcp")
    assert factor == 0.35
    assert "assum" in basis, basis
