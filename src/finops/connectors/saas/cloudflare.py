from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx

from ..base import BaseConnector, CostEntry, CostSummary


class CloudflareConnector(BaseConnector):
    provider = "cloudflare"
    _API = "https://api.cloudflare.com/client/v4"

    def __init__(self) -> None:
        self._api_token = os.getenv("CLOUDFLARE_API_TOKEN", "")
        self._account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")

    async def is_configured(self) -> bool:
        return bool(self._api_token and self._account_id)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_token}"}

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
            # Billing history
            r = await client.get(
                f"{self._API}/accounts/{self._account_id}/billing-history",
                headers=self._headers(),
                params={"since": start_date.isoformat(), "before": end_date.isoformat()},
            )
            if r.status_code == 200:
                for item in r.json().get("result", []):
                    amount = float(item.get("amount", 0))
                    product = item.get("type", "Unknown")
                    zone = item.get("zone", {}).get("name", "")
                    total += amount
                    by_service[product] = by_service.get(product, 0.0) + amount
                    entries.append(CostEntry(
                        provider="cloudflare",
                        account_id=self._account_id,
                        account_name=self._account_id,
                        service=product,
                        region=zone,
                        amount=amount,
                    ))
            else:
                # Fallback: fetch subscriptions
                r2 = await client.get(
                    f"{self._API}/accounts/{self._account_id}/subscriptions",
                    headers=self._headers(),
                )
                if r2.status_code == 200:
                    for sub in r2.json().get("result", []):
                        name = sub.get("component_values", [{}])[0].get("name", "Subscription")
                        price = float(sub.get("price", 0))
                        if price:
                            by_service[name] = by_service.get(name, 0.0) + price
                            total += price
                            entries.append(CostEntry(
                                provider="cloudflare",
                                account_id=self._account_id,
                                account_name=self._account_id,
                                service=name,
                                region="",
                                amount=price,
                            ))

        return CostSummary(
            provider="cloudflare",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service,
            by_account={self._account_id: total},
            by_region={},
            entries=entries,
        )

    async def list_accounts(self) -> list[dict[str, str]]:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{self._API}/accounts/{self._account_id}",
                headers=self._headers(),
            )
            if r.status_code == 200:
                data = r.json().get("result", {})
                return [{"id": self._account_id, "name": data.get("name", self._account_id)}]
        return [{"id": self._account_id, "name": "Cloudflare"}]
