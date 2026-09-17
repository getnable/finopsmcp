from __future__ import annotations

import os
import re
from datetime import date
from typing import Any

from ..base import BaseConnector, CostEntry, CostSummary

_PEM_RE = re.compile(
    r"-----BEGIN (?P<label>[A-Z ]+)-----(?P<body>.*?)-----END (?P=label)-----", re.S)


def normalize_pem(text: str) -> str:
    """A PEM as the parser wants it, from a PEM as a paste delivers it.

    A key copied out of a terminal or through a one-line input arrives with
    its line breaks turned into spaces, or as literal backslash-n pairs from
    a JSON or .env file. The key is the same; only the wrapping moved. This
    puts the header and footer on their own lines and rewraps the base64 at
    64 columns. Text with no PEM armor is returned as-is, so the parser can
    name the problem.
    """
    text = text.strip().replace("\\n", "\n")
    m = _PEM_RE.search(text)
    if not m:
        return text
    body = "".join(m.group("body").split())
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    label = m.group("label")
    return "\n".join([f"-----BEGIN {label}-----", *lines, f"-----END {label}-----"]) + "\n"


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
        # No role means the user's default role. Forcing ACCOUNTADMIN here
        # made every setup guide say "use ACCOUNTADMIN", when a role with
        # IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE is all the reads need.
        self._role = os.getenv("SNOWFLAKE_ROLE", "")
        # Two ways to hand over the key. The path suits a laptop running the
        # CLI. The body suits a browser paste into a hosted box, which has no
        # file of yours on its disk. The body wins when both are set.
        self._private_key_path = os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH", "")
        self._private_key = os.getenv("SNOWFLAKE_PRIVATE_KEY", "")
        # Only set if the user knows their actual contract rate
        raw = os.getenv("SNOWFLAKE_CREDIT_PRICE", "")
        self._credit_price: float | None = float(raw) if raw else None

    async def is_configured(self) -> bool:
        has_auth = bool(self._password or self._private_key_path or self._private_key)
        return bool(self._account and self._user and has_auth)

    def _connect(self):
        try:
            import snowflake.connector
        except ImportError as e:
            raise RuntimeError(
                "Snowflake support needs an extra dependency. "
                "Run: pip install 'finops-mcp[snowflake]'"
            ) from e
        kwargs: dict[str, Any] = dict(account=self._account, user=self._user)
        if self._role:
            kwargs["role"] = self._role
        if self._warehouse:
            kwargs["warehouse"] = self._warehouse
        pem = self._private_key_pem()
        if pem is not None:
            kwargs["private_key"] = self._der_from_pem(pem)
        else:
            kwargs["password"] = self._password
        return snowflake.connector.connect(**kwargs)

    def _private_key_pem(self) -> bytes | None:
        """The PEM bytes, from the body if set, else the path, else None."""
        if self._private_key.strip():
            return normalize_pem(self._private_key).encode()
        if self._private_key_path:
            with open(self._private_key_path, "rb") as f:
                return f.read()
        return None

    @staticmethod
    def _der_from_pem(pem: bytes) -> bytes:
        """PKCS#8 DER, the form the Snowflake driver signs its JWT with.

        A parse failure is a ValueError naming the problem: the driver would
        otherwise report an opaque auth failure for what is really a bad
        paste, and a hosted probe needs to tell those two apart.
        """
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.serialization import (
            Encoding, NoEncryption, PrivateFormat, load_pem_private_key,
        )
        try:
            pk = load_pem_private_key(pem, password=None, backend=default_backend())
        except TypeError as e:
            raise ValueError(
                "the private key is encrypted; nable needs an unencrypted PKCS#8 key "
                "(openssl pkcs8 -topk8 -nocrypt)") from e
        except Exception as e:
            raise ValueError(f"the private key did not parse as PEM ({e})") from e
        return pk.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())

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
