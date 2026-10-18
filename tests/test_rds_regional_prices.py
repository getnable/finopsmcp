# SPDX-License-Identifier: Apache-2.0
"""Regional and Multi-AZ RDS on-demand rates, each one a cited Price List row.

Before this, nable held RDS rates for us-east-1 Single-AZ only, and priced
Multi-AZ as twice the Single-AZ hour. The offer lists Multi-AZ as its own row,
and it is not always twice: in us-east-1 PostgreSQL db.r5d.xlarge is $0.571
Single-AZ and $1.143 Multi-AZ. Outside us-east-1 there was no RDS rate at all.

What has to stay true:
  - rds_rate() answers from src/finops/price_data/rds_on_demand.json, which
    scripts/build_rds_price_tables.py writes from the AWS Price List: every
    rate is the listed USD string with its SKU, region offer and version
  - the pinned rows below read exactly as AmazonRDS offer 20261006224050
    lists them, Multi-AZ included, and Multi-AZ is the listed row, never a
    doubled Single-AZ hour
  - every us-east-1 Single-AZ row RDS_HOURLY and RDS_HOURLY_POSTGRES hold
    agrees with it, so the two sources cannot drift apart unnoticed
  - Aurora, Oracle, SQL Server, a region or a class the table does not hold
    get None: no figure, never a borrowed one
  - the build script's derivation, run on the trimmed real offer excerpts in
    tests/fixtures/aws_price_list/rds, gives the shipped rows exactly, and it
    fails loudly on a duplicate key or a SKU without exactly one hourly
    OnDemand price
  - only the opt-in live re-check reads the network
"""
from __future__ import annotations

import copy
import dataclasses
import importlib.util
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from finops import aws_prices
from finops.aws_prices import RDS_HOURLY, RDS_HOURLY_POSTGRES

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "aws_price_list" / "rds"
SHIPPED = ROOT / "src" / "finops" / "price_data" / "rds_on_demand.json"
VERSION = "20261006224050"
REGIONS = ("us-east-1", "us-east-2", "us-west-1", "us-west-2", "ca-central-1", "eu-west-1",
           "eu-west-2", "eu-west-3", "eu-central-1", "eu-north-1", "ap-south-1",
           "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "ap-northeast-2", "sa-east-1")
LIVE_ENV = "FINOPS_LIVE_PRICE_LIST"


def _builder():
    spec = importlib.util.spec_from_file_location(
        "build_rds_price_tables", ROOT / "scripts" / "build_rds_price_tables.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _shipped() -> dict:
    return json.loads(SHIPPED.read_text(encoding="utf-8"))


def _fixture(region: str) -> dict:
    return json.loads((FIXTURES / f"{region}.json").read_text(encoding="utf-8"))


# ── the pinned rows ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("cls,engine,multi_az,usd,sku", [
    ("db.m5.large", "mysql", False, "0.189", "3QX5CT7T6GM9PH8R"),
    ("db.m5.large", "mysql", True, "0.378", "CWX8VJJC7RHPGPN8"),
    ("db.r6g.xlarge", "postgres", True, "1.007", "TBNX4UCY7PQVP8S3"),
    ("db.t4g.micro", "mysql", True, "0.035", "8SG8VRDN66R8UTE9"),
])
def test_eu_west_1_rows_read_as_the_offer_lists_them(cls, engine, multi_az, usd, sku):
    rate = aws_prices.rds_rate(cls, engine, "eu-west-1", multi_az=multi_az)
    assert rate is not None
    assert Decimal(rate.hourly_usd) == Decimal(usd)
    assert (rate.region, rate.engine, rate.sku) == ("eu-west-1", engine, sku)
    assert rate.deployment == ("Multi-AZ" if multi_az else "Single-AZ")
    assert rate.source == f"AWS Price List, AmazonRDS eu-west-1 offer, version {VERSION}, SKU {sku}"
    assert rate.as_of == _shipped()["regions"]["eu-west-1"]["read"]


