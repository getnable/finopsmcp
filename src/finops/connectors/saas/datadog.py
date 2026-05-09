from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any

import httpx

from ..base import BaseConnector, CostEntry, CostSummary


class DatadogConnector(BaseConnector):
    provider = "datadog"
    _BASE = "https://api.datadoghq.com/api/v1"

    def __init__(self) -> None:
        self._api_key = os.getenv("DATADOG_API_KEY", "")
        self._app_key = os.getenv("DATADOG_APP_KEY", "")
        # EU site support
        site = os.getenv("DATADOG_SITE", "datadoghq.com")
        self._BASE = f"https://api.{site}/api/v2"

    async def is_configured(self) -> bool:
        return bool(self._api_key and self._app_key)

    def _headers(self) -> dict:
        return {
            "DD-API-KEY": self._api_key,
            "DD-APPLICATION-KEY": self._app_key,
            "Accept": "application/json",
        }

    async def get_costs(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
        group_by: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> CostSummary:
        # Usage Cost API: /api/v2/usage/estimated_cost
        params = {
            "start_month": start_date.strftime("%Y-%m"),
            "end_month": end_date.strftime("%Y-%m"),
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self._BASE}/usage/estimated_cost",
                headers=self._headers(),
                params=params,
            )
            r.raise_for_status()
            data = r.json()

        entries: list[CostEntry] = []
        by_service: dict[str, float] = {}
        total = 0.0

        for item in data.get("data", []):
            attrs = item.get("attributes", {})
            for charge in attrs.get("charges", []):
                product = charge.get("product_name", "Unknown")
                cost = float(charge.get("cost", 0))
                total += cost
                by_service[product] = by_service.get(product, 0.0) + cost
                entries.append(CostEntry(
                    provider="datadog",
                    account_id=attrs.get("org_name", "default"),
                    account_name=attrs.get("org_name", "default"),
                    service=product,
                    region="",
                    amount=cost,
                ))

        return CostSummary(
            provider="datadog",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service,
            by_account={"datadog": total},
            by_region={},
            entries=entries,
        )

    async def list_accounts(self) -> list[dict[str, str]]:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{self._BASE}/orgs",
                headers=self._headers(),
            )
            if r.status_code == 200:
                return [
                    {"id": o.get("public_id", ""), "name": o.get("name", "")}
                    for o in r.json().get("orgs", [])
                ]
        return [{"id": "default", "name": "Datadog Org"}]
