# SPDX-License-Identifier: Apache-2.0
"""Regional list prices, and an effective figure only on an exact list (D168).

What has to stay true:
  - aws_prices.elasticache_rate() answers from
    src/finops/price_data/elasticache_on_demand.json, which
    scripts/build_elasticache_price_tables.py writes from the public AWS Price
    List for the sixteen regions rds_on_demand.json holds: every rate is the
    listed USD string with its SKU, region offer and version; the tests build
    from the trimmed real offer excerpts in tests/fixtures/aws_price_list/
    elasticache, and only the opt-in live re-check reads the network
  - an ElastiCache create-cache-cluster, an RDS create-db-instance (through
    aws_prices.rds_rate) and a saved Terraform plan's EC2, RDS and ElastiCache
    resources are shown at their own region's listed rate
  - rule 13: in a region whose listed rate is below us-east-1's, the figure
    the guard judges stays the us-east-1 figure, so no ask the guard raised
    before disappears
  - an effective figure appears only for a SKU whose list is that region's
    exact listed rate; an EC2 floor, a region nable holds no rate for, or a
    scaled size says "Priced at list price" and why; and for every SKU the
    effective figure is either absent or the exact list times the factor,
    never below it (rule 15)
  - the tables are read without waiting, and only when they are regular
    files: a FIFO in place of one costs the figure, never the hook's time
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import pytest

import finops.guard as g
from finops import ai_budget, aws_prices, cloud_rates, cloud_standing
from finops.aws_prices import EC2_HOURLY, EC2_REGION_RANGE, HOURS_PER_MONTH
from finops.connectors.terraform_estimate import (
    _ELASTICACHE_HOURLY,
    _ELASTICACHE_VALKEY_HOURLY,
    elasticache_hourly,
    estimate_plan,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "aws_price_list" / "elasticache"
SHIPPED = ROOT / "src" / "finops" / "price_data" / "elasticache_on_demand.json"
RDS_SHIPPED = ROOT / "src" / "finops" / "price_data" / "rds_on_demand.json"
VERSION = "20261014221139"
LIVE_ENV = "FINOPS_LIVE_PRICE_LIST"
BOX = "https://box.example.com"
WS_A = "ws_" + "a" * 32
AT = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
FACTOR = "0.8000"


def _builder():
    spec = importlib.util.spec_from_file_location(
        "build_elasticache_price_tables", ROOT / "scripts" / "build_elasticache_price_tables.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _shipped() -> dict:
    return json.loads(SHIPPED.read_text(encoding="utf-8"))


def _fixture(region: str) -> dict:
    return json.loads((FIXTURES / f"{region}.json").read_text(encoding="utf-8"))


def _cents_up(x: Decimal) -> float:
    return float(x.quantize(Decimal("0.01"), rounding=ROUND_CEILING))


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_VELOCITY_CAP_USD",
                "FINOPS_POLICY_LOOP_COUNT", "FINOPS_GUARD_TEAM", "FINOPS_GUARD_ACCOUNT",
                "FINOPS_POLICY_ON_BUDGET_BREACH", "FINOPS_POLICY_FILE", "FINOPS_PROFILE",
                "FINOPS_DATA_DIR", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE",
                "AWS_DEFAULT_PROFILE", "XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL",
                "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM", "FINOPS_GUARD_BUDGET_MAX_AGE_HOURS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "no-accounts.yaml"))
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})
    monkeypatch.setattr(cloud_standing, "now_epoch", lambda: AT.timestamp())
    db = sys.modules.get("finops.storage.db")
    if db is not None:
        monkeypatch.setattr(db, "_DATA_DIR", None)


def _row(product, usage_type, region, factor=FACTOR):
    return {"product_code": product, "region": region, "usage_type": usage_type,
            "unit": "Hrs", "on_demand_factor": factor, "blended_factor": factor,
            "covered_share": "0.5000", "days": 30, "line_items": 120}


def keep_rates(rows):
    as_of = "2026-10-18"
    start = (datetime.fromisoformat(as_of) - timedelta(days=29)).date().isoformat()
    block = {"v": 1, "read": True, "workspace_ref": WS_A, "as_of": as_of,
             "window": {"start": start, "end": as_of},
             "source": "the AWS cost export at s3://acme-bills/cur/", "columns": [],
             "rows": rows, "more": 0, "not_served": {}}
    cloud_standing.bind(BOX, workspace=WS_A)
    got = cloud_rates.keep(block, box=BOX, workspace_ref=WS_A, fetched_at=AT)
    assert got["kept"], got


# ── the table and its builder ────────────────────────────────────────────────

@pytest.mark.parametrize("node,engine,region,usd,sku", [
    ("cache.r6g.large", "redis", "eu-west-1", "0.229", "FQHREBFZBQ25BR4G"),
    ("cache.r6g.large", "memcached", "eu-west-1", "0.229", "5HFBQVVZ8GBVHSN2"),
    ("cache.r6g.large", "valkey", "eu-west-1", "0.1832", "CW3MMD9P4QCM3VTV"),
    ("cache.r6g.large", "redis", "us-east-1", "0.206", "XRYU5DPB7QK6S94A"),
])
def test_rows_read_as_the_offer_lists_them(node, engine, region, usd, sku):
    rate = aws_prices.elasticache_rate(node, engine, region)
    assert rate is not None
    assert Decimal(rate.hourly_usd) == Decimal(usd)
    assert rate.sku == sku and rate.region == region and rate.engine == engine
    assert f"AmazonElastiCache {region} offer, version {VERSION}, SKU {sku}" == \
        rate.source.removeprefix("AWS Price List, ")
    assert rate.as_of == "2026-10-22"


def test_the_shipped_table_covers_the_sixteen_regions_rds_holds():
    doc = _shipped()
    rds = json.loads(RDS_SHIPPED.read_text(encoding="utf-8"))
    assert set(doc["regions"]) == set(rds["regions"]) and len(doc["regions"]) == 16
    for region, entry in doc["regions"].items():
        assert entry["version"] == VERSION and entry["rows"], region
        assert entry["url"].endswith(f"/AmazonElastiCache/{VERSION}/{region}/index.json")
    assert _builder().REGIONS == tuple(sorted(rds["regions"], key=_builder().REGIONS.index))


@pytest.mark.parametrize("engine, table", [("redis", _ELASTICACHE_HOURLY),
                                           ("memcached", _ELASTICACHE_HOURLY),
                                           ("valkey", _ELASTICACHE_VALKEY_HOURLY)])
def test_every_us_east_1_row_the_estimator_holds_agrees_with_the_table(engine, table):
    """The two sources cannot drift apart unnoticed: the figure the guard
    judged before is the table's us-east-1 row."""
    for node, hourly in table.items():
        rate = aws_prices.elasticache_rate(node, engine, "us-east-1")
        assert rate is not None, node
        assert Decimal(rate.hourly_usd) == Decimal(str(hourly)), (node, engine)


