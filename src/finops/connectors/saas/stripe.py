from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any

import httpx

from ..base import BaseConnector, CostEntry, CostSummary


class StripeConnector(BaseConnector):
    """
    Tracks what you pay *to* Stripe — i.e., Stripe fees on your invoices/charges.
    Useful for SaaS companies tracking payment processing costs.
    """
    provider = "stripe"
    _API = "https://api.stripe.com/v1"

    def __init__(self) -> None:
        self._secret_key = os.getenv("STRIPE_SECRET_KEY", "")

    async def is_configured(self) -> bool:
        return bool(self._secret_key)

    def _auth(self) -> tuple:
        return (self._secret_key, "")

    async def get_costs(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
        group_by: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> CostSummary:
        start_ts = int(datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc).timestamp())
        end_ts = int(datetime.combine(end_date, datetime.min.time()).replace(tzinfo=timezone.utc).timestamp())

        entries: list[CostEntry] = []
        by_service: dict[str, float] = {}
        total = 0.0

        async with httpx.AsyncClient(timeout=30) as client:
            # Pull balance transactions of type "stripe_fee"
            params: dict[str, Any] = {
                "type": "stripe_fee",
                "created[gte]": start_ts,
                "created[lte]": end_ts,
                "limit": 100,
            }
            while True:
                r = await client.get(
                    f"{self._API}/balance_transactions",
                    auth=self._auth(),
                    params=params,
                )
                r.raise_for_status()
                data = r.json()

                for txn in data.get("data", []):
                    fee = abs(float(txn.get("fee", 0))) / 100  # cents -> dollars
                    desc = txn.get("description") or "Processing Fee"
                    total += fee
                    by_service[desc] = by_service.get(desc, 0.0) + fee
                    entries.append(CostEntry(
                        provider="stripe",
                        account_id="stripe",
                        account_name="Stripe",
                        service=desc,
                        region="",
                        amount=fee,
                    ))

                if not data.get("has_more"):
                    break
                params["starting_after"] = data["data"][-1]["id"]

        # Collapse into summary categories
        by_service_collapsed: dict[str, float] = {
            "Payment Processing Fees": total
        }

        return CostSummary(
            provider="stripe",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service_collapsed,
            by_account={"stripe": total},
            by_region={},
            entries=entries,
        )

    async def list_accounts(self) -> list[dict[str, str]]:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{self._API}/account", auth=self._auth())
            if r.status_code == 200:
                data = r.json()
                return [{"id": data.get("id", ""), "name": data.get("settings", {}).get("dashboard", {}).get("display_name", "Stripe Account")}]
        return [{"id": "stripe", "name": "Stripe Account"}]
