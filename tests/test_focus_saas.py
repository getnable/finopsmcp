"""FOCUS normalization for usage-based (non-cloud) providers.

The clouds have bespoke translators. Every usage-based SaaS provider shares one
generic translator (focus.translators.generic.saas_focus_records) that maps the
uniform CostEntry shape into FOCUS 2.0 records. This is the long-tail engine that
turns nable's connector fan-out into a single normalized cost dataset.

Covers: field mapping, ServiceCategory clamping, credits/usage preserved as Tags
(no fabricated dollars), and empty input.
"""
from datetime import date, datetime

from finops.connectors.base import CostEntry, CostSummary
from finops.focus.schema import FocusRecord, CHARGE_CATEGORIES, SERVICE_CATEGORIES
from finops.focus.translators.generic import saas_focus_records

_START = date(2026, 6, 1)
_END = date(2026, 6, 30)


def _summary(provider: str, entries: list[CostEntry]) -> CostSummary:
    return CostSummary(
        provider=provider,
        start_date=_START,
        end_date=_END,
        total_usd=sum(e.amount for e in entries),
        by_service={},
        by_account={},
        by_region={},
        entries=entries,
    )


def test_snowflake_maps_to_focus():
    e = CostEntry(
        provider="snowflake",
        account_id="xy12345",
        account_name="xy12345",
        service="Warehouse: ANALYTICS_WH",
        region="",
        amount=480.0,
        metadata={"credits_consumed": 120.0},
    )
    recs = saas_focus_records(
        _summary("snowflake", [e]),
        provider="Snowflake", publisher="Snowflake", category="Database",
        start_date=_START, end_date=_END, resource_type="Warehouse",
    )
    assert len(recs) == 1
    r = recs[0]
    assert isinstance(r, FocusRecord)
    assert r.ProviderName == "Snowflake" and r.PublisherName == "Snowflake"
    assert r.ServiceCategory in SERVICE_CATEGORIES and r.ServiceCategory == "Database"
    assert r.ChargeCategory in CHARGE_CATEGORIES and r.ChargeCategory == "Usage"
    assert r.ResourceType == "Warehouse"
    assert r.BilledCost == 480.0 and r.EffectiveCost == 480.0
    assert r.SubAccountId == "xy12345"
    # Credits ride along in Tags so nothing is lost on normalization.
    assert r.Tags.get("credits_consumed") == "120.0"
    assert isinstance(r.ChargePeriodStart, datetime) and r.ChargePeriodStart.tzinfo is not None


def test_credits_only_no_fabricated_dollars():
    # With no contract price the amount is 0, but usage is still recorded honestly.
    e = CostEntry(
        provider="snowflake", account_id="xy12345", account_name="xy12345",
        service="Warehouse: WH", region="", amount=0.0,
        metadata={"credits_consumed": 88.0, "cost_source": "not_available"},
    )
    r = saas_focus_records(
        _summary("snowflake", [e]),
        provider="Snowflake", publisher="Snowflake", category="Database",
        start_date=_START, end_date=_END,
    )[0]
    assert r.BilledCost == 0.0
    assert r.Tags.get("credits_consumed") == "88.0"


def test_unknown_category_clamps_to_other():
    e = CostEntry(
        provider="datadog", account_id="acme", account_name="acme",
        service="infra_hosts", region="", amount=99.0,
    )
    r = saas_focus_records(
        _summary("datadog", [e]),
        provider="Datadog", publisher="Datadog", category="Observability",
        start_date=_START, end_date=_END,
    )[0]
    assert r.ServiceCategory == "Other"
    assert r.ProviderName == "Datadog"


def test_new_relic_zero_cost_keeps_usage_tags():
    e = CostEntry(
        provider="new_relic", account_id="123", account_name="123",
        service="Data Ingest", region="", amount=0.0,
        metadata={"gb_ingested": 512.5},
    )
    r = saas_focus_records(
        _summary("new_relic", [e]),
        provider="New Relic", publisher="New Relic", category="Other",
        start_date=_START, end_date=_END,
    )[0]
    assert r.BilledCost == 0.0
    assert r.Tags.get("gb_ingested") == "512.5"


def test_non_finite_amounts_clamped_to_zero():
    # NaN/inf from a provider API must not poison totals or JSON serialization.
    entries = [
        CostEntry("p", "a", "a", "nan-svc", "", float("nan")),
        CostEntry("p", "a", "a", "inf-svc", "", float("inf")),
        CostEntry("p", "a", "a", "ok-svc", "", 12.5),
    ]
    recs = saas_focus_records(
        _summary("p", entries),
        provider="P", publisher="P", category="Other",
        start_date=_START, end_date=_END,
    )
    costs = {r.ServiceName: r.BilledCost for r in recs}
    assert costs == {"nan-svc": 0.0, "inf-svc": 0.0, "ok-svc": 12.5}


def test_empty_summary_yields_no_records():
    recs = saas_focus_records(
        _summary("mongodb_atlas", []),
        provider="MongoDB Atlas", publisher="MongoDB", category="Database",
        start_date=_START, end_date=_END,
    )
    assert recs == []