def test_a_sku_that_reads_as_a_token_is_escaped_and_reads_back_whole():
    raw = SHIPPED.read_text(encoding="utf-8")
    assert '"AP7' not in raw and "\\u0041P7E9WZCBT5MRM3R" in raw
    rows = {r[3] for e in _shipped()["regions"].values() for r in e["rows"]}
    assert "AP7E9WZCBT5MRM3R" in rows  # pragma: allowlist secret - a Price List SKU


@pytest.mark.parametrize("node,engine,region", [
    ("cache.r6g.large", "redis", "af-south-1"), ("cache.nope.large", "redis", "eu-west-1"),
    ("cache.r6g.large", "mongodb", "eu-west-1")])
def test_what_the_table_does_not_hold_is_none(node, engine, region):
    assert aws_prices.elasticache_rate(node, engine, region) is None


def test_engine_and_region_are_read_without_regard_to_case():
    a = aws_prices.elasticache_rate("cache.r6g.large", "Redis", "EU-WEST-1")
    assert a is not None and a.sku == "FQHREBFZBQ25BR4G"


def test_the_table_loads_on_first_use_not_on_import():
    code = ("import finops.aws_prices as a; "
            "assert a._CACHE_REGIONAL is None, 'read at import'; "
            "a.elasticache_rate('cache.r6g.large', 'redis', 'us-east-1'); "
            "assert a._CACHE_REGIONAL, 'not read on use'")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=60, check=False)
    assert r.returncode == 0, r.stderr


