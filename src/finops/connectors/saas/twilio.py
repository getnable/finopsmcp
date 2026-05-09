from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx

from ..base import BaseConnector, CostEntry, CostSummary


class TwilioConnector(BaseConnector):
    provider = "twilio"
    _API = "https://api.twilio.com/2010-04-01"

    def __init__(self) -> None:
        self._account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        self._auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")

    async def is_configured(self) -> bool:
        return bool(self._account_sid and self._auth_token)

    def _auth(self) -> tuple:
        return (self._account_sid, self._auth_token)

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
            # Usage Records API
            params: dict[str, Any] = {
                "StartDate": start_date.isoformat(),
                "EndDate": end_date.isoformat(),
                "PageSize": 100,
            }
            url = f"{self._API}/Accounts/{self._account_sid}/Usage/Records.json"

            while url:
                r = await client.get(url, auth=self._auth(), params=params)
                r.raise_for_status()
                data = r.json()
                params = {}  # only needed on first request; next_page_uri has them

                for record in data.get("usage_records", []):
                    category = record.get("category", "unknown")
                    price = float(record.get("price", 0) or 0)
                    if price == 0:
                        continue
                    total += price
                    by_service[category] = by_service.get(category, 0.0) + price
                    entries.append(CostEntry(
                        provider="twilio",
                        account_id=self._account_sid,
                        account_name=self._account_sid,
                        service=category,
                        region="",
                        amount=price,
                        metadata={"units": record.get("usage", ""), "unit_type": record.get("usage_unit", "")},
                    ))

                next_page = data.get("next_page_uri")
                url = f"https://api.twilio.com{next_page}" if next_page else None

        return CostSummary(
            provider="twilio",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service,
            by_account={self._account_sid: total},
            by_region={},
            entries=entries,
        )

    async def list_accounts(self) -> list[dict[str, str]]:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{self._API}/Accounts/{self._account_sid}.json",
                auth=self._auth(),
            )
            if r.status_code == 200:
                data = r.json()
                return [{"id": self._account_sid, "name": data.get("friendly_name", self._account_sid)}]
        return [{"id": self._account_sid, "name": "Twilio"}]
