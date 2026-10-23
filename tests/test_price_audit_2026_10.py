"""Prices corrected by the 2026-10-13 audit, pinned at their primary-source value.

Each test names the offer file (or page) and the SKU the value was read from,
and would have failed on the value the code held before. Downloaded offer files
are not committed; the SKUs are enough to find each row again at
https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<offer>/<version>/us-east-1/index.csv.
"""
from __future__ import annotations

import asyncio
import inspect
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


# ── the guard ────────────────────────────────────────────────────────────────

def test_guard_prices_ebs_volumes_launched_with_the_instances():
    from finops.guard import estimate_command_monthly_cost

    est = estimate_command_monthly_cost(
        "aws ec2 run-instances --image-id ami-1 --instance-type m5.large --count 4 "
        "--block-device-mappings '[{\"DeviceName\":\"/dev/sdb\",\"Ebs\":{\"VolumeSize\":16000,"
        "\"VolumeType\":\"io2\",\"Iops\":64000}}]'")
    # Per volume: 16,000 GB x $0.125 + 32,000 IOPS x $0.065 + 32,000 x $0.0455
    # (AmazonEC2 offer 20261012193925: 6V576P37PS7K7KYU, X9AGQNCQXFA9Q8XZ,
    # KDKG2R3W4NMV7CHP) = $5,536/mo. This read 4 x m5.large only, ~$280/mo.
    assert est["ebs_monthly_usd"] == 4 * 5536.0
    assert est["monthly_usd"] == round(4 * 0.096 * HOURS_PER_MONTH + 4 * 5536.0, 2)
    shorthand = estimate_command_monthly_cost(
        "aws ec2 run-instances --image-id ami-1 --instance-type m5.large "
        "--block-device-mappings DeviceName=/dev/sdb,Ebs={VolumeSize=500,VolumeType=gp3}")
    assert shorthand["ebs_monthly_usd"] == 40.0
    plain = estimate_command_monthly_cost("aws ec2 run-instances --instance-type m5.large")
    assert "root volume is not counted" in plain["line"]


@pytest.mark.parametrize("cmd,region,premium", [
    ("aws ec2 run-instances --region ap-southeast-1 --instance-type m5.large",
     "ap-southeast-1", 1.149),
    ("AWS_REGION=eu-west-1 aws ec2 run-instances --instance-type m5.large", "eu-west-1", 1.069),
    (("aws rds create-db-instance --region=us-west-2 --db-instance-class db.m5.large "
      "--engine mysql"), "us-west-2", None),
])
def test_guard_says_when_a_us_east_1_figure_is_for_another_region(cmd, region, premium):
    from finops.guard import estimate_command_monthly_cost

    # m5.large lists $0.12 in ap-southeast-1 against $0.096 in us-east-1 (AmazonEC2
    # offer 20261012193925). An EC2 launch is judged in the region it names (the
    # guard audit's regional pricing): $0.096 x 1.149 = $0.1103/hr there, the
    # least ap-southeast-1 charges over us-east-1, at or under its $0.12 list,
    # and the figure no longer calls itself the us-east-1 rate. eu-west-1: $0.096
    # x 1.069 = $0.1026/hr. RDS is priced at the region's own listed row
    # (AmazonRDS us-west-2 offer 20261006224050, D168), and its basis names it.
    est = estimate_command_monthly_cost(cmd)
    assert est["region"] == region
    if premium is None:
        assert f"on-demand {region} list price" in est["basis"] and "AmazonRDS" in est["basis"]
        assert f"in {region}" in est["line"] and "often higher" not in est["line"]
    else:
        assert est["hourly_usd"] == pytest.approx(0.096 * premium)
        assert f"in {region}" in est["line"] and "the least" in est["basis"]
        assert "us-east-1 list rate" not in est["basis"] and "often higher" not in est["line"]
    same = estimate_command_monthly_cost(
        "aws ec2 run-instances --region us-east-1 --instance-type m5.large")
    assert "region" not in same and "targets" not in same["line"]


def test_guard_prices_a_node_group_at_its_dearest_type_and_labels_spot():
    from finops.guard import estimate_command_monthly_cost

    est = estimate_command_monthly_cost(
        "aws eks create-nodegroup --cluster-name c --nodegroup-name n "
        "--instance-types g5.xlarge,g5.12xlarge --capacity-type SPOT "
        "--scaling-config minSize=1,maxSize=10,desiredSize=4")
    # It took the first type, g5.xlarge ($1.006), for a group that may launch
    # g5.12xlarge ($5.672, SKU UH59G55UJ94HZTJJ), and called a SPOT group on-demand.
    assert est["instance_type"] == "g5.12xlarge" and est["hourly_usd"] == 5.672
    assert "spot prices vary" in est["basis"]


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