def test_an_unreadable_table_gives_no_figure(monkeypatch, tmp_path):
    monkeypatch.setattr(aws_prices, "_CACHE_REGIONAL", None)
    monkeypatch.setattr(aws_prices, "ELASTICACHE_ON_DEMAND_JSON", str(tmp_path / "missing.json"))
    assert aws_prices.elasticache_rate("cache.r6g.large", "redis", "us-east-1") is None
    monkeypatch.setattr(aws_prices, "_CACHE_REGIONAL", None)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
@pytest.mark.parametrize("which", ["ELASTICACHE_ON_DEMAND_JSON", "RDS_ON_DEMAND_JSON"])
def test_a_fifo_in_place_of_a_table_is_no_figure_never_a_wait(tmp_path, which):
    """The guard's hook reads the tables: a FIFO in place of one is read
    without waiting and refused as not a regular file."""
    fifo = tmp_path / "table.json"
    os.mkfifo(fifo)
    ask = ("a.elasticache_rate('cache.r6g.large', 'redis', 'us-east-1')"
           if which == "ELASTICACHE_ON_DEMAND_JSON"
           else "a.rds_rate('db.m5.large', 'mysql', 'us-east-1')")
    code = (f"import finops.aws_prices as a; a.{which} = {str(fifo)!r}; "
            f"assert {ask} is None")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=20, check=False)
    assert r.returncode == 0, r.stderr


def test_a_price_source_names_the_table_and_its_offer():
    entry = aws_prices.PRICE_SOURCES["ELASTICACHE_ON_DEMAND_JSON"]
    doc = _shipped()
    assert VERSION in entry["source"] and "AmazonElastiCache" in entry["source"]
    assert entry["as_of"] == max(e["read"] for e in doc["regions"].values())
    assert (Path(aws_prices.__file__).parent / aws_prices.ELASTICACHE_ON_DEMAND_JSON) == SHIPPED


@pytest.mark.parametrize("region", ["eu-west-1", "us-east-1"])
def test_the_derivation_reproduces_the_shipped_rows(region):
    build = _builder()
    offer = _fixture(region)
    rows = build.derive(offer, region)
    shipped = {r[3]: r for r in _shipped()["regions"][region]["rows"]}
    assert rows
    for row in rows:
        assert shipped[row[3]] == row, row
    assert offer["version"] == _shipped()["regions"][region]["version"]


def test_the_derivation_keeps_only_the_plain_node_hour():
    """The eu-west-1 excerpt carries rows the table must not hold: Extended
    Support and SyncDurability node hours, an Outposts row, a Serverless
    row and a snapshot row."""
    build = _builder()
    offer = _fixture("eu-west-1")
    kept = {row[3] for row in build.derive(offer, "eu-west-1")}
    assert kept == {"FQHREBFZBQ25BR4G", "5HFBQVVZ8GBVHSN2", "CW3MMD9P4QCM3VTV"}
    assert len(set(offer["products"]) - kept) == 6


def test_a_previous_generation_node_is_not_kept():
    """The October offers list no previous-generation node at a plain node
    hour; a later one may, and the table holds the current generation only."""
    build = _builder()
    offer = _fixture("eu-west-1")
    plain = offer["products"]["FQHREBFZBQ25BR4G"]
    assert build.keep(plain)
    old = {**plain, "attributes": {**plain["attributes"], "currentGeneration": "No"}}
    assert not build.keep(old)


def test_the_build_runs_from_saved_offer_files(tmp_path):
    build = _builder()
    src = tmp_path / "offers"
    src.mkdir()
    for name in ("region_index.json", "eu-west-1.json", "us-east-1.json"):
        (src / name).write_text((FIXTURES / name).read_text())
    out = tmp_path / "elasticache_on_demand.json"
    code = build.main(["--offers-dir", str(src), "--out", str(out), "--read-date", "2026-10-22",
                       "--regions", "eu-west-1", "us-east-1"])
    assert code == 0
    doc = json.loads(out.read_text())
    assert doc["regions"]["eu-west-1"]["url"].endswith(
        f"/AmazonElastiCache/{VERSION}/eu-west-1/index.json")
    assert len(doc["regions"]["eu-west-1"]["rows"]) == 3


