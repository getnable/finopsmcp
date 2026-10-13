"""Prices corrected by the 2026-10-13 audit, pinned at their primary-source value.

Each test names the offer file (or page) and the SKU the value was read from,
and would have failed on the value the code held before. Downloaded offer files
are not committed; the SKUs are enough to find each row again at
https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<offer>/<version>/us-east-1/index.csv.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from finops.aws_prices import HOURS_PER_MONTH, RDS_HOURLY, rds_hourly

# ── RDS ──────────────────────────────────────────────────────────────────────

def test_rds_m5_8xlarge_is_the_listed_rate_not_sixteen_larges():
    # AmazonRDS offer 20261006224050, us-east-1, Single-AZ: SKU D5NU4Q3STSENKMSM
    # (MySQL) and K9FM4VKJBEDZ49XX (MariaDB) are $2.74. The table held $2.736,
    # 16 x the db.m5.large rate.
    assert RDS_HOURLY["db.m5.8xlarge"] == 2.74
    assert rds_hourly("db.m5.8xlarge", "mysql") == 2.74
    assert rds_hourly("db.m5.8xlarge", "mariadb") == 2.74


def test_guard_prices_rds_m5_8xlarge_at_the_listed_rate():
    from finops.guard import estimate_command_monthly_cost

    est = estimate_command_monthly_cost(
        "aws rds create-db-instance --db-instance-identifier x "
        "--db-instance-class db.m5.8xlarge --engine mysql")
    assert est["hourly_usd"] == 2.74
    assert est["monthly_usd"] == round(2.74 * HOURS_PER_MONTH, 2)


# ── Terraform estimator: ElastiCache, OpenSearch, MSK, Redshift ──────────────

def _rc(rtype: str, after: dict):
    from finops.connectors.terraform_estimate import ResourceChange

    return ResourceChange(f"{rtype}.x", rtype, ["create"], None, after)


@pytest.mark.parametrize("node,was,now", [
    # AmazonElastiCache offer 20260914063714, NodeUsage:<type>, Redis OSS.
    ("cache.m5.large", 0.139, 0.156),      # D3Y7CNKGUTH3CE59
    ("cache.m6g.large", 0.128, 0.149),     # BCWUFWUMAS4WVJ4Q
    ("cache.r5.large", 0.207, 0.216),      # 63VV29ZM5Q5TPWMH
    ("cache.r6g.large", 0.186, 0.206),     # XRYU5DPB7QK6S94A
    ("cache.r6g.2xlarge", 0.744, 0.821),   # SW4HDKMXGK2KPX5T
])
def test_elasticache_rows_are_the_listed_rates(node, was, now):
    from finops.connectors.terraform_estimate import _ELASTICACHE_HOURLY, elasticache_hourly

    assert _ELASTICACHE_HOURLY[node] == now != was
    assert elasticache_hourly(node, "memcached") == now


def test_valkey_is_priced_at_its_own_lower_rate():
    from finops.connectors.terraform_estimate import elasticache_hourly

    # Valkey, cache.r6g.large: SKU RV2VBCDBY8RZE2WZ, $0.1648 (20% under Redis).
    assert elasticache_hourly("cache.r6g.large", "valkey") == 0.1648
    assert elasticache_hourly("cache.r6g.large", "redis") == 0.206


def test_a_cluster_mode_replication_group_counts_every_node():
    from finops.connectors.terraform_estimate import _estimate_elasticache

    # 3 shards x (1 primary + 1 replica) = 6 nodes. num_cache_nodes is not an
    # attribute of a replication group, so this used to price one node.
    line = _estimate_elasticache(_rc("aws_elasticache_replication_group", {
        "node_type": "cache.r6g.large", "num_node_groups": 3, "replicas_per_node_group": 1}))
    assert line.monthly_delta == pytest.approx(6 * 0.206 * HOURS_PER_MONTH)
    line = _estimate_elasticache(_rc("aws_elasticache_replication_group", {
        "node_type": "cache.t4g.small", "num_cache_clusters": 2, "engine": "valkey"}))
    assert line.monthly_delta == pytest.approx(2 * 0.0256 * HOURS_PER_MONTH)


@pytest.mark.parametrize("node,was,now", [
    # AmazonES offer 20260927122424, ESInstance:<type>.
    ("c5.large.search", 0.096, 0.125),     # CUV96RRT7ZXEQGCP
    ("c5.xlarge.search", 0.192, 0.251),    # JED863GNYTGS3V3D
    ("m5.xlarge.search", 0.285, 0.283),    # Z3HZ7P7Q4GNGBQFG
    ("r5.large.search", 0.187, 0.186),     # GSTK76R4P7QA5DPG
    ("r5.xlarge.search", 0.374, 0.372),    # WSC5QMCRTPHYWTDJ
])
def test_opensearch_rows_are_the_listed_rates(node, was, now):
    from finops.connectors.terraform_estimate import _OPENSEARCH_HOURLY

    assert _OPENSEARCH_HOURLY[node] == now != was


@pytest.mark.parametrize("broker,was,now", [
    # AmazonMSK offer 20260911124456, USE1-Kafka.<type> broker hours.
    ("kafka.t3.small", 0.021, 0.0456),     # EAENBG6PQA5U8JTN
    ("kafka.m5.large", 0.142, 0.21),       # CBMW8TY4JC6XHKHM
    ("kafka.m5.8xlarge", 2.272, 3.36),     # 59HBRTXVJJUW6ATD
    ("kafka.m7g.large", 0.128, 0.204),     # NR4E2TGDUNJZTB8H
])
def test_msk_rows_are_the_listed_rates(broker, was, now):
    from finops.connectors.terraform_estimate import _MSK_BROKER_HOURLY

    assert _MSK_BROKER_HOURLY[broker] == now != was


def test_msk_counts_brokers_not_subnets():
    from finops.connectors.terraform_estimate import _estimate_msk

    line = _estimate_msk(_rc("aws_msk_cluster", {
        "number_of_broker_nodes": 6,
        "broker_node_group_info": [{"instance_type": "kafka.m5.large",
                                    "client_subnets": ["a", "b", "c"]}]}))
    assert line.monthly_delta == pytest.approx(6 * 0.21 * HOURS_PER_MONTH)


def test_redshift_ra3_4xlarge_is_priced_and_the_made_up_type_is_gone():
    from finops.connectors.terraform_estimate import _REDSHIFT_HOURLY, _estimate_redshift

    # AmazonRedshift offer 20260911124505, Node:ra3.4xlarge, SKU ZYZ6HF4BA4HB6RED.
    assert "ra3.4xlplus" not in _REDSHIFT_HOURLY
    line = _estimate_redshift(_rc("aws_redshift_cluster",
                                  {"node_type": "ra3.4xlarge", "number_of_nodes": 2}))
    assert line.monthly_delta == pytest.approx(2 * 3.26 * HOURS_PER_MONTH)


@pytest.mark.parametrize("rtype,after", [
    ("aws_opensearch_domain", {"cluster_config": [{"instance_type": "r6g.4xlarge.search"}]}),
    ("aws_msk_cluster", {"number_of_broker_nodes": 3,
                         "broker_node_group_info": [{"instance_type": "kafka.m7g.4xlarge"}]}),
    ("aws_redshift_cluster", {"node_type": "ra3.64xlarge"}),
    ("aws_elasticache_cluster", {"node_type": "cache.r7g.12xlarge"}),
])
def test_an_unknown_node_type_gets_no_figure_not_a_guessed_one(rtype, after):
    from finops.connectors.terraform_estimate import _ESTIMATORS

    line = _ESTIMATORS[rtype](_rc(rtype, after))
    assert line.monthly_delta == 0.0
    assert line.confidence == "low" and "not priced" in line.detail


def test_vscode_mirror_follows_the_corrected_tables():
    from finops.vscode_extension_prices import price_resource_py

    assert price_resource_py("aws_msk_cluster", {
        "instance_type": "kafka.m5.large", "number_of_broker_nodes": "3"})["monthly"] == \
        round(3 * 0.21 * HOURS_PER_MONTH, 2)
    assert price_resource_py("aws_opensearch_domain", {
        "instance_type": "r6g.4xlarge.search"})["monthly"] == 0.0
    # Kinesis, $0.015 per shard-hour (AmazonKinesis offer 20260911124443, SKU
    # 7RXVY4V5SG8NQC38), over the 730-hour month rather than 720.
    assert price_resource_py("aws_kinesis_stream", {"shard_count": "2"})["monthly"] == 21.9


def test_pr_estimator_prices_per_node_services_by_type(monkeypatch):
    from finops.pr_comments import estimator
    from finops.pr_comments.parser import ResourceChange

    class _Rates:
        has_private_pricing = False
        confidence = "low"

        def effective_multiplier(self):
            return 1.0

    monkeypatch.setattr(estimator, "detect_effective_rates", lambda: _Rates())
    changes = [
        ResourceChange("add", "aws_redshift_cluster", "dw", "aws",
                       {"node_type": "ra3.4xlarge", "number_of_nodes": "2"}),
        ResourceChange("add", "aws_msk_cluster", "kafka", "aws",
                       {"instance_type": "kafka.m5.large", "number_of_broker_nodes": "3"}),
        ResourceChange("add", "aws_elasticache_cluster", "cache", "aws",
                       {"node_type": "cache.t3.micro", "num_cache_nodes": "1"}),
        ResourceChange("add", "aws_redshift_cluster", "odd", "aws", {"node_type": "xx.large"}),
        ResourceChange("add", "aws_eks_node_group", "ng", "aws", {"desired_size": "3"}),
    ]
    got = {e.resource_name: e for e in estimator.estimate_changes(changes)}
    # These were a flat $180, $200 and $52 whatever the node type and count.
    assert got["dw"].monthly_usd == round(2 * 3.26 * HOURS_PER_MONTH, 2)
    assert got["kafka"].monthly_usd == round(3 * 0.21 * HOURS_PER_MONTH, 2)
    assert got["cache"].monthly_usd == round(0.017 * HOURS_PER_MONTH, 2)
    assert got["odd"].monthly_usd == 0 and got["odd"].confidence == "low"
    # No instance_types in the diff: no longer priced as m5.large.
    assert got["ng"].monthly_usd == 0 and got["ng"].confidence == "low"


_PRICES_TS = Path(__file__).resolve().parents[1] / "vscode-extension" / "src" / "prices.ts"


def _ts_table(name: str) -> dict[str, float]:
    text = _PRICES_TS.read_text(encoding="utf-8")
    body = re.search(rf"const {name}: Record<string, number> = \{{(.*?)\n\}};", text, re.DOTALL)
    assert body, f"{name} not found in {_PRICES_TS}"
    return {k: float(v) for k, v in re.findall(r'"([^"]+)":\s*([0-9.]+)', body.group(1))}


@pytest.mark.skipif(not _PRICES_TS.exists(), reason="VS Code extension source not present")
@pytest.mark.parametrize("ts_name,py_name", [
    ("ELASTICACHE_HOURLY", "_ELASTICACHE_HOURLY"),
    ("ELASTICACHE_VALKEY_HOURLY", "_ELASTICACHE_VALKEY_HOURLY"),
    ("OPENSEARCH_HOURLY", "_OPENSEARCH_HOURLY"),
    ("REDSHIFT_HOURLY", "_REDSHIFT_HOURLY"),
    ("MSK_HOURLY", "_MSK_BROKER_HOURLY"),
])
def test_vscode_typescript_tables_match_the_python_ones(ts_name, py_name):
    from finops.connectors import terraform_estimate

    ts = _ts_table(ts_name)
    py = getattr(terraform_estimate, py_name)
    assert ts and {k: v for k, v in ts.items() if py.get(k) != v} == {}


@pytest.mark.skipif(not _PRICES_TS.exists(), reason="VS Code extension source not present")
def test_vscode_typescript_has_no_guessed_fallback_rates():
    text = _PRICES_TS.read_text(encoding="utf-8")
    # `?? 0.142` priced every unknown OpenSearch and MSK type as an m5.large.
    assert not re.search(r"\?\?\s*0\.\d", text)
    # DynamoDB on-demand: $0.625/M WRU (FGXVD96DKJMUASY3), $0.125/M RRU
    # (4W4ZMC46EHE8XTTZ), AmazonDynamoDB offer 20260911124422.
    assert "$0.625/M writes, $0.125/M reads" in text
