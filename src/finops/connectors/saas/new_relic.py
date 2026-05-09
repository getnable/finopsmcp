from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

import httpx

from ..base import BaseConnector, CostEntry, CostSummary


class NewRelicConnector(BaseConnector):
    """
    Returns actual consumption metrics from NerdGraph: GB ingested, user counts.

    Dollar amounts are ONLY reported when the user explicitly sets their contract
    rates via NEW_RELIC_INGEST_PRICE_PER_GB and NEW_RELIC_FULL_PLATFORM_PRICE.
    Without those, we return consumption data with amount=0 and metadata.
    """
    provider = "new_relic"
    _GRAPHQL = "https://api.newrelic.com/graphql"

    def __init__(self) -> None:
        self._api_key = os.getenv("NEW_RELIC_API_KEY", "")
        self._account_id = os.getenv("NEW_RELIC_ACCOUNT_ID", "")
        raw_ingest = os.getenv("NEW_RELIC_INGEST_PRICE_PER_GB", "")
        raw_fp = os.getenv("NEW_RELIC_FULL_PLATFORM_PRICE", "")
        self._ingest_price: float | None = float(raw_ingest) if raw_ingest else None
        self._fp_price: float | None = float(raw_fp) if raw_fp else None

    async def is_configured(self) -> bool:
        return bool(self._api_key and self._account_id)

    def _headers(self) -> dict:
        return {"Api-Key": self._api_key, "Content-Type": "application/json"}

    async def _nrql(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        gql = {"query": f'{{ actor {{ account(id: {self._account_id}) {{ nrql(query: "{query}") {{ results }} }} }} }}'}
        r = await client.post(self._GRAPHQL, headers=self._headers(), json=gql)
        r.raise_for_status()
        return r.json().get("data", {}).get("actor", {}).get("account", {}).get("nrql", {}).get("results", [])

    async def get_costs(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
        group_by: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> CostSummary:
        since = f"SINCE '{start_date.isoformat()}'"
        until = f"UNTIL '{end_date.isoformat()}'"
        days = (end_date - start_date).days or 1
        months = days / 30

        entries: list[CostEntry] = []
        by_service: dict[str, float] = {}
        total = 0.0

        async with httpx.AsyncClient(timeout=30) as client:
            ingest_res = await self._nrql(
                client,
                f"SELECT sum(GigabytesIngested) FROM NrConsumption WHERE productLine='DataPlatform' {since} {until} LIMIT 1",
            )
            gb = float((ingest_res[0].get("sum.GigabytesIngested") or 0) if ingest_res else 0)

            if self._ingest_price is not None:
                ingest_cost = gb * self._ingest_price
                total += ingest_cost
                by_service["Data Ingest"] = ingest_cost
            else:
                ingest_cost = 0.0
                by_service["Data Ingest"] = 0.0

            entries.append(CostEntry(
                provider="new_relic",
                account_id=self._account_id,
                account_name=self._account_id,
                service="Data Ingest",
                region="",
                amount=ingest_cost,
                metadata={
                    "gb_ingested": round(gb, 4),
                    "cost_source": "user_contract_rate" if self._ingest_price else "not_available",
                    "note": "" if self._ingest_price else "Set NEW_RELIC_INGEST_PRICE_PER_GB for USD amounts",
                },
            ))

            fp_res = await self._nrql(
                client,
                f"SELECT latest(UserCount) FROM NrMTDConsumption WHERE metric='FullPlatformUsersBillable' {since} LIMIT 1",
            )
            fp_users = float((fp_res[0].get("latest.UserCount") or 0) if fp_res else 0)

            if self._fp_price is not None:
                fp_cost = fp_users * self._fp_price * months
                total += fp_cost
                by_service["Full Platform Users"] = fp_cost
            else:
                fp_cost = 0.0
                by_service["Full Platform Users"] = 0.0

            entries.append(CostEntry(
                provider="new_relic",
                account_id=self._account_id,
                account_name=self._account_id,
                service="Full Platform Users",
                region="",
                amount=fp_cost,
                metadata={
                    "user_count": int(fp_users),
                    "cost_source": "user_contract_rate" if self._fp_price else "not_available",
                    "note": "" if self._fp_price else "Set NEW_RELIC_FULL_PLATFORM_PRICE for USD amounts",
                },
            ))

        return CostSummary(
            provider="new_relic",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service,
            by_account={self._account_id: total},
            by_region={},
            entries=entries,
        )

    async def list_accounts(self) -> list[dict[str, str]]:
        return [{"id": self._account_id, "name": f"New Relic {self._account_id}"}]