def test_a_duplicate_key_fails_loudly():
    build = _builder()
    offer = _fixture("eu-west-1")
    twin = copy.deepcopy(offer["products"]["FQHREBFZBQ25BR4G"])
    offer["products"]["ZZZZZZZZZZZZZZZZ"] = twin
    offer["terms"]["OnDemand"]["ZZZZZZZZZZZZZZZZ"] = copy.deepcopy(
        offer["terms"]["OnDemand"]["FQHREBFZBQ25BR4G"])
    with pytest.raises(build.BuildError, match="duplicate"):
        build.derive(offer, "eu-west-1")


@pytest.mark.parametrize("damage", ["two_dimensions", "no_term", "monthly_unit", "no_usd"])
def test_a_sku_without_exactly_one_hourly_price_fails_loudly(damage):
    build = _builder()
    offer = _fixture("eu-west-1")
    sku = "FQHREBFZBQ25BR4G"
    terms = offer["terms"]["OnDemand"]
    [term] = terms[sku].values()
    [dim_key] = term["priceDimensions"]
    if damage == "two_dimensions":
        term["priceDimensions"]["extra"] = copy.deepcopy(term["priceDimensions"][dim_key])
    elif damage == "no_term":
        del terms[sku]
    elif damage == "monthly_unit":
        term["priceDimensions"][dim_key]["unit"] = "Month"
    else:
        term["priceDimensions"][dim_key]["pricePerUnit"] = {"CNY": "1.0"}
    with pytest.raises(build.BuildError, match=sku):
        build.derive(offer, "eu-west-1")


def test_a_row_from_another_region_fails_loudly():
    build = _builder()
    with pytest.raises(build.BuildError, match="eu-west-1"):
        build.derive(_fixture("eu-west-1"), "eu-west-2")


def test_the_builder_reads_only_the_public_price_list_over_https():
    build = _builder()
    assert build.INDEX_URL == ("https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/"
                               "AmazonElastiCache/current/region_index.json")
    for bad in ("http://pricing.us-east-1.amazonaws.com/x", "file:///etc/passwd",
                "https://example.com/offers/v1.0/aws/AmazonElastiCache/x.json",
                "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/x.json"):
        with pytest.raises(build.BuildError):
            build.fetch_json(bad)


@pytest.mark.skipif(os.environ.get(LIVE_ENV) != "1",
                    reason=f"reads the public AWS Price List; set {LIVE_ENV}=1 to run")
def test_live_the_shipped_table_still_matches_the_price_list():
    """Re-reads the region index and the eu-west-3 offer. A newer version is
    reported, not silently accepted: rebuild with
    scripts/build_elasticache_price_tables.py and review the diff."""
    build = _builder()
    index = build.fetch_json(build.INDEX_URL)
    entry = _shipped()["regions"]["eu-west-3"]
    url = build.HOST + index["regions"]["eu-west-3"]["currentVersionUrl"]
    assert url == entry["url"], f"AWS publishes {url} now; rebuild the table"
    assert build.derive(build.fetch_json(url), "eu-west-3") == entry["rows"]


# ── shown at their own region's listed rate ─────────────────────────────────

CACHE_EU = ("aws elasticache create-cache-cluster --region eu-west-1 --cache-cluster-id s "
            "--engine redis --cache-node-type cache.r6g.large --num-cache-nodes 3")
RDS_EU = ("aws rds create-db-instance --region eu-west-1 --db-instance-identifier orders-db "
          "--engine postgres --db-instance-class db.r6g.large")


def test_an_elasticache_cluster_is_priced_at_its_regions_listed_rate():
    est = g.estimate_command_monthly_cost(CACHE_EU)
    rate = Decimal(aws_prices.elasticache_rate("cache.r6g.large", "redis",
                                               "eu-west-1").hourly_usd)
    assert est["hourly_usd"] == float(rate) and est["region"] == "eu-west-1"
    assert est["monthly_usd"] == round(float(rate) * 3 * HOURS_PER_MONTH, 2)
    assert "FQHREBFZBQ25BR4G" in est["basis"] and "eu-west-1" in est["line"]
    sku = est["sku"]
    assert (sku["region"], sku["usd_hour"], sku["list_exact"], sku["path"]) == (
        "eu-west-1", float(rate), True, "cli")