# ── DocumentDB and Kendra ────────────────────────────────────────────────────

@pytest.mark.parametrize("cls,was,now", [
    # AmazonDocDB offer 20260911124422, InstanceUsage:<class>, standard storage.
    ("db.t3.medium", 0.076, 0.078),        # H2KGW7B35EBSGXNN
    ("db.r6g.large", 0.249, 0.26315),      # 772AQPZNUUFB9TKD
    ("db.r6g.16xlarge", 7.973, 8.41928),   # N8D5QCNAFXE798U4
    ("db.r5.2xlarge", 1.109, 1.108),       # XSBQ4TEXP5ZUXUUK
    ("db.r5.24xlarge", 13.308, 13.296),    # YSBXKJMAJKG7E6QA
])
def test_documentdb_rows_are_the_listed_rates(cls, was, now):
    from finops.connectors.aws_services.documentdb import _INSTANCE_HOURLY_USD

    assert _INSTANCE_HOURLY_USD[cls] == now != was


def test_kendra_editions_are_list_hours_times_730():
    from finops.connectors.aws_services.kendra import _EDITION_MONTHLY_USD

    # AmazonKendra offer 20260911124443: Developer NSTMQK2AFGH9HENZ $1.125/hr,
    # Enterprise 6ENMMB4KRPAZVYZ2 $1.40/hr. The table held $810 and $1,400.
    assert _EDITION_MONTHLY_USD == {"DEVELOPER_EDITION": 821.25, "ENTERPRISE_EDITION": 1022.0}


# ── Savings Plans and Reserved Instances ─────────────────────────────────────

def test_commitment_discounts_are_the_listed_one_year_no_upfront_rates():
    from finops.recommendations import commitments as c

    # AWSComputeSavingsPlan us-east-1 20261013035015, 1yr No Upfront:
    # Compute SP m5.large $0.071 vs $0.096 on demand (26.0% off, SKU
    # BB7BKBSC6NZW7P5B); EC2 Instance SP m6i.large $0.0635 (33.9%, DC4GCSR3H4S43EMU).
    # These were 0.66 (34% off), 0.72 (28%) and 0.60 (40%).
    assert c._COMPUTE_SP_DISCOUNT == 0.74
    assert c._EC2_SP_DISCOUNT == 0.662
    assert c._RI_DISCOUNT == 0.662
    # A Compute SP never discounts more than an EC2 Instance SP on one type.
    assert c._COMPUTE_SP_DISCOUNT > c._EC2_SP_DISCOUNT


def test_commitment_projection_commits_at_the_discounted_rate(monkeypatch):
    from finops import server  # noqa: F401  (initialises finops.tools first)
    from finops.recommendations import commitments as c
    from finops.tools.commitments import get_commitment_analysis

    analysis = c.CommitmentAnalysis(
        savings_plan_coverage_pct=20.0, savings_plan_utilization_pct=100.0,
        savings_plan_unused_usd=0.0, ri_coverage_pct=20.0, ri_utilization_pct=100.0,
        ri_unused_usd=0.0, uncovered_on_demand_usd=30_000.0)   # $10,000 a month
    monkeypatch.setattr(c, "analyze_commitments", lambda *a, **k: analysis)
    monkeypatch.setattr(c, "commitment_summary", lambda *a, **k: {})
    out = get_commitment_analysis()
    if inspect.iscoroutine(out):          # the MCP tool wrapper is async
        out = asyncio.run(out)
    more = out["actionable_analysis"]["if_you_bought_more"]
    # Half the $10,000 gap is $5,000 of on-demand; at 26% off that is a $3,700
    # commitment saving $1,300. It said a $5,000 commitment saving $1,700.
    assert more["additional_monthly_commitment_usd"] == 3700.0
    assert more["projected_monthly_savings_usd"] == 1300.0


