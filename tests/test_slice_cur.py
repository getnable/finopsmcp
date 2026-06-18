"""Tests for the CUR pushdown of the slice engine (finops.slice.cur_engine)."""
from __future__ import annotations

from datetime import date

import pytest

from finops.slice import cur_engine, parse_spec
from finops.slice.spec import CUR_DIMENSIONS, needs_cur

SD, ED = date(2026, 5, 1), date(2026, 5, 31)


@pytest.fixture(autouse=True)
def cur_db(monkeypatch):
    # _db()/_table() read these; the bucket vars are intentionally left unset so
    # is_configured() stays False (lets us test the config gate too).
    monkeypatch.setenv("CUR_ATHENA_DATABASE", "curdb")
    monkeypatch.setenv("CUR_ATHENA_TABLE", "curtbl")
    monkeypatch.delenv("CUR_S3_BUCKET", raising=False)
    monkeypatch.delenv("CUR_ATHENA_RESULTS_BUCKET", raising=False)


# ── routing + registry ────────────────────────────────────────────────────────

def test_needs_cur():
    assert needs_cur(parse_spec({"dimensions": ["usage_type"]}))
    assert needs_cur(parse_spec({"dimensions": ["RegionId"],
                                 "filters": [{"dimension": "instance_type", "op": "eq", "values": ["m5.large"]}]}))
    assert not needs_cur(parse_spec({"dimensions": ["RegionId", "ServiceName"]}))


def test_parse_accepts_cur_dims():
    assert CUR_DIMENSIONS == {"usage_type", "instance_type", "resource_id"}
    s = parse_spec({"dimensions": ["usage_type", "resource_id"]})
    assert s.dimensions == ["usage_type", "resource_id"]


# ── SQL builder ─────────────────────────────────────────────────────────────--

def test_build_sql_basic():
    spec = parse_spec({"dimensions": ["usage_type"], "metric": "BilledCost", "limit": 10})
    sql, aliases = cur_engine.build_cur_sql(spec, SD, ED)
    assert "line_item_usage_type AS d_usage_type" in sql
    assert "SUM(line_item_unblended_cost) AS metric" in sql
    assert "FROM curdb.curtbl" in sql
    assert "GROUP BY line_item_usage_type" in sql
    assert "ORDER BY metric DESC" in sql
    assert "LIMIT 10" in sql
    assert "year='2026' AND month='05'" in sql       # partition pruning
    assert ">= DATE '2026-05-01'" in sql and "< DATE '2026-06-01'" in sql
    assert aliases == [("usage_type", "d_usage_type")]


def test_build_sql_filters_and_exclusions():
    spec = parse_spec({
        "dimensions": ["usage_type"],
        "filters": [{"dimension": "instance_type", "op": "eq", "values": ["m5.large"]}],
        "exclusions": [{"dimension": "ChargeCategory", "op": "in", "values": ["Tax"]}],
    })
    sql, _ = cur_engine.build_cur_sql(spec, SD, ED)
    assert "product_instance_type = 'm5.large'" in sql
    assert "NOT (line_item_line_item_type IN ('Tax'))" in sql


def test_sql_injection_is_escaped():
    spec = parse_spec({"dimensions": ["usage_type"],
                       "filters": [{"dimension": "usage_type", "op": "eq", "values": ["x' OR '1'='1"]}]})
    sql, _ = cur_engine.build_cur_sql(spec, SD, ED)
    # the quote is doubled -> it's one harmless string literal, not an injection
    assert "= 'x'' OR ''1''=''1'" in sql


def test_safe_literal_rejects_control_chars_and_long_values():
    with pytest.raises(ValueError):
        cur_engine._safe_literal("bad\nvalue")
    with pytest.raises(ValueError):
        cur_engine._safe_literal("x" * 300)


def test_unsupported_dimension_on_cur_raises():
    # valid in FOCUS, but no CUR column -> must raise rather than silently mis-map
    spec = parse_spec({"dimensions": ["CommitmentDiscountId"]})
    with pytest.raises(ValueError):
        cur_engine.build_cur_sql(spec, SD, ED)


def test_date_and_tag_dimensions():
    spec = parse_spec({"dimensions": ["date", "Tags[team]"], "granularity": "MONTHLY"})
    sql, aliases = cur_engine.build_cur_sql(spec, SD, ED)
    assert "date_format(line_item_usage_start_date, '%Y-%m')" in sql
    assert "resource_tags_user_team" in sql
    assert ("date", "d_date") in aliases


def test_contains_lowercases_and_wraps():
    spec = parse_spec({"dimensions": ["usage_type"],
                       "filters": [{"dimension": "usage_type", "op": "contains", "values": ["BoxUsage"]}]})
    sql, _ = cur_engine.build_cur_sql(spec, SD, ED)
    assert "LIKE '%boxusage%'" in sql


def test_run_slice_cur_requires_full_config():
    """With only db+table set (no buckets), is_configured() is False -> raises."""
    spec = parse_spec({"dimensions": ["usage_type"]})
    with pytest.raises(cur_engine.CURNotConfigured):
        cur_engine.run_slice_cur(spec, SD, ED)