def test_the_rate_is_a_frozen_record_with_the_agreed_fields():
    """Workstream A feature-detects this API in the enterprise planner: the
    name and the fields are a contract."""
    names = [f.name for f in dataclasses.fields(aws_prices.RdsRate)]
    assert names == ["hourly_usd", "region", "engine", "deployment", "sku", "source", "as_of"]
    rate = aws_prices.rds_rate("db.m5.large", "mysql", "eu-west-1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        rate.hourly_usd = "0"  # type: ignore[misc]
    assert isinstance(rate.hourly_usd, str)


def test_mariadb_and_postgres_are_their_own_rows():
    mysql = aws_prices.rds_rate("db.m5.large", "mysql", "eu-west-1")
    maria = aws_prices.rds_rate("db.m5.large", "mariadb", "eu-west-1")
    pg = aws_prices.rds_rate("db.m5.large", "postgres", "eu-west-1")
    assert len({mysql.sku, maria.sku, pg.sku}) == 3
    assert Decimal(pg.hourly_usd) > Decimal(mysql.hourly_usd)


@pytest.mark.parametrize("cls,engine,region", [
    ("db.r6g.large", "aurora-mysql", "us-east-1"),
    ("db.r6g.large", "aurora-postgresql", "us-east-1"),
    ("db.m5.large", "oracle-ee", "us-east-1"),
    ("db.m5.large", "oracle-se2", "eu-west-1"),
    ("db.m5.large", "sqlserver-se", "us-east-1"),
    ("db.m5.large", "mysql", "eu-west-9"),
    ("db.m5.large", "mysql", "af-south-1"),
    ("db.m5.huge", "mysql", "us-east-1"),
    ("db.m4.large", "mysql", "eu-west-1"),
    ("", "mysql", "us-east-1"),
])
def test_what_the_table_does_not_hold_is_none(cls, engine, region):
    assert aws_prices.rds_rate(cls, engine, region) is None
    assert aws_prices.rds_rate(cls, engine, region, multi_az=True) is None


@pytest.mark.parametrize("bad", [None, 3, b"mysql"])
def test_input_that_is_not_text_is_none(bad):
    assert aws_prices.rds_rate(bad, "mysql", "us-east-1") is None
    assert aws_prices.rds_rate("db.m5.large", bad, "us-east-1") is None
    assert aws_prices.rds_rate("db.m5.large", "mysql", bad) is None


def test_engine_and_region_are_read_without_regard_to_case():
    a = aws_prices.rds_rate("db.m5.large", "MySQL", "EU-WEST-1")
    assert a == aws_prices.rds_rate("db.m5.large", "mysql", "eu-west-1")


# ── agreement with the us-east-1 tables ───────────────────────────────────────

def test_every_us_east_1_single_az_row_agrees_with_the_tables():
    off = []
    for engine, table in (("mysql", RDS_HOURLY), ("mariadb", RDS_HOURLY),
                          ("postgres", RDS_HOURLY_POSTGRES)):
        for cls, hourly in table.items():
            rate = aws_prices.rds_rate(cls, engine, "us-east-1")
            if rate is None or Decimal(rate.hourly_usd) != Decimal(str(hourly)):
                off.append((engine, cls, hourly, rate and rate.hourly_usd))
            elif aws_prices.rds_hourly(cls, engine) != hourly:
                off.append((engine, cls, "rds_hourly changed"))
    assert not off, off


def test_multi_az_is_the_listed_row_never_twice_single_az():
    single = aws_prices.rds_rate("db.r5d.xlarge", "postgres", "us-east-1")
    multi = aws_prices.rds_rate("db.r5d.xlarge", "postgres", "us-east-1", multi_az=True)
    assert Decimal(single.hourly_usd) == Decimal("0.571")
    assert Decimal(multi.hourly_usd) == Decimal("1.143")
    assert Decimal(multi.hourly_usd) != 2 * Decimal(single.hourly_usd)
    assert multi.sku != single.sku and multi.deployment == "Multi-AZ"


def test_every_multi_az_row_carries_its_own_sku():
    doc = _shipped()
    doubled_everywhere = True
    for region, entry in doc["regions"].items():
        rows = {(e, d, c): (usd, sku) for e, d, c, usd, sku in entry["rows"]}
        for (e, d, c), (usd, sku) in rows.items():
            if d != "Multi-AZ" or (e, "Single-AZ", c) not in rows:
                continue
            s_usd, s_sku = rows[(e, "Single-AZ", c)]
            assert sku != s_sku, (region, e, c)
            if Decimal(usd) != 2 * Decimal(s_usd):
                doubled_everywhere = False
    # If every listed Multi-AZ row were exactly double, a doubling would be
    # indistinguishable; the offer says otherwise, and so must the table.
    assert not doubled_everywhere


# ── the shipped table ─────────────────────────────────────────────────────────

def test_the_shipped_table_covers_the_sixteen_regions_with_their_citation():
    doc = _shipped()
    assert set(doc["regions"]) == set(REGIONS)
    assert doc["columns"] == ["engine", "deployment", "class", "usd", "sku"]
    for region, entry in doc["regions"].items():
        assert entry["version"] == VERSION, region
        assert entry["publication_date"] == "2026-10-06T22:40:50Z", region
        assert entry["url"] == ("https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/"
                                f"AmazonRDS/{VERSION}/{region}/index.json")
        assert entry["read"] >= "2026-10-06", region
        keys = set()
        for engine, deployment, cls, usd, sku in entry["rows"]:
            assert engine in ("mysql", "postgres", "mariadb")
            assert deployment in ("Single-AZ", "Multi-AZ")
            assert cls.startswith("db.")
            assert Decimal(usd) > 0 and usd == usd.strip()
            assert sku.isalnum() and len(sku) == 16
            assert (engine, deployment, cls) not in keys
            keys.add((engine, deployment, cls))
        assert len(keys) > 500, region


def test_a_price_source_names_the_table_and_its_offer():
    entry = aws_prices.PRICE_SOURCES["RDS_ON_DEMAND_JSON"]
    doc = _shipped()
    assert VERSION in entry["source"] and "AmazonRDS" in entry["source"]
    assert entry["as_of"] == max(e["read"] for e in doc["regions"].values())
    assert (Path(aws_prices.__file__).parent / aws_prices.RDS_ON_DEMAND_JSON) == SHIPPED


def test_the_table_loads_on_first_use_not_on_import():
    code = ("import finops.aws_prices as a, sys; "
            "assert a._RDS_REGIONAL is None, 'read at import'; "
            "a.rds_rate('db.m5.large', 'mysql', 'us-east-1'); "
            "assert a._RDS_REGIONAL, 'not read on use'")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=60, check=False)
    assert r.returncode == 0, r.stderr


