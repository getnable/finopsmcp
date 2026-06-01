from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any

from .base import BaseConnector, CostEntry, CostSummary


class GCPConnector(BaseConnector):
    provider = "gcp"

    def __init__(self) -> None:
        self._billing_account_ids: list[str] = [
            b.strip()
            for b in os.getenv("GCP_BILLING_ACCOUNT_IDS", "").split(",")
            if b.strip()
        ]
        key_path = os.getenv("GCP_SERVICE_ACCOUNT_KEY_PATH")
        if key_path:
            os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", key_path)

    async def is_configured(self) -> bool:
        has_creds = bool(
            os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            or os.getenv("GCP_SERVICE_ACCOUNT_KEY_PATH")
        )
        return has_creds and bool(self._billing_account_ids)

    # ── internal helpers ────────────────────────────────────────────────────

    def _client(self):
        from google.cloud import billing_v1

        return billing_v1.CloudBillingClient()

    def _catalog_client(self):
        from google.cloud import billing_v1

        return billing_v1.CloudCatalogClient()

    def _query_bigquery(self, billing_account_id: str, start_date: date, end_date: date) -> list[dict]:
        """
        Query the BigQuery billing export table.
        Requires: GCP_BQ_BILLING_TABLE env var in the form `project.dataset.table`.
        Falls back to Cloud Billing API summary if not configured.
        """
        bq_table = os.getenv("GCP_BQ_BILLING_TABLE")
        if not bq_table:
            return self._query_billing_api(billing_account_id, start_date, end_date)

        from google.cloud import bigquery

        client = bigquery.Client()
        query = f"""
            SELECT
                service.description AS service,
                location.region AS region,
                -- Net cost = gross cost + credits (credits are stored negative:
                -- committed-use discounts, SUDs, promotions). Summing cost alone
                -- overstates spend by 10-30% and won't match the GCP console.
                SUM(cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS total_cost,
                currency
            FROM `{bq_table}`
            WHERE
                billing_account_id = @billing_account_id
                AND DATE(usage_start_time) >= @start_date
                AND DATE(usage_start_time) <= @end_date
            GROUP BY service, region, currency
            ORDER BY total_cost DESC
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("billing_account_id", "STRING", billing_account_id),
                bigquery.ScalarQueryParameter("start_date", "DATE", start_date.isoformat()),
                bigquery.ScalarQueryParameter("end_date", "DATE", end_date.isoformat()),
            ]
        )
        rows = list(client.query(query, job_config=job_config).result())
        return [dict(row) for row in rows]

    def _query_billing_api(self, billing_account_id: str, start_date: date, end_date: date) -> list[dict]:
        """
        Light fallback using the Cloud Billing SKU catalog.
        Note: The Billing API doesn't expose actual spend — for real spend data
        configure GCP_BQ_BILLING_TABLE (BigQuery billing export).
        """
        return []

    def _rows_to_summary(
        self,
        rows: list[dict],
        billing_account_id: str,
        start_date: date,
        end_date: date,
    ) -> CostSummary:
        entries: list[CostEntry] = []
        by_service: dict[str, float] = {}
        by_region: dict[str, float] = {}
        total = 0.0

        for row in rows:
            service = row.get("service", "Unknown")
            region = row.get("region") or ""
            amount = float(row.get("total_cost", 0))
            total += amount
            by_service[service] = by_service.get(service, 0.0) + amount
            if region:
                by_region[region] = by_region.get(region, 0.0) + amount
            entries.append(
                CostEntry(
                    provider="gcp",
                    account_id=billing_account_id,
                    account_name=billing_account_id,
                    service=service,
                    region=region,
                    amount=amount,
                )
            )

        return CostSummary(
            provider="gcp",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service,
            by_account={billing_account_id: total},
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
            provider="gcp",
            start_date=start_date,
            end_date=end_date,
            total_usd=0.0,
            by_service={},
            by_account={},
            by_region={},
            entries=[],
        )

        for billing_account_id in self._billing_account_ids:
            rows = self._query_bigquery(billing_account_id, start_date, end_date)
            summary = self._rows_to_summary(rows, billing_account_id, start_date, end_date)
            merged.total_usd += summary.total_usd
            for k, v in summary.by_service.items():
                merged.by_service[k] = merged.by_service.get(k, 0.0) + v
            for k, v in summary.by_account.items():
                merged.by_account[k] = merged.by_account.get(k, 0.0) + v
            for k, v in summary.by_region.items():
                merged.by_region[k] = merged.by_region.get(k, 0.0) + v
            merged.entries.extend(summary.entries)

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

        # Derive invoice_month from start_date for BillingPeriod derivation
        invoice_month = f"{start_date.year}{start_date.month:02d}"

        records = []
        for entry in summary.entries:
            raw: dict[str, Any] = {
                "cost": entry.amount,
                "service": {"description": entry.service},
                "location": {"region": entry.region},
                "project": {"id": entry.account_id, "name": entry.account_name},
                "invoice_month": invoice_month,
                "labels": entry.tags,
            }
            records.append(normalize("gcp", raw))
        return records

    async def list_accounts(self) -> list[dict[str, str]]:
        return [{"id": b, "name": b} for b in self._billing_account_ids]
