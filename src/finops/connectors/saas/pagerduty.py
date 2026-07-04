from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx

from ..base import BaseConnector, CostEntry, CostSummary


class PagerDutyConnector(BaseConnector):
    """
    PagerDuty does not expose invoice or billing data via its REST API.
    This connector reports active seat count only — no dollar amounts.
    Use this alongside your contract rate to estimate cost, or check
    your PagerDuty invoice directly.
    """
    provider = "pagerduty"
    _API = "https://api.pagerduty.com"

    def __init__(self) -> None:
        self._api_key = os.getenv("PAGERDUTY_API_KEY", "")

    async def is_configured(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Token token={self._api_key}",
            "Accept": "application/vnd.pagerduty+json;version=2",
        }

    async def get_costs(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
        group_by: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> CostSummary:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self._API}/users",
                headers=self._headers(),
                params={"limit": 1},
            )
            r.raise_for_status()
            total_users = r.json().get("total", 0)

        return CostSummary(
            provider="pagerduty",
            start_date=start_date,
            end_date=end_date,
            total_usd=0.0,
            by_service={"User Seats": 0.0},
            by_account={"pagerduty": 0.0},
            by_region={},
            entries=[CostEntry(
                provider="pagerduty",
                account_id="pagerduty",
                account_name="PagerDuty",
                service="User Seats",
                region="",
                amount=0.0,
                metadata={
                    "active_users": total_users,
                    "note": "PagerDuty has no billing API. USD cost = active_users × your contract rate. Check pagerduty.com/billing for invoice.",
                },
            )],
        )

    async def get_costs_as_focus(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
    ) -> list:
        """Return PagerDuty cost as FOCUS 1.2 records (per-seat incident-ops subscription)."""
        from ...focus.translators.generic import saas_focus_records

        summary = await self.get_costs(start_date, end_date, granularity=granularity)
        return saas_focus_records(
            summary,
            provider="PagerDuty",
            publisher="PagerDuty",
            category="Observability",
            start_date=start_date,
            end_date=end_date,
        )

    async def list_accounts(self) -> list[dict[str, str]]:
        return [{"id": "pagerduty", "name": "PagerDuty"}]
