from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any

import httpx

from ..base import BaseConnector, CostEntry, CostSummary


def _normalize_monthly(
    unit_amount_cents: int | None,
    quantity: int | None,
    interval: str | None,
    interval_count: int | None,
) -> float | None:
    """
    Normalize a single Stripe subscription-item price to a monthly USD amount.

    Returns None for metered / usage-based prices (no fixed unit_amount) so the
    caller can skip them and keep MRR a conservative floor rather than guessing.
    """
    if unit_amount_cents is None:
        return None
    amount = (unit_amount_cents / 100.0) * (quantity or 1)
    n = interval_count or 1
    if interval == "month":
        months_per_period = n
    elif interval == "year":
        months_per_period = n * 12
    elif interval == "week":
        months_per_period = n / 4.345  # ~weeks per month
    elif interval == "day":
        months_per_period = n / 30.44  # ~days per month
    else:
        return None
    if months_per_period <= 0:
        return None
    return amount / months_per_period


class StripeConnector(BaseConnector):
    """
    Tracks what you pay *to* Stripe, i.e. Stripe fees on your invoices/charges.
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

    async def fetch_business_snapshot(self) -> dict[str, Any]:
        """
        Pull current MRR and active paying-customer count by summing active
        subscriptions. This is what lets unit economics (cost per customer,
        AI as % of MRR) work the first time someone asks, with no manual entry.

        MRR              sum of active subscription items normalized to monthly.
        paying_customers distinct customers with at least one active subscription.

        Metered / usage-based items (no fixed unit_amount) are skipped and noted,
        so MRR is a floor, never an overstatement. Multi-item subscriptions are
        read from the inline items list (Stripe returns up to 10 per subscription;
        rare overflow is noted rather than silently dropped).
        """
        if not self._secret_key:
            return {}

        mrr = 0.0
        customers: set[str] = set()
        currencies: set[str] = set()
        skipped_metered = 0
        truncated_items = 0
        pages = 0

        params: dict[str, Any] = {"status": "active", "limit": 100}
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                r = await client.get(
                    f"{self._API}/subscriptions",
                    auth=self._auth(),
                    params=params,
                )
                r.raise_for_status()
                data = r.json()

                for sub in data.get("data", []):
                    cust = sub.get("customer")
                    if isinstance(cust, dict):
                        cust = cust.get("id")
                    if cust:
                        customers.add(cust)

                    items = sub.get("items", {}) or {}
                    if items.get("has_more"):
                        truncated_items += 1
                    for item in items.get("data", []):
                        price = item.get("price") or {}
                        rec = price.get("recurring") or {}
                        monthly = _normalize_monthly(
                            price.get("unit_amount"),
                            item.get("quantity"),
                            rec.get("interval"),
                            rec.get("interval_count"),
                        )
                        if monthly is None:
                            skipped_metered += 1
                            continue
                        mrr += monthly
                        if price.get("currency"):
                            currencies.add(price["currency"].upper())

                pages += 1
                if not data.get("has_more") or pages >= 100 or not data.get("data"):
                    break
                params["starting_after"] = data["data"][-1]["id"]

        caveats: list[str] = []
        if skipped_metered:
            caveats.append(
                f"{skipped_metered} metered/usage-based item(s) skipped (no fixed "
                f"price); MRR is a floor."
            )
        if truncated_items:
            caveats.append(
                f"{truncated_items} subscription(s) have more than 10 line items; "
                f"MRR may be understated for those."
            )
        if len(currencies) > 1:
            caveats.append(
                f"Mixed currencies {sorted(currencies)} summed without FX conversion."
            )

        return {
            "mrr_usd":          round(mrr, 2),
            "paying_customers": len(customers),
            "currency":         (sorted(currencies)[0] if currencies else "USD"),
            "as_of":            datetime.now(timezone.utc).isoformat(),
            "caveats":          caveats,
            "source":           "stripe_active_subscriptions",
        }

    async def list_accounts(self) -> list[dict[str, str]]:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{self._API}/account", auth=self._auth())
            if r.status_code == 200:
                data = r.json()
                return [{"id": data.get("id", ""), "name": data.get("settings", {}).get("dashboard", {}).get("display_name", "Stripe Account")}]
        return [{"id": "stripe", "name": "Stripe Account"}]
