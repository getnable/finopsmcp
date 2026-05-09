from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx

from ..base import BaseConnector, CostEntry, CostSummary


class MongoDBAtlasConnector(BaseConnector):
    provider = "mongodb_atlas"
    _API = "https://cloud.mongodb.com/api/atlas/v2"

    def __init__(self) -> None:
        self._public_key = os.getenv("MONGODB_ATLAS_PUBLIC_KEY", "")
        self._private_key = os.getenv("MONGODB_ATLAS_PRIVATE_KEY", "")
        self._org_ids: list[str] = [
            o.strip() for o in os.getenv("MONGODB_ATLAS_ORG_IDS", "").split(",") if o.strip()
        ]

    async def is_configured(self) -> bool:
        return bool(self._public_key and self._private_key and self._org_ids)

    def _auth(self) -> httpx.DigestAuth:
        return httpx.DigestAuth(self._public_key, self._private_key)

    def _headers(self) -> dict:
        return {"Accept": "application/vnd.atlas.2023-01-01+json"}

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
            for org_id in self._org_ids:
                # List invoices in date range
                r = await client.get(
                    f"{self._API}/orgs/{org_id}/invoices",
                    auth=self._auth(),
                    headers=self._headers(),
                )
                r.raise_for_status()
                invoices = r.json().get("results", [])

                for inv in invoices:
                    # Filter by period overlap
                    inv_start = inv.get("startDate", "")[:10]
                    inv_end = inv.get("endDate", "")[:10]
                    if inv_end < start_date.isoformat() or inv_start > end_date.isoformat():
                        continue

                    amount_cents = inv.get("amountBilledCents", 0)
                    amount = amount_cents / 100
                    total += amount

                    # Break down by line items
                    for line in inv.get("lineItems", []):
                        sku = line.get("sku", "Unknown")
                        line_amount = line.get("totalPriceCents", 0) / 100
                        by_service[sku] = by_service.get(sku, 0.0) + line_amount
                        entries.append(CostEntry(
                            provider="mongodb_atlas",
                            account_id=org_id,
                            account_name=org_id,
                            service=sku,
                            region=line.get("clusterName", ""),
                            amount=line_amount,
                        ))

        return CostSummary(
            provider="mongodb_atlas",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service,
            by_account={org: total for org in self._org_ids},
            by_region={},
            entries=entries,
        )

    async def list_accounts(self) -> list[dict[str, str]]:
        accounts = []
        async with httpx.AsyncClient(timeout=15) as client:
            for org_id in self._org_ids:
                r = await client.get(
                    f"{self._API}/orgs/{org_id}",
                    auth=self._auth(),
                    headers=self._headers(),
                )
                if r.status_code == 200:
                    data = r.json()
                    accounts.append({"id": org_id, "name": data.get("name", org_id)})
                else:
                    accounts.append({"id": org_id, "name": org_id})
        return accounts