@pytest.mark.parametrize("multi_az", [False, True])
def test_an_rds_instance_is_priced_through_rds_rate_in_its_region(multi_az):
    cmd = RDS_EU + (" --multi-az" if multi_az else "")
    est = g.estimate_command_monthly_cost(cmd)
    row = aws_prices.rds_rate("db.r6g.large", "postgres", "eu-west-1", multi_az=multi_az)
    assert est["monthly_usd"] == round(float(row.hourly_usd) * HOURS_PER_MONTH, 2)
    assert row.sku in est["basis"]
    sku = est["sku"]
    assert sku["usd_hour"] == float(row.hourly_usd) and sku["list_exact"] is True
    assert sku["usage_type"] == ("Multi-AZUsage" if multi_az else "InstanceUsage") \
        + ":db.r6g.large"


def test_a_region_nable_holds_no_rate_for_is_us_east_1s_and_says_so():
    est = g.estimate_command_monthly_cost(CACHE_EU.replace("eu-west-1", "af-south-1"))
    assert est["hourly_usd"] == elasticache_hourly("cache.r6g.large", "redis")
    sku = est["sku"]
    assert sku["list_exact"] is False and "nable holds no af-south-1" in sku["list_why"]


# ── rule 13: a cheaper region never removes an ask ──────────────────────────

def _cheaper(monkeypatch, region="eu-west-1"):
    """The loaded tables with `region` listing r6g nodes and db.r6g at a
    quarter of us-east-1, as a region priced under it would."""
    cache = copy.deepcopy(aws_prices._cache_regional())
    rows = cache[region]["rows"]
    for key, (usd, sku) in list(rows.items()):
        rows[key] = (str(Decimal(usd) / 4), sku)
    rds = copy.deepcopy(aws_prices._rds_regional())
    rrows = rds[region]["rows"]
    for key, (usd, sku) in list(rrows.items()):
        rrows[key] = (str(Decimal(usd) / 4), sku)
    monkeypatch.setattr(aws_prices, "_CACHE_REGIONAL", cache)
    monkeypatch.setattr(aws_prices, "_RDS_REGIONAL", rds)


BIG_CACHE = ("aws elasticache create-cache-cluster --region eu-west-1 --cache-cluster-id s "
             "--engine redis --cache-node-type cache.r6g.2xlarge --num-cache-nodes 2")
BIG_RDS = ("aws rds create-db-instance --region eu-west-1 --db-instance-identifier o "
           "--engine postgres --db-instance-class db.r5.4xlarge")


@pytest.mark.parametrize("cmd", [BIG_CACHE, BIG_RDS], ids=["elasticache", "rds"])
def test_a_cheaper_region_is_judged_at_the_us_east_1_figure(monkeypatch, cmd):
    before = g.estimate_command_monthly_cost(cmd.replace("eu-west-1", "us-east-1"))
    _cheaper(monkeypatch)
    est = g.estimate_command_monthly_cost(cmd)
    assert est["monthly_usd"] == before["monthly_usd"]
    assert est["sku"]["monthly_usd"] < before["monthly_usd"]
    assert est["sku"]["judged_monthly_usd"] == before["monthly_usd"]
    v = g.gate_command(cmd, record=False)
    assert v is not None and v["decision"] == "ask" and v["rule"] == "threshold"
    assert v["monthly_delta_usd"] == before["monthly_usd"]
    assert "the least the guard judges a region at" in v["reason"]


def _plan(region: str, resources: list[dict]) -> dict:
    return {"format_version": "1.2",
            "configuration": {"provider_config": {"aws": {
                "name": "aws", "expressions": {"region": {"constant_value": region}}}}},
            "resource_changes": resources}


def _rc(address: str, rtype: str, after: dict, actions=("create",)) -> dict:
    return {"address": address, "type": rtype, "mode": "managed",
            "change": {"actions": list(actions), "before": None, "after": after}}


