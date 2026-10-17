"""Kubernetes request-based allocation: every node dollar lands somewhere once.

allocate_cluster_costs split each node's cost over its pods by their share of
the node's requests, and called a node idle only when it had no pods at all.
The unrequested part of a node that ran one small pod went nowhere: neither
allocated nor idle, so idle_cost and idle_pct missed it (a node 10% requested
read 0% idle). It also scaled allocatable by 0.95 on top of what kubelet
already reserves, so pods that requested the whole node were allocated 105%
of its cost.

_parse_mem read the decimal suffixes as if they were binary: "1G" is 10^9
bytes, 953.67 MiB, not 1,000 MiB.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from finops.aws_prices import EC2_HOURLY, HOURS_PER_MONTH
from finops.connectors import kubernetes_costs as kc

_NODE_MONTHLY = EC2_HOURLY["m5.xlarge"] * HOURS_PER_MONTH


def _node(name: str) -> NS:
    res = {"cpu": "4", "memory": "16Gi"}
    return NS(metadata=NS(name=name, labels={"node.kubernetes.io/instance-type": "m5.xlarge"}),
              status=NS(capacity=res, allocatable=res))


def _pod(name: str, node: str, cpu: str, mem: str) -> NS:
    container = NS(resources=NS(requests={"cpu": cpu, "memory": mem}))
    return NS(metadata=NS(name=name, namespace="app", labels={}, owner_references=[]),
              spec=NS(node_name=node, containers=[container]),
              status=NS(phase="Running"))


def _report(monkeypatch, pods):
    api = NS(list_node=lambda: NS(items=[_node("n1")]),
             list_pod_for_all_namespaces=lambda: NS(items=pods))
    monkeypatch.setattr(kc, "_k8s_client", lambda: (api, None))
    return kc.allocate_cluster_costs("c")


def test_the_unrequested_part_of_a_busy_node_is_idle(monkeypatch):
    # 10% of the node's CPU and memory requested.
    r = _report(monkeypatch, [_pod("api", "n1", "400m", "1638Mi")])
    assert r.allocated_cost_usd == pytest.approx(0.1 * _NODE_MONTHLY, abs=0.05)
    assert r.idle_cost_usd == pytest.approx(0.9 * _NODE_MONTHLY, abs=0.05)
    assert r.allocated_cost_usd + r.idle_cost_usd == pytest.approx(_NODE_MONTHLY, abs=0.02)


def test_pods_that_request_the_whole_node_are_allocated_its_cost_once(monkeypatch):
    r = _report(monkeypatch, [_pod("a", "n1", "2", "8Gi"), _pod("b", "n1", "2", "8Gi")])
    assert r.allocated_cost_usd == pytest.approx(_NODE_MONTHLY, abs=0.02)
    assert r.idle_cost_usd == pytest.approx(0.0, abs=0.02)


def test_decimal_memory_suffixes_are_decimal():
    assert kc._parse_mem("1G") == pytest.approx(10 ** 9 / 2 ** 20)
    assert kc._parse_mem("500M") == pytest.approx(500 * 10 ** 6 / 2 ** 20)
    assert kc._parse_mem("1Gi") == 1024