def test_an_unreadable_table_gives_no_figure(monkeypatch, tmp_path):
    monkeypatch.setattr(aws_prices, "_RDS_REGIONAL", None)
    monkeypatch.setattr(aws_prices, "RDS_ON_DEMAND_JSON", str(tmp_path / "missing.json"))
    assert aws_prices.rds_rate("db.m5.large", "mysql", "us-east-1") is None
    monkeypatch.setattr(aws_prices, "_RDS_REGIONAL", None)


# ── the build script ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("region", ["eu-west-1", "us-east-1"])
def test_the_derivation_reproduces_the_shipped_rows(region):
    build = _builder()
    offer = _fixture(region)
    rows = build.derive(offer, region)
    shipped = {r[4]: r for r in _shipped()["regions"][region]["rows"]}
    assert rows, region
    for row in rows:
        assert shipped[row[4]] == row, row
    assert offer["version"] == _shipped()["regions"][region]["version"]
    entry = build.region_entry(offer, region, "https://pricing.us-east-1.amazonaws.com"
                               f"/offers/v1.0/aws/AmazonRDS/{VERSION}/{region}/index.json",
                               "2026-10-17")
    assert entry["version"] == VERSION and entry["publication_date"] == offer["publicationDate"]


def test_the_derivation_keeps_only_what_the_filter_names():
    """The eu-west-1 excerpt carries rows the table must not hold: Multi-AZ
    with readable standbys, Aurora, Oracle BYOL, SQL Server, an Outposts row,
    a previous-generation class and a storage SKU."""
    build = _builder()
    offer = _fixture("eu-west-1")
    kept = {row[4] for row in build.derive(offer, "eu-west-1")}
    assert kept == {"3QX5CT7T6GM9PH8R", "CWX8VJJC7RHPGPN8", "TBNX4UCY7PQVP8S3",
                    "7S8ASG4BXPUZ394J", "8SG8VRDN66R8UTE9", "GTUCDXE9SAFY8AU7",
                    "HMCDZN9Q2DJZ9Q5D", "DQYXUS3RESNC7PXF"}
    dropped = set(offer["products"]) - kept
    assert len(dropped) == 7