def test_a_cheaper_region_never_lowers_a_saved_plans_judged_figure(monkeypatch, tmp_path):
    doc = _plan("eu-west-1", [
        _rc("aws_db_instance.db", "aws_db_instance",
            {"instance_class": "db.r5.4xlarge", "engine": "postgres", "multi_az": False}),
        _rc("aws_elasticache_cluster.c", "aws_elasticache_cluster",
            {"node_type": "cache.r6g.2xlarge", "engine": "redis", "num_cache_nodes": 2})])
    use1 = estimate_plan(_plan("us-east-1", doc["resource_changes"]))
    _cheaper(monkeypatch)
    got = estimate_plan(doc)
    assert got["monthly_delta_usd"] < use1["monthly_delta_usd"]
    assert got["gate_monthly_delta_usd"] == use1["monthly_delta_usd"]
    (tmp_path / "plan.out").write_bytes(b"binary plan")
    monkeypatch.setattr(g, "_plan_read", lambda *a, **k: ("terraform", "plan.out", doc))
    v = g.gate_command("terraform apply plan.out", cwd=str(tmp_path), record=False)
    assert v["decision"] == "ask" and v["monthly_delta_usd"] == use1["monthly_delta_usd"]


def test_a_removal_in_a_cheaper_region_never_raises_the_saving_judged(monkeypatch):
    """A cost removed is judged at the lower of the two, so the figure judged
    stays at or above us-east-1's (D168)."""
    res = [_rc("aws_elasticache_cluster.c", "aws_elasticache_cluster",
               {"node_type": "cache.r6g.2xlarge", "engine": "redis", "num_cache_nodes": 2},
               actions=("delete",))]
    res[0]["change"]["before"], res[0]["change"]["after"] = res[0]["change"]["after"], None
    use1 = estimate_plan(_plan("us-east-1", res))
    _cheaper(monkeypatch)
    got = estimate_plan(_plan("eu-west-1", res))
    assert got.get("gate_monthly_delta_usd", got["monthly_delta_usd"]) >= \
        use1["monthly_delta_usd"]


# ── an effective figure only on an exact list ───────────────────────────────

def test_elasticache_in_eu_west_1_shows_the_exact_list_times_the_factor():
    keep_rates([_row("AmazonElastiCache", "NodeUsage:cache.r6g.large", "eu-west-1")])
    v = g.gate_command(CACHE_EU.replace("--num-cache-nodes 3", "--num-cache-nodes 6"),
                       record=False)
    assert v["decision"] == "ask"
    rate = Decimal(aws_prices.elasticache_rate("cache.r6g.large", "redis",
                                               "eu-west-1").hourly_usd)
    exact = rate * 6 * Decimal(str(HOURS_PER_MONTH)) * Decimal(FACTOR)
    assert v["price_basis"] == "effective"
    assert v["effective"]["monthly_usd"] == _cents_up(exact)
    assert v["effective"]["monthly_usd"] >= float(exact)
    assert "at your effective rate" in v["reason"]


def _tf_plan(tmp_path, monkeypatch, doc):
    (tmp_path / "plan.out").write_bytes(b"binary plan")
    monkeypatch.setattr(g, "_plan_read", lambda *a, **k: ("terraform", "plan.out", doc))


def test_terraform_ec2_in_ap_southeast_1_is_a_floor_and_priced_at_list(tmp_path, monkeypatch):
    keep_rates([_row("AmazonEC2", "BoxUsage:m5.4xlarge", "ap-southeast-1")])
    _tf_plan(tmp_path, monkeypatch, _plan("ap-southeast-1", [
        _rc("aws_instance.app", "aws_instance", {"instance_type": "m5.4xlarge"})]))
    v = g.gate_command("terraform apply plan.out", cwd=str(tmp_path), record=False)
    low = EC2_REGION_RANGE["ap-southeast-1"][0]
    assert v["decision"] == "ask"
    assert v["priced"]["usd_hour"] == round(EC2_HOURLY["m5.4xlarge"] * low, 6)
    assert v["price_basis"] == "list" and "effective" not in v
    assert "Priced at list price" in v["reason"]
    assert "ap-southeast-1's EC2 floor" in v["reason"] and "not that region's listed rate" \
        in v["reason"]


