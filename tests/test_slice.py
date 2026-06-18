"""Tests for the slice engine (finops.slice): the moldable-views primitive."""
from __future__ import annotations

from datetime import datetime

import pytest

from finops.focus.schema import FocusRecord
from finops.slice import parse_spec, run_slice
from finops.slice.engine import derive_card
from finops.slice.spec import SliceSpec, SliceSpecError


def _rec(billed=10.0, effective=None, list_cost=None, service="Amazon EC2",
         category="Compute", provider="AWS", region="us-east-1", account="111",
         charge="Usage", commit_type=None, tags=None, day="2026-06-10",
         resource="i-0abc"):
    eff = billed if effective is None else effective
    lc = billed if list_cost is None else list_cost
    dt = datetime.fromisoformat(day + "T00:00:00")
    return FocusRecord(
        BilledCost=billed, EffectiveCost=eff, ListCost=lc,
        ResourceId=resource, ResourceName=None, ResourceType="Instance",
        ServiceName=service, ServiceCategory=category,
        ProviderName=provider, PublisherName=provider,
        RegionId=region, RegionName=region,
        BillingPeriodStart=dt, BillingPeriodEnd=dt,
        ChargePeriodStart=dt, ChargePeriodEnd=dt,
        ChargeCategory=charge, ChargeDescription=None,
        CommitmentDiscountId=("sp-1" if commit_type else None),
        CommitmentDiscountType=commit_type,
        Tags=tags or {}, SubAccountId=account, SubAccountName=None,
    )


# ── grouping ──────────────────────────────────────────────────────────────────

def test_group_by_single_dimension_sums_and_orders():
    recs = [
        _rec(billed=100, region="us-east-1"),
        _rec(billed=40, region="us-west-2"),
        _rec(billed=10, region="us-east-1"),
    ]
    out = run_slice(parse_spec({"dimensions": ["RegionId"], "metric": "BilledCost"}), recs)
    assert out.total == 150
    # ordered by metric desc
    assert [r["RegionId"] for r in out.rows] == ["us-east-1", "us-west-2"]
    assert out.rows[0]["metric"] == 110
    assert out.rows[0]["record_count"] == 2


def test_no_dimensions_is_a_kpi_total():
    recs = [_rec(billed=5), _rec(billed=7)]
    out = run_slice(parse_spec({"dimensions": [], "metric": "BilledCost"}), recs)
    assert out.total == 12
    assert len(out.rows) == 1
    assert out.rows[0]["metric"] == 12
    assert out.dimensions == []


def test_two_dimension_composite_key():
    recs = [
        _rec(billed=10, service="Amazon EC2", region="us-east-1"),
        _rec(billed=20, service="Amazon EC2", region="us-west-2"),
        _rec(billed=5, service="Amazon S3", region="us-east-1"),
    ]
    out = run_slice(parse_spec({"dimensions": ["ServiceName", "RegionId"], "metric": "BilledCost"}), recs)
    assert len(out.rows) == 3
    top = out.rows[0]
    assert top["ServiceName"] == "Amazon EC2" and top["RegionId"] == "us-west-2" and top["metric"] == 20


def test_group_by_date_daily_and_monthly():
    recs = [_rec(billed=10, day="2026-06-10"), _rec(billed=5, day="2026-06-10"), _rec(billed=8, day="2026-07-01")]
    daily = run_slice(parse_spec({"dimensions": ["date"], "granularity": "DAILY", "metric": "BilledCost", "order_by": "date"}), recs)
    assert [r["date"] for r in daily.rows] == ["2026-06-10", "2026-07-01"]
    assert daily.rows[0]["metric"] == 15
    monthly = run_slice(parse_spec({"dimensions": ["date"], "granularity": "MONTHLY", "metric": "BilledCost"}), recs)
    months = {r["date"]: r["metric"] for r in monthly.rows}
    assert months == {"2026-06": 15, "2026-07": 8}


def test_group_by_tag():
    recs = [
        _rec(billed=10, tags={"team": "data"}),
        _rec(billed=20, tags={"team": "web"}),
        _rec(billed=3, tags={}),
    ]
    out = run_slice(parse_spec({"dimensions": ["Tags[team]"], "metric": "BilledCost"}), recs)
    by = {r["Tags[team]"]: r["metric"] for r in out.rows}
    assert by == {"web": 20, "data": 10, "(untagged)": 3}


# ── filters + exclusions ───────────────────────────────────────────────────────

def test_filter_in_narrows_records():
    recs = [_rec(billed=10, service="Amazon EC2"), _rec(billed=99, service="Amazon S3")]
    out = run_slice(parse_spec({
        "dimensions": ["ServiceName"], "metric": "BilledCost",
        "filters": [{"dimension": "ServiceName", "op": "eq", "values": ["Amazon EC2"]}],
    }), recs)
    assert out.total == 10
    assert len(out.rows) == 1 and out.rows[0]["ServiceName"] == "Amazon EC2"


