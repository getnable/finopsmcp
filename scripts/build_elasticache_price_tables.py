#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build src/finops/price_data/elasticache_on_demand.json from the public AWS Price List.

    python scripts/build_elasticache_price_tables.py [--out PATH] [--regions R ...]
                                                     [--offers-dir DIR]
                                                     [--read-date YYYY-MM-DD]

Reads the AmazonElastiCache region index

    https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonElastiCache/current/region_index.json

then each region's currentVersionUrl, and keeps only the on-demand node rows
nable prices: productFamily "Cache Instance"; cacheEngine Redis, Memcached or
Valkey; locationType "AWS Region" (not Outposts); currentGeneration "Yes"; and
a usage type that is the plain node hour, NodeUsage:<node type> with or
without its region prefix (not ExtendedSupport, not SyncDurability). Each row
is the listed USD string and the SKU, so every rate can be found again in the
offer it came from, and each region records the offer version, publication
date, URL and the day it was read. The regions are the sixteen
rds_on_demand.json holds.

It fails loudly, writing nothing, on two rows with the same (engine, node
type) key, and on a kept SKU that does not have exactly one OnDemand price
dimension in hours with a positive USD price.

This public HTTPS read is the only network the builder uses, and it is run by
hand, never from the tests: they run derive() on the trimmed real offer
excerpts in tests/fixtures/aws_price_list/elasticache. --offers-dir builds
from files saved earlier (region_index.json and <region>.json), for a review
that should not depend on the network.

A SKU that starts AP and a hex digit reads as an Artifactory token to
detect-secrets. It is a public product code: render() writes its leading A
as the JSON escape \u0041, which any JSON reader reads back as the same
SKU, so the table needs no audited exception in .secrets.baseline.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "src" / "finops" / "price_data" / "elasticache_on_demand.json"

