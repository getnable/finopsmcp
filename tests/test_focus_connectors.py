"""Every FOCUS-capable SaaS connector wires get_costs_as_focus to the generic
translator with a valid ServiceCategory and the right import path.

Stubs get_costs (no network) and asserts the connector emits FOCUS 1.2 records
with the expected ProviderName and a schema-valid ServiceCategory. This catches
category typos and relative-import mistakes (saas/ needs ...focus, the top-level
databricks connector needs ..focus).
"""
import asyncio
from datetime import date

import pytest

from finops.connectors.base import CostEntry, CostSummary
from finops.connectors.saas.snowflake import SnowflakeConnector
from finops.connectors.saas.datadog import DatadogConnector
from finops.connectors.saas.mongodb_atlas import MongoDBAtlasConnector
from finops.connectors.saas.new_relic import NewRelicConnector
from finops.connectors.saas.vercel import VercelConnector
from finops.connectors.saas.langfuse import LangfuseConnector
from finops.connectors.saas.cloudflare import CloudflareConnector
from finops.connectors.saas.pagerduty import PagerDutyConnector
from finops.connectors.saas.github import GitHubConnector
from finops.connectors.saas.twilio import TwilioConnector
from finops.connectors.databricks import DatabricksConnector
from finops.focus.schema import FocusRecord, SERVICE_CATEGORIES

_START = date(2026, 6, 1)
_END = date(2026, 6, 30)

# (connector class, expected FOCUS ProviderName)
_CASES = [
    (SnowflakeConnector, "Snowflake"),
    (DatadogConnector, "Datadog"),
    (MongoDBAtlasConnector, "MongoDB Atlas"),
    (NewRelicConnector, "New Relic"),
    (VercelConnector, "Vercel"),
    (LangfuseConnector, "Langfuse"),
    (CloudflareConnector, "Cloudflare"),
    (PagerDutyConnector, "PagerDuty"),
    (GitHubConnector, "GitHub"),
    (TwilioConnector, "Twilio"),
    (DatabricksConnector, "Databricks"),
]


def _canned(provider: str) -> CostSummary:
    entries = [
        CostEntry(
            provider=provider, account_id="acct", account_name="acct",
            service="line-item-a", region="us-east-1", amount=42.0,
            metadata={"usage_qty": 7},
        ),
        CostEntry(
            provider=provider, account_id="acct", account_name="acct",
            service="line-item-b", region="", amount=0.0, metadata={},
        ),
    ]
    return CostSummary(
        provider=provider, start_date=_START, end_date=_END, total_usd=42.0,
        by_service={}, by_account={}, by_region={}, entries=entries,
    )


@pytest.mark.parametrize("cls,expected_provider", _CASES)
def test_connector_get_costs_as_focus(cls, expected_provider, monkeypatch):
    conn = cls()

    async def _stub(start_date, end_date, granularity="MONTHLY", **kw):
        return _canned(conn.provider)

    monkeypatch.setattr(conn, "get_costs", _stub)
    recs = asyncio.run(conn.get_costs_as_focus(_START, _END))

    assert len(recs) == 2
    assert all(isinstance(r, FocusRecord) for r in recs)
    assert all(r.ProviderName == expected_provider for r in recs)
    assert all(r.ServiceCategory in SERVICE_CATEGORIES for r in recs)
    # First line item carries its dollar amount and usage tag; second is honest 0.
    assert recs[0].BilledCost == 42.0
    assert recs[0].Tags.get("usage_qty") == "7"
    assert recs[1].BilledCost == 0.0