def test_exclude_savings_plan_credits():
    """'minus SP credits' = exclude Credit charges AND committed usage."""
    recs = [
        _rec(billed=100, charge="Usage"),
        _rec(billed=-30, charge="Credit"),
        _rec(billed=50, charge="Usage", commit_type="Savings Plan"),
    ]
    out = run_slice(parse_spec({
        "dimensions": [], "metric": "BilledCost",
        "exclusions": [
            {"dimension": "ChargeCategory", "op": "in", "values": ["Credit"]},
            {"dimension": "CommitmentDiscountType", "op": "neq", "values": ["(none)"]},
        ],
    }), recs)
    # only the plain $100 usage row survives
    assert out.total == 100
    assert out.record_count == 1


def test_filter_contains_and_regex():
    recs = [_rec(service="Amazon EC2"), _rec(service="Amazon ECS"), _rec(service="Amazon S3")]
    contains = run_slice(parse_spec({
        "dimensions": ["ServiceName"],
        "filters": [{"dimension": "ServiceName", "op": "contains", "values": ["ec"]}],
    }), recs)
    assert {r["ServiceName"] for r in contains.rows} == {"Amazon EC2", "Amazon ECS"}
    rgx = run_slice(parse_spec({
        "dimensions": ["ServiceName"],
        "filters": [{"dimension": "ServiceName", "op": "regex", "values": ["EC[0-9S]"]}],
    }), recs)
    assert {r["ServiceName"] for r in rgx.rows} == {"Amazon EC2", "Amazon ECS"}


# ── metric, order, limit ────────────────────────────────────────────────────────

def test_metric_selection_effective_vs_list():
    recs = [_rec(billed=80, effective=80, list_cost=100)]
    eff = run_slice(parse_spec({"dimensions": [], "metric": "EffectiveCost"}), recs)
    lst = run_slice(parse_spec({"dimensions": [], "metric": "ListCost"}), recs)
    assert eff.total == 80 and lst.total == 100


def test_limit_truncates_and_flags():
    recs = [_rec(billed=i, region=f"r{i}") for i in range(1, 11)]
    out = run_slice(parse_spec({"dimensions": ["RegionId"], "metric": "BilledCost", "limit": 3}), recs)
    assert len(out.rows) == 3
    assert out.truncated is True
    # still the top 3 by metric
    assert [r["metric"] for r in out.rows] == [10, 9, 8]


# ── card derivation ─────────────────────────────────────────────────────────────

def test_derive_card_templates():
    assert derive_card(SliceSpec(dimensions=[]), run_slice(SliceSpec(dimensions=[]), [])).template == "kpi"
    assert derive_card(SliceSpec(dimensions=["RegionId"]), run_slice(SliceSpec(dimensions=["RegionId"]), [])).template == "bar"
    assert derive_card(SliceSpec(dimensions=["date"]), run_slice(SliceSpec(dimensions=["date"]), [])).template == "line"
    assert derive_card(SliceSpec(dimensions=["date", "ServiceName"]), run_slice(SliceSpec(dimensions=["date", "ServiceName"]), [])).template == "stacked_bar"
    assert derive_card(SliceSpec(dimensions=["ServiceName", "RegionId"]), run_slice(SliceSpec(dimensions=["ServiceName", "RegionId"]), [])).template == "table"
    # the card carries the slice that regenerates it (for pinning)
    c = derive_card(SliceSpec(dimensions=["RegionId"], metric="BilledCost"), run_slice(SliceSpec(dimensions=["RegionId"]), []))
    assert c.slice["dimensions"] == ["RegionId"] and c.slice["metric"] == "BilledCost"


# ── validation (the agent guardrail) ────────────────────────────────────────────

def test_parse_rejects_unknown_dimension():
    with pytest.raises(SliceSpecError):
        parse_spec({"dimensions": ["NotARealColumn"]})


def test_parse_rejects_unknown_op_and_metric():
    with pytest.raises(SliceSpecError):
        parse_spec({"dimensions": ["RegionId"], "filters": [{"dimension": "RegionId", "op": "blah", "values": ["x"]}]})
    with pytest.raises(SliceSpecError):
        parse_spec({"dimensions": [], "metric": "MadeUpCost"})


def test_parse_clamps_limit_and_caps_dimensions():
    s = parse_spec({"dimensions": ["RegionId"], "limit": 99999})
    assert s.limit == 500  # MAX_LIMIT
    with pytest.raises(SliceSpecError):
        parse_spec({"dimensions": ["a", "b", "c", "d"]})  # >3 (also unknown, raises either way)


def test_parse_accepts_tag_and_date_dims():
    s = parse_spec({"dimensions": ["Tags[team]", "date"], "granularity": "monthly"})
    assert s.dimensions == ["Tags[team]", "date"]
    assert s.granularity == "MONTHLY"  # normalized upper
