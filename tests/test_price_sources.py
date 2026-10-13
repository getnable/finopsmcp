"""Every embedded price table says where it was read and when.

A price with no source cannot be re-checked, and one with no date cannot be
known to be stale. The 2026-10-13 audit compared every table below with its
primary source (the AWS Price List offer files, the Anthropic pricing page, the
Google Cloud pricing pages) and found rows that had been typed in from memory:
MSK brokers a third under list, ElastiCache Graviton nodes 14% under, a Redshift
node type that does not exist, a Compute Savings Plan discount eight points
over. These tests hold every table to carrying its source, so the next such row
has to say where it came from.
"""
from __future__ import annotations

import datetime as dt
import importlib

import pytest

from finops import aws_prices

_TODAY = dt.datetime.now(dt.UTC).date()


def _is_price_value(value: object) -> bool:
    """A number, a dict of numbers, or a tuple of (ceiling, rate) rows."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, dict) and value:
        return all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for v in value.values())
    if isinstance(value, tuple) and value:
        return all(isinstance(row, tuple) for row in value)
    return False


def _check_entry(name: str, entry: dict) -> None:
    assert entry.get("source", "").strip(), f"{name} has no source"
    as_of = dt.date.fromisoformat(entry["as_of"])
    assert as_of <= _TODAY, f"{name} is dated in the future: {as_of}"
    assert entry.get("verified", "yes") in ("yes", "no"), name


def test_every_priced_constant_in_aws_prices_has_a_source():
    priced = {name for name, value in vars(aws_prices).items()
              if name.isupper() and not name.startswith("_") and _is_price_value(value)}
    missing = priced - set(aws_prices.PRICE_SOURCES) - set(aws_prices.DERIVED)
    assert not missing, f"priced constants with no PRICE_SOURCES entry: {sorted(missing)}"


def test_every_aws_prices_source_names_a_real_constant_with_a_date():
    for name, entry in aws_prices.PRICE_SOURCES.items():
        assert hasattr(aws_prices, name), f"PRICE_SOURCES names {name}, which does not exist"
        _check_entry(name, entry)
    assert dt.date.fromisoformat(aws_prices.AS_OF) <= _TODAY


def test_derived_constants_are_not_also_sourced():
    assert not set(aws_prices.DERIVED) & set(aws_prices.PRICE_SOURCES)
    for name in aws_prices.DERIVED:
        assert hasattr(aws_prices, name), name


# Tables outside aws_prices, each with its module's PRICE_SOURCES entry.
_OTHER_TABLES: list[tuple[str, str]] = [
]


@pytest.mark.parametrize("module,table", _OTHER_TABLES)
def test_tables_outside_aws_prices_carry_a_source(module, table):
    mod = importlib.import_module(module)
    assert hasattr(mod, table), f"{module}.{table} is gone; drop it from this list"
    sources = getattr(mod, "PRICE_SOURCES", None)
    assert isinstance(sources, dict), f"{module} has no PRICE_SOURCES"
    assert table in sources, f"{module}.{table} has no PRICE_SOURCES entry"
    _check_entry(f"{module}.{table}", sources[table])


@pytest.mark.parametrize("module", sorted({m for m, _ in _OTHER_TABLES}))
def test_no_source_entry_outlives_its_table(module):
    mod = importlib.import_module(module)
    for name in mod.PRICE_SOURCES:
        assert hasattr(mod, name), f"{module}.PRICE_SOURCES names {name}, which does not exist"
