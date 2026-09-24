from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timezone
from typing import Any

from . import azure_detail as _detail
from .base import BaseConnector, CostEntry, CostSummary, combined_currency


class AzureConnector(BaseConnector):
    provider = "azure"

    def __init__(self) -> None:
        self._subscription_ids: list[str] = [
            s.strip()
            for s in os.getenv("AZURE_SUBSCRIPTION_IDS", "").split(",")
            if s.strip()
        ]

    async def is_configured(self) -> bool:
        """Any usable Azure credential, not just a service principal.

        This used to require AZURE_CLIENT_ID/SECRET/TENANT_ID plus an explicit
        AZURE_SUBSCRIPTION_IDS. Someone who had run `az login`, which is the
        normal state for anyone working in Azure, read as unconfigured and was
        sent through a manual service principal wizard. AWS meanwhile accepted
        its whole default credential chain. Same question, same answer, for
        every cloud now: see finops/ambient.py."""
        required = ["AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID"]
        if all(os.getenv(v) for v in required) and self._subscription_ids:
            return True
        # to_thread, not a direct call. PROBES["azure"] shells out to the Azure
        # SDK's credential chain, which is synchronous and capped at 6s. Calling
        # it straight from a coroutine pins the event loop for that whole time:
        # nothing else runs, not the sibling provider probe in the same gather,
        # not the per-provider timeout that is supposed to bound a hung API, not
        # a streaming response to the user.
        #
        # _active() gathers is_configured() across providers and is the front
        # door for essentially every cost tool, so on a machine where the SDKs
        # are installed and the credential chain is slow (the docker and
        # enterprise image) the user waits Azure's probe PLUS GCP's, serially,
        # once every ambient.CACHE_TTL_S of tool use. Up to 12 seconds of dead
        # air before an answer starts.
        import asyncio

        from ..ambient import PROBES
        amb = await asyncio.to_thread(PROBES["azure"])
        if amb.usable and not self._subscription_ids:
            # Adopt what the ambient credential can actually see, so the rest of
            # the connector has a scope to query without a second setup step.
            self._subscription_ids = amb.scopes
        return amb.usable

    # ── internal helpers ────────────────────────────────────────────────────

    def _credential(self):
        """The service principal when it is fully configured, else the default chain.

        is_configured() accepts `az login`, managed identity and the rest of
        DefaultAzureCredential, which set none of the service principal vars.
        Building ClientSecretCredential unconditionally made every one of those
        users fail on KeyError 'AZURE_TENANT_ID'.
        """
        sp = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
        if all(os.getenv(v) for v in sp):
            from azure.identity import ClientSecretCredential

            return ClientSecretCredential(
                tenant_id=os.environ["AZURE_TENANT_ID"],
                client_id=os.environ["AZURE_CLIENT_ID"],
                client_secret=os.environ["AZURE_CLIENT_SECRET"],
            )
        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential(exclude_interactive_browser_credential=True)

    def _query_costs(self, subscription_id: str, start_date: date, end_date: date, granularity: str) -> list[dict]:
        """Rows (dicts keyed by column name) from the Cost Management Query API.

        REST through azure_detail rather than the SDK's query.usage, which
        returns one page and has no way to follow nextLink: a subscription with
        more rows than a page reported only the first. azure-identity mints the
        token, as in ambient.py. The aggregation is explicit so the cost column
        is named "Cost" rather than whatever the API defaults to.
        """
        token = self._credential().get_token("https://management.azure.com/.default").token
        body = {
            "type": "ActualCost",
            "timeframe": "Custom",
            "timePeriod": {
                "from": f"{start_date.isoformat()}T00:00:00Z",
                "to": f"{end_date.isoformat()}T00:00:00Z",
            },
            "dataset": {
                "granularity": granularity.capitalize(),
                "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                "grouping": [
                    {"type": "Dimension", "name": "ServiceName"},
                    {"type": "Dimension", "name": "ResourceLocation"},
                ],
            },
        }
        return _detail._query_cost_management(token, subscription_id, body)

    def _parse_result(self, rows: list[dict], subscription_id: str, start_date: date, end_date: date) -> CostSummary:
        entries: list[CostEntry] = []
        by_service: dict[str, float] = {}
        by_region: dict[str, float] = {}
        total = 0.0
        currencies: set[str] = set()

        for row in rows:
            # By name only. The positional fallback read column 0 as cost when
            # the name did not match, which is a date or a service on some shapes.
            if "Cost" not in row:
                raise RuntimeError(
                    f"Azure cost query returned no Cost column (columns: {sorted(row)})")
            amount = float(row["Cost"] or 0)
            service = str(row.get("ServiceName") or "")
            region = str(row.get("ResourceLocation") or "")
            # The query returns the billing currency as its own column. A EUR or
            # JPY subscription used to come out labelled USD.
            cur = str(row.get("Currency") or "")
            if cur:
                currencies.add(cur)
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
                    currency=cur or "USD",
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
            currency=(currencies.pop() if len(currencies) == 1 else ("MIXED" if currencies else "USD")),
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
        # Read-through cache + parallel subscriptions. Azure Cost Management is
        # the slowest provider API in the stack, and the sync SDK call used to
        # run on the event loop, blocking every other connector while it waited.
        import copy as _copy
        from .. import cache as _cache
        _ck = _cache.make_key(
            "azure.get_costs", ",".join(sorted(self._subscription_ids)),
            start_date.isoformat(), end_date.isoformat(), granularity,
        )
        _hit = _cache.get(_ck)
        if _hit is not None:
            return _copy.deepcopy(_hit)

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

        async def _one(sub_id: str) -> CostSummary:
            try:
                raw = await asyncio.to_thread(self._query_costs, sub_id, start_date, end_date, granularity)
            except Exception as exc:
                from ._reauth import is_auth_expiry, reauth_message
                if is_auth_expiry(exc, "azure"):
                    raise RuntimeError(reauth_message("azure", f"subscription {sub_id}")) from exc
                raise
            return self._parse_result(raw, sub_id, start_date, end_date)

        _parts = await asyncio.gather(*[_one(s) for s in self._subscription_ids])
        for summary in _parts:
            merged.total_usd += summary.total_usd
            for k, v in summary.by_service.items():
                merged.by_service[k] = merged.by_service.get(k, 0.0) + v
            for k, v in summary.by_account.items():
                merged.by_account[k] = merged.by_account.get(k, 0.0) + v
            for k, v in summary.by_region.items():
                merged.by_region[k] = merged.by_region.get(k, 0.0) + v
            merged.entries.extend(summary.entries)
        # Each subscription reports in its own billing currency; carry it through.
        merged.currency = combined_currency(list(_parts))

        _cache.set(_ck, _copy.deepcopy(merged), _cache.COST_TTL)
        return merged

    async def get_costs_as_focus(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
    ) -> list:
        """Return cost data as a list of FocusRecord objects."""
        from ..focus import normalize

        summary = await self.get_costs(start_date, end_date, granularity=granularity)
        period_start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)
        period_end = datetime(end_date.year, end_date.month, end_date.day, tzinfo=timezone.utc)

        records = []
        for entry in summary.entries:
            raw: dict[str, Any] = {
                "BilledCost": entry.amount,
                "EffectiveCost": entry.amount,
                "ServiceName": entry.service,
                "ResourceLocation": entry.region,
                "SubscriptionId": entry.account_id,
                "SubscriptionName": entry.account_name,
                "ChargeType": "Usage",
                "BillingPeriodStartDate": period_start.isoformat(),
                "BillingPeriodEndDate": period_end.isoformat(),
                "UsageDate": period_start.isoformat(),
                "Tags": entry.tags,
            }
            records.append(normalize("azure", raw))
        return records

    async def list_accounts(self) -> list[dict[str, str]]:
        return [{"id": s, "name": s} for s in self._subscription_ids]