def test_database_savings_plan_is_twenty_percent():
    from finops.recommendations import database_savings_plans as d

    # AWSDatabaseSavingsPlans us-east-1 20261007173435: 1yr No Upfront only,
    # 20% off every RDS instance hour it covers (SKU DZCHH8H3SPVYZ2Z4).
    assert d.DATABASE_SP_DISCOUNT_1YR_NO_UPFRONT == 0.20
    assert d.PRICE_SOURCES["DATABASE_SP_DISCOUNT_1YR_ALL_UPFRONT"]["verified"] == "no"


# ── Google Cloud ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("machine,was,now", [
    # cloud.google.com/products/compute/pricing/general-purpose, Iowa
    # (us-central1), Default column, read 2026-10-13, x 730.
    ("n2-standard-8", 241.95, 283.58),     # $0.388472/hr
    ("n2-standard-2", 60.49, 70.90),       # $0.097118/hr
    ("n1-standard-4", 106.92, 138.70),     # $0.189999/hr
    ("n1-standard-1", 26.73, 34.67),       # $0.04749975/hr
    ("e2-standard-4", 97.82, 97.84),       # $0.13402284/hr
])
def test_gce_node_rows_are_the_us_central1_list_rates(machine, was, now):
    from finops.connectors.kubernetes import _GKE_MONTHLY, _node_monthly_cost

    assert _GKE_MONTHLY[machine] == now != was
    assert _node_monthly_cost(machine, "gcp") == now


def test_guard_labels_the_gce_region_and_the_unverified_azure_table():
    from finops.guard import estimate_command_monthly_cost

    gce = estimate_command_monthly_cost(
        "gcloud compute instances create vm-1 --machine-type n2-standard-8")
    assert gce["monthly_usd"] == 283.58 and "us-central1" in gce["basis"]
    az = estimate_command_monthly_cost("az vm create -g rg -n vm1 --size Standard_D4s_v5")
    assert "not verified" in az["basis"]


def test_gcp_disk_and_snapshot_rates():
    from finops.recommendations import gcp_waste

    # cloud.google.com/compute/disks-image-pricing, us-central1: Hyperdisk
    # Balanced space $0.000109589/GiB-hr ($0.08/mo, was $0.10); regional standard
    # snapshot storage $0.000068493/GiB-hr ($0.05/mo, was the pre-2022 $0.026).
    assert gcp_waste.DISK_RATES["hyperdisk-balanced"] == 0.08
    assert gcp_waste.SNAPSHOT_GB_MONTHLY == 0.05
    # The per-vCPU idle-VM floor is e2-highcpu's $18.06, not $24.
    assert gcp_waste.VM_VCPU_MONTHLY == 18.0


# ── LLM token prices ─────────────────────────────────────────────────────────

def test_claude_sonnet_5_5_is_priced_at_the_published_rates():
    from finops import llm_prices

    # https://platform.claude.com/docs/en/about-claude/pricing, read 2026-10-13:
    # $2 in, $10 out, $2.50 5m write, $4 1h write, $0.10 cache read (0.05x). The
    # same rates are the Bedrock global-endpoint rates (AmazonBedrockFoundationModels
    # us-east-1 20261012084008: PXMCKMF8EGSRB3GB, HP32J6RXJZ3XYFUM, CES7A5M9YH89UF9G,
    # SW4A7584KK4SK2MK, 487WBAJQRFHYFE5M). It used to read as unpriced.
    p = llm_prices.price_for("claude-sonnet-5-5")
    assert (p.input, p.output, p.cache_write_5m, p.cache_write_1h, p.cache_read) == \
        (2.00, 10.00, 2.50, 4.00, 0.10)
    assert llm_prices.price_for("us.anthropic.claude-sonnet-5-5-v1:0") is p


def test_claude_haiku_5_5_stays_unpriced_because_its_price_depends_on_prompt_length():
    from finops import llm_prices

    # $0.10/$0.50 per million up to 100,000 prompt tokens, $0.50/$2.50 above:
    # no single row is right for a daily total, so it must not borrow one.
    assert llm_prices.price_for("claude-haiku-5-5") is None


def test_llm_prices_date_each_provider_and_claim_the_oldest():
    from finops import llm_prices

    assert llm_prices.AS_OF_BY_PROVIDER == {"anthropic": "2026-10-13", "openai": "2026-09-24"}
    assert llm_prices.AS_OF == "2026-09-24"


def test_the_stale_vertex_rate_table_is_gone():
    from finops.connectors.saas import vertex_costs

    # Unused, and Gemini 2.0 Flash at half its listed $0.15/$0.60 per million.
    assert not hasattr(vertex_costs, "_VERTEX_PRICING")


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
