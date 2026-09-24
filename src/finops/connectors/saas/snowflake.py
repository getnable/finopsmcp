from __future__ import annotations

import calendar
import os
from datetime import date
from typing import Any

from ..base import BaseConnector, CostEntry, CostSummary


class SnowflakeConnector(BaseConnector):
    """
    Returns actual credits consumed from ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY.

    Dollar conversion ONLY happens when SNOWFLAKE_CREDIT_PRICE is explicitly set
    by the user (i.e. they know their contract rate). Without it get_costs raises
    with the credits consumed rather than reporting an invented (or zero) dollar
    amount. Storage is priced separately, per TB-month, and is included when
    SNOWFLAKE_STORAGE_PRICE_PER_TB is set to the contract storage rate.

    NOTE: Every query nable runs against Snowflake consumes warehouse compute credits.
    Each cost query is a single SQL statement against ACCOUNT_USAGE views, which are
    lightweight, but this is not zero-cost. If your warehouse auto-suspends, nable
    queries will resume it and consume credits. Set SNOWFLAKE_WAREHOUSE to a
    dedicated small warehouse (X-SMALL) to minimize cost. Typical nable query cost:
    less than 0.01 credits per call at X-SMALL sizing.
    """
    provider = "snowflake"

    def __init__(self) -> None:
        self._account = os.getenv("SNOWFLAKE_ACCOUNT", "")
        self._user = os.getenv("SNOWFLAKE_USER", "")
        self._password = os.getenv("SNOWFLAKE_PASSWORD", "")
        self._warehouse = os.getenv("SNOWFLAKE_WAREHOUSE", "")
        self._role = os.getenv("SNOWFLAKE_ROLE", "ACCOUNTADMIN")
        self._private_key_path = os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH", "")
        # Only set if the user knows their actual contract rate
        raw = os.getenv("SNOWFLAKE_CREDIT_PRICE", "")
        self._credit_price: float | None = float(raw) if raw else None
        raw = os.getenv("SNOWFLAKE_STORAGE_PRICE_PER_TB", "")
        self._storage_price_tb: float | None = float(raw) if raw else None

    async def is_configured(self) -> bool:
        has_auth = bool(self._password or self._private_key_path)
        return bool(self._account and self._user and has_auth)

    def _connect(self):
        try:
            import snowflake.connector
        except ImportError as e:
            raise RuntimeError(
                "Snowflake support needs an extra dependency. "
                "Run: pip install 'finops-mcp[snowflake]'"
            ) from e
        kwargs: dict[str, Any] = dict(account=self._account, user=self._user, role=self._role)
        if self._warehouse:
            kwargs["warehouse"] = self._warehouse
        if self._private_key_path:
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives.serialization import (
                Encoding, NoEncryption, PrivateFormat, load_pem_private_key,
            )
            with open(self._private_key_path, "rb") as f:
                pk = load_pem_private_key(f.read(), password=None, backend=default_backend())
            kwargs["private_key"] = pk.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
        else:
            kwargs["password"] = self._password
        return snowflake.connector.connect(**kwargs)

    async def get_costs(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
        group_by: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> CostSummary:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT WAREHOUSE_NAME, SUM(CREDITS_USED) AS total_credits
                FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
                WHERE START_TIME::DATE >= %s AND START_TIME::DATE <= %s
                GROUP BY WAREHOUSE_NAME ORDER BY total_credits DESC
            """, (start_date.isoformat(), end_date.isoformat()))
            wh_rows = cur.fetchall()

            storage_rows: list = []
            if self._storage_price_tb is not None:
                # Per day, because storage bills on the daily average over each
                # calendar month: a day's TB costs rate / days-in-that-month.
                cur.execute("""
                    SELECT
                        USAGE_DATE,
                        (STORAGE_BYTES + STAGE_BYTES + FAILSAFE_BYTES) / POWER(1024,4) AS total_tb
                    FROM SNOWFLAKE.ACCOUNT_USAGE.STORAGE_USAGE
                    WHERE USAGE_DATE >= %s AND USAGE_DATE <= %s
                """, (start_date.isoformat(), end_date.isoformat()))
                storage_rows = cur.fetchall()
        finally:
            conn.close()

        if self._credit_price is None:
            # Unpriced credits are not $0. Contributing 0 USD here made 15,000
            # credits of warehouse spend render as $0.00 in the cost summary.
            credits_total = sum(float(r[1] or 0) for r in wh_rows)
            raise RuntimeError(
                f"Snowflake used {credits_total:,.2f} credits from {start_date} to "
                f"{end_date}, but SNOWFLAKE_CREDIT_PRICE is not set, so nable cannot "
                f"express that in USD. Set SNOWFLAKE_CREDIT_PRICE to your contract "
                f"price per credit. This is a missing rate, not a finding of zero spend."
            )

        entries: list[CostEntry] = []
        by_service: dict[str, float] = {}
        total = 0.0

        for row in wh_rows:
            wh_name, credits = row[0], float(row[1] or 0)
            svc = f"Warehouse: {wh_name}"
            amount = credits * self._credit_price
            total += amount
            by_service[svc] = by_service.get(svc, 0.0) + amount
            entries.append(CostEntry(
                provider="snowflake",
                account_id=self._account,
                account_name=self._account,
                service=svc,
                region="",
                amount=amount,
                metadata={
                    "credits_consumed": credits,
                    "cost_source": "user_contract_rate",
                },
            ))

        if storage_rows:
            storage = 0.0
            for usage_date, tb in storage_rows:
                d = usage_date if isinstance(usage_date, date) else date.fromisoformat(str(usage_date)[:10])
                days = calendar.monthrange(d.year, d.month)[1]
                storage += float(tb or 0) * self._storage_price_tb / days  # type: ignore[operator]
            total += storage
            by_service["Storage"] = by_service.get("Storage", 0.0) + storage
            entries.append(CostEntry(
                provider="snowflake",
                account_id=self._account,
                account_name=self._account,
                service="Storage",
                region="",
                amount=storage,
                metadata={"cost_source": "user_contract_rate", "storage_days": len(storage_rows)},
            ))

        return CostSummary(
            provider="snowflake",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service,
            by_account={self._account: total},
            by_region={},
            entries=entries,
        )

    async def get_costs_as_focus(
        self,
        start_date: date,
        end_date: date,
        granularity: str = "MONTHLY",
    ) -> list:
        """Return Snowflake cost as FOCUS 1.2 records (warehouse compute as Database usage).

        Credits consumed ride along in each record's Tags. Without a contract
        credit price get_costs raises, so no record is ever emitted at $0.
        """
        from ...focus.translators.generic import saas_focus_records

        summary = await self.get_costs(start_date, end_date, granularity=granularity)
        return saas_focus_records(
            summary,
            provider="Snowflake",
            publisher="Snowflake",
            category="Database",
            start_date=start_date,
            end_date=end_date,
            resource_type="Warehouse",
        )

    async def list_accounts(self) -> list[dict[str, str]]:
        return [{"id": self._account, "name": self._account}]