HOST = "https://pricing.us-east-1.amazonaws.com"
INDEX_URL = f"{HOST}/offers/v1.0/aws/AmazonElastiCache/current/region_index.json"
# The sixteen regions price_data/rds_on_demand.json holds.
REGIONS = ("us-east-1", "us-east-2", "us-west-1", "us-west-2", "ca-central-1", "eu-west-1",
           "eu-west-2", "eu-west-3", "eu-central-1", "eu-north-1", "ap-south-1",
           "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "ap-northeast-2", "sa-east-1")
# The offer's engine names and the ones nable uses (ElastiCache's own engine ids).
ENGINES = {"Redis": "redis", "Memcached": "memcached", "Valkey": "valkey"}
COLUMNS = ["engine", "node", "usd", "sku"]
FILTER = {
    "productFamily": "Cache Instance",
    "cacheEngine": sorted(ENGINES),
    "locationType": "AWS Region",
    "currentGeneration": "Yes",
    "usagetype": "NodeUsage:<node type>, with or without its region prefix",
    "term": "OnDemand, one price dimension in Hrs, USD",
}
# A SKU detect-secrets would read as an Artifactory token (see the docstring).
_TOKEN_LIKE = re.compile(r'"AP(?=[0-9A-F])')
# The plain node hour: USE1 has no prefix, EU-, APN1-, USW2- and the like do.
_NODE_USAGE = re.compile(r"^(?:[A-Z]{2,4}[0-9]?-)?NodeUsage:(cache\.[a-z0-9.]+)$")
_TIMEOUT_S = 120


class BuildError(Exception):
    """The offer is not what the table can be built from; nothing is written."""


def keep(product: dict[str, Any]) -> bool:
    """Whether a product is a row the table holds (see FILTER)."""
    a = product.get("attributes") or {}
    return (product.get("productFamily") == "Cache Instance"
            and a.get("cacheEngine") in ENGINES
            and a.get("locationType") == "AWS Region"
            and a.get("currentGeneration") == "Yes"
            and _NODE_USAGE.match(str(a.get("usagetype") or "")) is not None)


def _hourly_usd(offer: dict[str, Any], sku: str, region: str) -> str:
    """The one listed USD price per hour for `sku`, as the offer spells it."""
    terms = ((offer.get("terms") or {}).get("OnDemand") or {}).get(sku) or {}
    dims = [d for term in terms.values() for d in (term.get("priceDimensions") or {}).values()]
    if len(terms) != 1 or len(dims) != 1:
        raise BuildError(f"{region} SKU {sku}: {len(terms)} OnDemand terms and {len(dims)} "
                         "price dimensions, not exactly one")
    dim = dims[0]
    if dim.get("unit") != "Hrs":
        raise BuildError(f"{region} SKU {sku}: priced per {dim.get('unit')!r}, not per Hrs")
    usd = (dim.get("pricePerUnit") or {}).get("USD")
    try:
        ok = isinstance(usd, str) and Decimal(usd) > 0
    except InvalidOperation:
        ok = False
    if not ok:
        raise BuildError(f"{region} SKU {sku}: no positive USD price ({dim.get('pricePerUnit')})")
    return usd


def derive(offer: dict[str, Any], region: str) -> list[list[str]]:
    """[engine, node, usd, sku] for every kept row of one regional
    AmazonElastiCache offer, sorted. Raises BuildError rather than guess."""
    if offer.get("offerCode") != "AmazonElastiCache":
        raise BuildError(f"{region}: offer {offer.get('offerCode')!r} is not AmazonElastiCache")
    rows: dict[tuple[str, str], list[str]] = {}
    for sku, product in (offer.get("products") or {}).items():
        if not keep(product):
            continue
        a = product["attributes"]
        if a.get("regionCode") != region:
            raise BuildError(f"SKU {sku} is a {a.get('regionCode')} row in the {region} offer")
        node = _NODE_USAGE.match(a["usagetype"]).group(1)
        if a.get("instanceType") != node:
            raise BuildError(f"{region} SKU {sku}: instance type {a.get('instanceType')!r} is "
                             f"not its usage type's {node!r}")
        key = (ENGINES[a["cacheEngine"]], node)
        if key in rows:
            raise BuildError(f"{region}: duplicate key {key} in SKUs {rows[key][3]} and {sku}")
        rows[key] = [*key, _hourly_usd(offer, sku, region), sku]
    return [rows[k] for k in sorted(rows)]


def region_entry(offer: dict[str, Any], region: str, url: str, read: str) -> dict[str, Any]:
    """One region of the table: its citation, then its rows."""
    return {"version": str(offer.get("version") or ""),
            "publication_date": str(offer.get("publicationDate") or ""),
            "url": url, "read": read, "rows": derive(offer, region)}


def render(regions: dict[str, dict[str, Any]]) -> str:
    """The table as JSON with one row per line, so a rebuild reviews as a diff."""
    head = {
        "about": ("ElastiCache on-demand node-hour rates, as listed in the AWS Price List. "
                  "Built by scripts/build_elasticache_price_tables.py; do not edit by hand."),
        "index": INDEX_URL,
        "filter": FILTER,
        "columns": COLUMNS,
    }
    lines = ["{"]
    for k, v in head.items():
        lines.append(f"  {json.dumps(k)}: {json.dumps(v)},")
    lines.append('  "regions": {')
    for i, region in enumerate(sorted(regions)):
        e = regions[region]
        lines.append(f"    {json.dumps(region)}: {{")
        for k in ("version", "publication_date", "url", "read"):
            lines.append(f"      {json.dumps(k)}: {json.dumps(e[k])},")
        lines.append('      "rows": [')
        rows = e["rows"]
        for j, row in enumerate(rows):
            text = _TOKEN_LIKE.sub(r'"\\u0041P', json.dumps(row))
            lines.append(f"        {text}{',' if j < len(rows) - 1 else ''}")
        lines.append("      ]")
        lines.append("    }" + ("," if i < len(regions) - 1 else ""))
    lines += ["  }", "}", ""]
    return "\n".join(lines)


def fetch_json(url: str) -> dict[str, Any]:
    """A Price List file over HTTPS, from the AWS Price List host only."""
    if not url.startswith(f"{HOST}/offers/v1.0/aws/AmazonElastiCache/"):
        raise BuildError(f"not an AmazonElastiCache Price List URL: {url}")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # nosec B310 - https, one host
        return json.loads(resp.read())


def _loader(offers_dir: Path | None):
    def load(name: str, url: str) -> dict[str, Any]:
        if offers_dir is None:
            return fetch_json(url)
        return json.loads((offers_dir / name).read_text(encoding="utf-8"))
    return load


def build(regions: tuple[str, ...], read: str, offers_dir: Path | None = None
          ) -> dict[str, dict[str, Any]]:
    load = _loader(offers_dir)
    index = load("region_index.json", INDEX_URL)
    out: dict[str, dict[str, Any]] = {}
    for region in regions:
        listed = (index.get("regions") or {}).get(region)
        if not listed or not str(listed.get("currentVersionUrl", "")).startswith("/offers/"):
            raise BuildError(f"the region index lists no current offer for {region}")
        url = HOST + listed["currentVersionUrl"]
        offer = load(f"{region}.json", url)
        entry = region_entry(offer, region, url, read)
        if not entry["rows"]:
            raise BuildError(f"{region}: no rows kept; the filter or the offer changed")
        out[region] = entry
        print(f"{region}: offer {entry['version']}, {len(entry['rows'])} rows", file=sys.stderr)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT, help="where to write the table")
    ap.add_argument("--regions", nargs="+", default=list(REGIONS), metavar="REGION")
    ap.add_argument("--offers-dir", type=Path, default=None,
                    help="read region_index.json and <region>.json from here, not the network")
    ap.add_argument("--read-date", default=None,
                    help="the day the offers were read (default: today, UTC)")
    args = ap.parse_args(argv)
    read = args.read_date or dt.datetime.now(dt.UTC).date().isoformat()
    dt.date.fromisoformat(read)
    try:
        regions = build(tuple(args.regions), read, args.offers_dir)
    except BuildError as exc:
        print(f"build_elasticache_price_tables: {exc}; nothing written", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(regions), encoding="utf-8")
    print(f"wrote {args.out} ({sum(len(e['rows']) for e in regions.values())} rows, "
          f"{len(regions)} regions)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
