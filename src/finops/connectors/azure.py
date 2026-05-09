from __future__ import annotations

import os
from datetime import date
from typing import Any

from .base import BaseConnector, CostEntry, CostSummary


class AzureConnector(BaseConnector):
    provider = "azure"

    def __init__(self) -> None:
        self._subscription_ids: list[str] = [
            s.strip()
            for s in os.getenv("AZURE_SUBSCRIPTION_IDS", "").split(",")
            if s.strip()
        ]

    async def is_configured(self) -> bool:
        required = ["AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID"]
        return all(os.getenv(v) for v in required) and bool(self._subscription_ids)

    # ── internal helpers ────────────────────────────────────────────────────

    def _credential(self):
        from azure.identity import ClientSecretCredential

        return ClientSecretCredential(
            tenant_id=os.environ["AZURE_TENANT_ID"],
            client_id=os.environ["AZURE_CLIENT_ID"],
            client_secret=os.environ["AZURE_CLIENT_SECRET"],
        )

    def _query_costs(self, subscription_id: str, start_date: date, end_date: date, granularity: str) -> dict:
        from azure.mgmt.costmanagement import CostManagementClient
        from azure.mgmt.costmanagement.models import (
            QueryDataset,
            QueryDefinition,
            QueryGrouping,
            QueryTimePeriod,
        )

        client = CostManagementClient(self._credential())
        scope = f"/subscriptions/{subscription_id}"

        query = QueryDefinition(
            type="ActualCost",
            timeframe="Custom",
            time_period=QueryTimePeriod(
                from_property=f"{start_date.isoformat()}T00:00:00Z",
                to=f"{end_date.isoformat()}T00:00:00Z",
            ),
            dataset=QueryDataset(
                granularity=granularity.capitalize(),
                grouping=[
                    QueryGrouping(type="Dimension", name="ServiceName"),
                    QueryGrouping(type="Dimension", name="ResourceLocation"),
                ],
            ),
        )

        result = client.query.usage(scope=scope, parameters=query)
        return result

    def _parse_result(self, result, subscription_id: str, start_date: date, end_date: date) -> CostSummary:
        entries: list[CostEntry] = []
        by_service: dict[str, float] = {}
        by_region: dict[str, float] = {}
        total = 0.0

        columns = {col.name: i for i, col in enumerate(result.columns)}
        cost_idx = columns.get("Cost", 0)
        service_idx = columns.get("ServiceName", 2)
        region_idx = columns.get("ResourceLocation", 3)

        for row in result.rows or []:
            amount = float(row[cost_idx])
            service = str(row[service_idx])
            region = str(row[region_idx])
            total += amount
            by_service[service] = by_service.get(service, 0.0) + amount
            by_region[region] = by_region.get(region, 0.0) + amount
            entries.append(
                CostEntry(
                    provider="azure",
                    account_id=subscription_id,
                    account_name=subscription_id,
                    service=service,
                    region=region,
                    amount=amount,
                )
            )

        return CostSummary(
            provider="azure",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service,
            by_account={subscription_id: total},
            by_region=by_region,
            entries=entries,
        )

    # ── public API ──────────────────────────────────────────────────────────

    async def get_costs(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
        group_by: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> CostSummary:
        merged = CostSummary(
            provider="azure",
            start_date=start_date,
            end_date=end_date,
            total_usd=0.0,
            by_service={},
            by_account={},
            by_region={},
            entries=[],
        )

        for sub_id in self._subscription_ids:
            raw = self._query_costs(sub_id, start_date, end_date, granularity)
            summary = self._parse_result(raw, sub_id, start_date, end_date)
            merged.total_usd += summary.total_usd
            for k, v in summary.by_service.items():
                merged.by_service[k] = merged.by_service.get(k, 0.0) + v
            for k, v in summary.by_account.items():
                merged.by_account[k] = merged.by_account.get(k, 0.0) + v
            for k, v in summary.by_region.items():
                merged.by_region[k] = merged.by_region.get(k, 0.0) + v
            merged.entries.extend(summary.entries)

        return merged

    async def list_accounts(self) -> list[dict[str, str]]:
        return [{"id": s, "name": s} for s in self._subscription_ids]