def test_the_build_runs_from_saved_offer_files(tmp_path):
    """--offers-dir builds from files read earlier (here, the excerpts), with
    the URL each region index entry names."""
    build = _builder()
    src = tmp_path / "offers"
    src.mkdir()
    index = json.loads((FIXTURES / "region_index.json").read_text())
    index["regions"] = {r: index["regions"][r] for r in ("eu-west-1", "us-east-1")}
    (src / "region_index.json").write_text(json.dumps(index))
    for r in ("eu-west-1", "us-east-1"):
        (src / f"{r}.json").write_text((FIXTURES / f"{r}.json").read_text())
    out = tmp_path / "rds_on_demand.json"
    code = build.main(["--offers-dir", str(src), "--out", str(out), "--read-date", "2026-10-17",
                       "--regions", "eu-west-1", "us-east-1"])
    assert code == 0
    doc = json.loads(out.read_text())
    assert doc["regions"]["eu-west-1"]["url"].endswith(f"/AmazonRDS/{VERSION}/eu-west-1/index.json")
    assert doc["regions"]["eu-west-1"]["read"] == "2026-10-17"
    assert len(doc["regions"]["eu-west-1"]["rows"]) == 8


def test_a_duplicate_key_fails_loudly():
    build = _builder()
    offer = _fixture("eu-west-1")
    twin = copy.deepcopy(offer["products"]["3QX5CT7T6GM9PH8R"])
    twin["sku"] = "ZZZZZZZZZZZZZZZZ"
    offer["products"]["ZZZZZZZZZZZZZZZZ"] = twin
    offer["terms"]["OnDemand"]["ZZZZZZZZZZZZZZZZ"] = copy.deepcopy(
        offer["terms"]["OnDemand"]["3QX5CT7T6GM9PH8R"])
    with pytest.raises(build.BuildError, match="duplicate"):
        build.derive(offer, "eu-west-1")


@pytest.mark.parametrize("damage", ["two_dimensions", "two_terms", "no_term", "monthly_unit",
                                    "no_usd"])
def test_a_sku_without_exactly_one_hourly_price_fails_loudly(damage):
    build = _builder()
    offer = _fixture("eu-west-1")
    sku = "3QX5CT7T6GM9PH8R"
    terms = offer["terms"]["OnDemand"]
    [term] = terms[sku].values()
    [dim_key] = term["priceDimensions"]
    if damage == "two_dimensions":
        term["priceDimensions"]["extra"] = copy.deepcopy(term["priceDimensions"][dim_key])
    elif damage == "two_terms":
        terms[sku]["extra"] = copy.deepcopy(term)
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
                               "AmazonRDS/current/region_index.json")
    assert build.REGIONS == REGIONS
    for bad in ("http://pricing.us-east-1.amazonaws.com/x", "file:///etc/passwd",
                "https://example.com/offers/v1.0/aws/AmazonRDS/x.json"):
        with pytest.raises(build.BuildError):
            build.fetch_json(bad)


# ── the live re-check (opt in) ────────────────────────────────────────────────

@pytest.mark.skipif(os.environ.get(LIVE_ENV) != "1",
                    reason=f"reads the public AWS Price List; set {LIVE_ENV}=1 to run")
def test_live_the_shipped_table_still_matches_the_price_list():
    """Re-reads the region index and the eu-west-3 offer (the smallest of the
    sixteen). A newer version is reported, not silently accepted: rebuild with
    scripts/build_rds_price_tables.py and review the diff."""
    build = _builder()
    index = build.fetch_json(build.INDEX_URL)
    entry = _shipped()["regions"]["eu-west-3"]
    url = build.HOST + index["regions"]["eu-west-3"]["currentVersionUrl"]
    assert url == entry["url"], f"AWS publishes {url} now; rebuild the table"
    rows = build.derive(build.fetch_json(url), "eu-west-3")
    assert rows == entry["rows"]
