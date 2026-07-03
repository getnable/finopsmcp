"""FOCUS normalization: Snowflake, the first usage-based (non-cloud) provider.

Proves the long-tail translator pattern: a Snowflake warehouse cost row maps to a
FOCUS 2.0 record (Database usage, credits recorded in the description, no fabricated
dollars), and the normalize() dispatcher knows the new provider.
"""
from datetime import datetime

from finops.focus import normalize
from finops.focus.schema import FocusRecord, CHARGE_CATEGORIES, SERVICE_CATEGORIES


def _row(**over):
    row = {
        "billed_cost": 480.0,
        "service_name": "Warehouse: ANALYTICS_WH",
        "resource_id": "Warehouse: ANALYTICS_WH",
        "resource_name": "Warehouse: ANALYTICS_WH",
        "credits": 120.0,
        "account": "xy12345",
        "region": "",
        "charge_category": "Usage",
        "service_category": "Database",
        "billing_period_start": "2026-06-01T00:00:00+00:00",
        "billing_period_end": "2026-06-30T00:00:00+00:00",
        "charge_period_start": "2026-06-01T00:00:00+00:00",
        "charge_period_end": "2026-06-30T00:00:00+00:00",
        "tags": {},
    }
    row.update(over)
    return row


def test_snowflake_maps_to_focus():
    rec = normalize("snowflake", _row())
    assert isinstance(rec, FocusRecord)
    assert rec.ProviderName == "Snowflake"
    assert rec.PublisherName == "Snowflake"
    assert rec.ServiceCategory in SERVICE_CATEGORIES and rec.ServiceCategory == "Database"
    assert rec.ChargeCategory in CHARGE_CATEGORIES and rec.ChargeCategory == "Usage"
    assert rec.BilledCost == 480.0 and rec.EffectiveCost == 480.0
    assert rec.SubAccountId == "xy12345"
    assert "120 credits" in (rec.ChargeDescription or "")
    assert isinstance(rec.ChargePeriodStart, datetime) and rec.ChargePeriodStart.tzinfo is not None


def test_snowflake_credits_only_no_fabricated_dollars():
    # With no contract price the amount is 0, but credits are still recorded honestly.
    rec = normalize("snowflake", _row(billed_cost=0.0, credits=88.0))
    assert rec.BilledCost == 0.0
    assert "88 credits" in (rec.ChargeDescription or "")


def test_snowflake_registered_case_insensitive():
    rec = normalize("SNOWFLAKE", _row())
    assert rec.ProviderName == "Snowflake"
