from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx

from ..base import BaseConnector, CostEntry, CostSummary


class GitHubConnector(BaseConnector):
    """
    Returns actual billable data from GitHub's billing API.

    Actions: reports paid_minutes_used (real). Does NOT convert to dollars
    because the API gives minutes, not runner-type breakdowns, so any rate
    applied would be a guess. Users can see dollar totals in GitHub billing.

    Copilot: reports active seats only. GitHub does not expose invoice totals via API.

    Packages: reports paid bandwidth/storage GB (real).
    """
    provider = "github"
    _API = "https://api.github.com"

    def __init__(self) -> None:
        self._token = os.getenv("GITHUB_TOKEN", "")
        self._orgs: list[str] = [
            o.strip() for o in os.getenv("GITHUB_ORGS", "").split(",") if o.strip()
        ]

    async def is_configured(self) -> bool:
        return bool(self._token and self._orgs)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _get(self, client: httpx.AsyncClient, path: str) -> Any:
        r = await client.get(f"{self._API}{path}", headers=self._headers())
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

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

        async with httpx.AsyncClient(timeout=30) as client:
            for org in self._orgs:
                actions = await self._get(client, f"/orgs/{org}/settings/billing/actions")
                if actions:
                    paid_min = int(actions.get("total_paid_minutes_used", 0))
                    included_min = int(actions.get("included_minutes", 0))
                    total_min = int(actions.get("total_minutes_used", 0))
                    # amount=0: we have minutes, not dollars
                    entries.append(CostEntry(
                        provider="github",
                        account_id=org,
                        account_name=org,
                        service="GitHub Actions",
                        region="",
                        amount=0.0,
                        metadata={
                            "paid_minutes": paid_min,
                            "included_minutes": included_min,
                            "total_minutes": total_min,
                            "note": "GitHub API returns minutes, not dollars. See GitHub billing portal for USD total.",
                        },
                    ))
                    by_service["GitHub Actions"] = 0.0

                packages = await self._get(client, f"/orgs/{org}/settings/billing/packages")
                if packages:
                    paid_bandwidth_gb = float(packages.get("total_paid_bandwidth_gb", 0))
                    paid_storage_gb = float(packages.get("total_paid_storage_gb", 0))
                    entries.append(CostEntry(
                        provider="github",
                        account_id=org,
                        account_name=org,
                        service="GitHub Packages",
                        region="",
                        amount=0.0,
                        metadata={
                            "paid_bandwidth_gb": paid_bandwidth_gb,
                            "paid_storage_gb": paid_storage_gb,
                            "note": "See GitHub billing portal for USD total.",
                        },
                    ))
                    by_service["GitHub Packages"] = 0.0

                copilot = await self._get(client, f"/orgs/{org}/copilot/billing")
                if copilot:
                    seats = int(copilot.get("seat_breakdown", {}).get("active_this_cycle", 0))
                    entries.append(CostEntry(
                        provider="github",
                        account_id=org,
                        account_name=org,
                        service="GitHub Copilot",
                        region="",
                        amount=0.0,
                        metadata={
                            "active_seats": seats,
                            "note": "GitHub API does not expose Copilot invoice totals. Check GitHub billing portal.",
                        },
                    ))
                    by_service["GitHub Copilot"] = 0.0

        return CostSummary(
            provider="github",
            start_date=start_date,
            end_date=end_date,
            total_usd=0.0,
            by_service=by_service,
            by_account={org: 0.0 for org in self._orgs},
            by_region={},
            entries=entries,
        )

    async def get_costs_as_focus(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
    ) -> list:
        """Return GitHub cost as FOCUS 2.0 records (Actions, Packages, storage usage)."""
        from ...focus.translators.generic import saas_focus_records

        summary = await self.get_costs(start_date, end_date, granularity=granularity)
        return saas_focus_records(
            summary,
            provider="GitHub",
            publisher="GitHub",
            category="Other",
            start_date=start_date,
            end_date=end_date,
        )

    async def list_accounts(self) -> list[dict[str, str]]:
        return [{"id": org, "name": org} for org in self._orgs]
