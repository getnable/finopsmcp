#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build src/finops/price_data/rds_on_demand.json from the public AWS Price List.

    python scripts/build_rds_price_tables.py [--out PATH] [--regions R ...]
                                             [--offers-dir DIR] [--read-date YYYY-MM-DD]

Reads the AmazonRDS region index

    https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/region_index.json

then each region's currentVersionUrl, and keeps only the on-demand rows nable
prices: productFamily "Database Instance"; databaseEngine MySQL, PostgreSQL or
MariaDB; deploymentOption exactly "Single-AZ" or "Multi-AZ" (not Multi-AZ with
readable standbys); licenseModel "No license required"; locationType "AWS
Region"; currentGeneration "Yes". Each row is the listed USD string and the
SKU, so every rate can be found again in the offer it came from, and each
region records the offer version, publication date, URL and the day it was
read.

It fails loudly, writing nothing, on two rows with the same (engine,
deployment, class) key, and on a kept SKU that does not have exactly one
OnDemand price dimension in hours with a positive USD price. A Multi-AZ rate
is the offer's own listed row; nothing here doubles a Single-AZ hour.

This public HTTPS read is the only network the builder uses, and it is run by
hand, never from the tests: they run derive() on the trimmed real offer
excerpts in tests/fixtures/aws_price_list/rds. --offers-dir builds from files
saved earlier (region_index.json and <region>.json), for a review that should
not depend on the network.

A few SKUs look like Artifactory tokens to detect-secrets (AP followed by a
hex digit). They are public product codes, recorded in .secrets.baseline as
audited false positives; after a rebuild, `detect-secrets scan --baseline
.secrets.baseline` records the new table's, to be checked the same way.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "src" / "finops" / "price_data" / "rds_on_demand.json"

HOST = "https://pricing.us-east-1.amazonaws.com"
INDEX_URL = f"{HOST}/offers/v1.0/aws/AmazonRDS/current/region_index.json"
REGIONS = ("us-east-1", "us-east-2", "us-west-1", "us-west-2", "ca-central-1", "eu-west-1",
           "eu-west-2", "eu-west-3", "eu-central-1", "eu-north-1", "ap-south-1",
           "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "ap-northeast-2", "sa-east-1")
# The offer's engine names and the ones nable uses (RDS's own engine ids).
ENGINES = {"MySQL": "mysql", "PostgreSQL": "postgres", "MariaDB": "mariadb"}
DEPLOYMENTS = ("Single-AZ", "Multi-AZ")
COLUMNS = ["engine", "deployment", "class", "usd", "sku"]
FILTER = {
    "productFamily": "Database Instance",
    "databaseEngine": sorted(ENGINES),
    "deploymentOption": list(DEPLOYMENTS),
    "licenseModel": "No license required",
    "locationType": "AWS Region",
    "currentGeneration": "Yes",
    "term": "OnDemand, one price dimension in Hrs, USD",
}
_TIMEOUT_S = 120


class BuildError(Exception):
    """The offer is not what the table can be built from; nothing is written."""


def keep(product: dict[str, Any]) -> bool:
    """Whether a product is a row the table holds (see FILTER)."""
    a = product.get("attributes") or {}
    return (product.get("productFamily") == "Database Instance"
            and a.get("databaseEngine") in ENGINES
            and a.get("deploymentOption") in DEPLOYMENTS
            and a.get("licenseModel") == "No license required"
            and a.get("locationType") == "AWS Region"
            and a.get("currentGeneration") == "Yes")


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
    """[engine, deployment, class, usd, sku] for every kept row of one
    regional AmazonRDS offer, sorted. Raises BuildError rather than guess."""
    if offer.get("offerCode") != "AmazonRDS":
        raise BuildError(f"{region}: offer {offer.get('offerCode')!r} is not AmazonRDS")
    rows: dict[tuple[str, str, str], list[str]] = {}
    for sku, product in (offer.get("products") or {}).items():
        if not keep(product):
            continue
        a = product["attributes"]
        if a.get("regionCode") != region:
            raise BuildError(f"SKU {sku} is a {a.get('regionCode')} row in the {region} offer")
        key = (ENGINES[a["databaseEngine"]], a["deploymentOption"], a.get("instanceType") or "")
        if not key[2].startswith("db."):
            raise BuildError(f"{region} SKU {sku}: instance type {key[2]!r}")
        if key in rows:
            raise BuildError(f"{region}: duplicate key {key} in SKUs {rows[key][4]} and {sku}")
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
        "about": ("RDS on-demand hourly rates, as listed in the AWS Price List. Built by "
                  "scripts/build_rds_price_tables.py; do not edit by hand."),
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
            lines.append(f"        {json.dumps(row)}{',' if j < len(rows) - 1 else ''}")
        lines.append("      ]")
        lines.append("    }" + ("," if i < len(regions) - 1 else ""))
    lines += ["  }", "}", ""]
    return "\n".join(lines)


def fetch_json(url: str) -> dict[str, Any]:
    """A Price List file over HTTPS, from the AWS Price List host only."""
    if not url.startswith(f"{HOST}/offers/v1.0/aws/AmazonRDS/"):
        raise BuildError(f"not an AmazonRDS Price List URL: {url}")
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
        print(f"build_rds_price_tables: {exc}; nothing written", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(regions), encoding="utf-8")
    print(f"wrote {args.out} ({sum(len(e['rows']) for e in regions.values())} rows, "
          f"{len(regions)} regions)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
