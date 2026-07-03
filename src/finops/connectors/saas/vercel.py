from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx

from ..base import BaseConnector, CostEntry, CostSummary


class VercelConnector(BaseConnector):
    """
    Returns actual invoice line items from the Vercel billing API.
    Requires a Pro or Enterprise plan with invoice API access.
    No plan-name price estimation — if the invoice API is unavailable, we return nothing.
    """
    provider = "vercel"
    _API = "https://api.vercel.com"

    def __init__(self) -> None:
        self._token = os.getenv("VERCEL_TOKEN", "")
        self._team_id = os.getenv("VERCEL_TEAM_ID", "")

    async def is_configured(self) -> bool:
        return bool(self._token)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def _params(self) -> dict:
        return {"teamId": self._team_id} if self._team_id else {}

    async def get_costs(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
        group_by: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> CostSummary:
        entries: list[CostEntry] = []
        by_service: dict[str, float] = {}
        total = 0.0

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self._API}/v1/billing/invoices",
                headers=self._headers(),
                params=self._params(),
            )

            if r.status_code == 404:
                # Invoice API not available on this plan — return nothing, not an estimate
                return CostSummary(
                    provider="vercel",
                    start_date=start_date,
                    end_date=end_date,
                    total_usd=0.0,
                    by_service={},
                    by_account={self._team_id or "personal": 0.0},
                    by_region={},
                    entries=[CostEntry(
                        provider="vercel",
                        account_id=self._team_id or "personal",
                        account_name=self._team_id or "personal",
                        service="Vercel",
                        region="",
                        amount=0.0,
                        metadata={"note": "Invoice API requires Pro/Enterprise plan. Check vercel.com/dashboard/usage for billing."},
                    )],
                )

            r.raise_for_status()
            for inv in r.json().get("invoices", []):
                period_start = (inv.get("period", {}).get("start") or "")[:10]
                period_end = (inv.get("period", {}).get("end") or "")[:10]
                if period_end < start_date.isoformat() or period_start > end_date.isoformat():
                    continue
                for item in inv.get("items", []):
                    name = item.get("name", "Unknown")
                    amount = float(item.get("price", 0))
                    total += amount
                    by_service[name] = by_service.get(name, 0.0) + amount
                    entries.append(CostEntry(
                        provider="vercel",
                        account_id=self._team_id or "personal",
                        account_name=self._team_id or "personal",
                        service=name,
                        region="",
                        amount=amount,
                    ))

        return CostSummary(
            provider="vercel",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service,
            by_account={self._team_id or "personal": total},
            by_region={},
            entries=entries,
        )

    async def get_costs_as_focus(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
    ) -> list:
        """Return Vercel cost as FOCUS 2.0 records (serverless/edge compute usage)."""
        from ...focus.translators.generic import saas_focus_records

        summary = await self.get_costs(start_date, end_date, granularity=granularity)
        return saas_focus_records(
            summary,
            provider="Vercel",
            publisher="Vercel",
            category="Compute",
            start_date=start_date,
            end_date=end_date,
        )

    async def list_accounts(self) -> list[dict[str, str]]:
        async with httpx.AsyncClient(timeout=15) as client:
            path = f"/v2/teams/{self._team_id}" if self._team_id else "/v2/user"
            r = await client.get(f"{self._API}{path}", headers=self._headers())
            if r.status_code == 200:
                data = r.json()
                name = data.get("name") or data.get("user", {}).get("name", "Vercel")
                return [{"id": self._team_id or "personal", "name": name}]
        return [{"id": "vercel", "name": "Vercel"}]
