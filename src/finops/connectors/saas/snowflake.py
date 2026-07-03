from __future__ import annotations

import os
from datetime import date
from typing import Any

from ..base import BaseConnector, CostEntry, CostSummary


class SnowflakeConnector(BaseConnector):
    """
    Returns actual credits consumed from ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY.

    Dollar conversion ONLY happens when SNOWFLAKE_CREDIT_PRICE is explicitly set
    by the user (i.e. they know their contract rate). Without it we report credits,
    not invented dollar amounts.

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

    async def is_configured(self) -> bool:
        has_auth = bool(self._password or self._private_key_path)
        return bool(self._account and self._user and has_auth)

    def _connect(self):
        import snowflake.connector
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

            cur.execute("""
                SELECT
                    AVG(STORAGE_BYTES)   / POWER(1024,4) AS table_tb,
                    AVG(STAGE_BYTES)     / POWER(1024,4) AS stage_tb,
                    AVG(FAILSAFE_BYTES)  / POWER(1024,4) AS failsafe_tb
                FROM SNOWFLAKE.ACCOUNT_USAGE.STORAGE_USAGE
                WHERE USAGE_DATE >= %s AND USAGE_DATE <= %s
            """, (start_date.isoformat(), end_date.isoformat()))
            storage_row = cur.fetchone()
        finally:
            conn.close()

        entries: list[CostEntry] = []
        by_service: dict[str, float] = {}
        total = 0.0

        has_price = self._credit_price is not None

        for row in wh_rows:
            wh_name, credits = row[0], float(row[1] or 0)
            svc = f"Warehouse: {wh_name}"
            if has_price:
                amount = credits * self._credit_price  # type: ignore[operator]
                total += amount
                by_service[svc] = by_service.get(svc, 0.0) + amount
            else:
                # No dollar amount — store credits as metadata, amount=0
                amount = 0.0
                by_service[svc] = 0.0
            entries.append(CostEntry(
                provider="snowflake",
                account_id=self._account,
                account_name=self._account,
                service=svc,
                region="",
                amount=amount,
                metadata={
                    "credits_consumed": credits,
                    "cost_source": "user_contract_rate" if has_price else "not_available",
                    "note": "" if has_price else "Set SNOWFLAKE_CREDIT_PRICE to your contract rate for USD amounts",
                },
            ))

        if storage_row and has_price:
            # Only report storage cost if we have a reliable price signal
            # $23/TB/month is list price — skip if user hasn't confirmed their rate
            # We intentionally leave storage cost out without a user-supplied price
            pass

        meta: dict[str, Any] = {"credits_only": not has_price}
        if not has_price:
            meta["note"] = (
                "No SNOWFLAKE_CREDIT_PRICE set. "
                "Credits consumed are in metadata. Set your contract rate for USD amounts."
            )

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

        Credits consumed ride along in each record's Tags, so the data is complete
        even when no contract credit price is set and the dollar amount is 0.
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