def test_an_exact_sku_beside_a_floor_sku_prices_only_the_exact_one(tmp_path, monkeypatch):
    keep_rates([_row("AmazonEC2", "BoxUsage:m5.4xlarge", "eu-west-1"),
                _row("AmazonRDS", "InstanceUsage:db.r6g.large", "eu-west-1")])
    _tf_plan(tmp_path, monkeypatch, _plan("eu-west-1", [
        _rc("aws_instance.app", "aws_instance", {"instance_type": "m5.4xlarge"}),
        _rc("aws_db_instance.db", "aws_db_instance",
            {"instance_class": "db.r6g.large", "engine": "postgres", "multi_az": False})]))
    v = g.gate_command("terraform apply plan.out", cwd=str(tmp_path), record=False)
    by = {p["usage_type"]: p for p in v["priced"]}
    assert by["BoxUsage:m5.4xlarge"]["factor"] is None
    assert by["InstanceUsage:db.r6g.large"]["factor"] == FACTOR
    rds = aws_prices.rds_rate("db.r6g.large", "postgres", "eu-west-1")
    ec2 = Decimal(str(round(EC2_HOURLY["m5.4xlarge"] * EC2_REGION_RANGE["eu-west-1"][0], 6)))
    hours = Decimal(str(HOURS_PER_MONTH))
    judged = Decimal(str(v["monthly_delta_usd"]))
    rds_list = Decimal(rds.hourly_usd) * hours
    want = judged - rds_list.quantize(Decimal("0.01")) + Decimal(
        str(_cents_up(rds_list * Decimal(FACTOR))))
    assert v["effective"]["monthly_usd"] == float(want.quantize(Decimal("0.01")))
    assert v["effective"]["monthly_usd"] >= float(ec2 * hours)
    assert "The rest is at list price" in v["reason"] and "floor" in v["reason"]


CASES = [CACHE_EU, RDS_EU, RDS_EU + " --multi-az",
         "aws ec2 run-instances --region us-east-1 --instance-type m5.4xlarge --count 2",
         "aws ec2 run-instances --region us-west-2 --instance-type m5.4xlarge --count 2",
         "aws ec2 run-instances --region eu-west-1 --instance-type m5.4xlarge --count 2",
         CACHE_EU.replace("eu-west-1", "af-south-1"),
         CACHE_EU.replace("--region eu-west-1 ", "")]


@pytest.mark.parametrize("cmd", CASES)
def test_every_skus_effective_figure_is_absent_or_its_exact_list_times_the_factor(cmd):
    """Rule 15, over every SKU shape: a factor only on an exact list, and the
    figure it gives is that list times the factor, rounded up, never below."""
    keep_rates([_row(p, u, r) for p, u in (("AmazonEC2", "BoxUsage:m5.4xlarge"),
                                           ("AmazonRDS", "InstanceUsage:db.r6g.large"),
                                           ("AmazonRDS", "Multi-AZUsage:db.r6g.large"),
                                           ("AmazonElastiCache", "NodeUsage:cache.r6g.large"))
                for r in ("us-east-1", "us-west-2", "eu-west-1", "af-south-1")])
    est = g.estimate_command_monthly_cost(cmd)
    [sku] = g._skus_of(est)
    got = cloud_rates.effective([sku], judged_monthly=est["monthly_usd"])
    exact = (Decimal(str(sku["usd_hour"])) * Decimal(str(sku["quantity"]))
             * Decimal(str(sku["hours"])) * Decimal(FACTOR))
    if sku["list_exact"] and sku["region"]:
        assert got["price_basis"] == "effective"
        assert got["effective"]["monthly_usd"] == _cents_up(exact)
    else:
        assert got["price_basis"] == "list" and got["effective"] is None
        assert "Priced at list price" in got["note"]


def test_us_west_2_lists_ec2_at_exactly_us_east_1_so_its_rate_is_exact():
    est = g.estimate_command_monthly_cost(CASES[4])
    assert est["sku"]["list_exact"] is True and est["sku"]["region"] == "us-west-2"
