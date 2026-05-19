from __future__ import annotations

import os
from datetime import date
from typing import Any

from .base import BaseConnector, CostEntry, CostSummary


class AWSConnector(BaseConnector):
    provider = "aws"

    def __init__(self) -> None:
        self._role_arns: list[str] = [
            arn.strip()
            for arn in os.getenv("AWS_ROLE_ARNS", "").split(",")
            if arn.strip()
        ]

    async def is_configured(self) -> bool:
        try:
            import boto3  # noqa: F401

            # Either explicit creds or a role/instance profile must be resolvable
            import botocore.session

            s = botocore.session.get_session()
            creds = s.get_credentials()
            return creds is not None
        except Exception:
            return False

    # ── internal helpers ────────────────────────────────────────────────────

    def _make_client(self, role_arn: str | None = None):
        import boto3

        if role_arn:
            sts = boto3.client("sts")
            assumed = sts.assume_role(RoleArn=role_arn, RoleSessionName="finops-mcp")
            creds = assumed["Credentials"]
            return boto3.client(
                "ce",
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name="us-east-1",
            )
        return boto3.client("ce", region_name="us-east-1")

    def _account_id(self, role_arn: str | None = None) -> str:
        import boto3

        if role_arn:
            return role_arn.split(":")[4]
        sts = boto3.client("sts")
        return sts.get_caller_identity()["Account"]

    def _build_summary(
        self,
        account_id: str,
        start_date: date,
        end_date: date,
        response: dict,
    ) -> CostSummary:
        entries: list[CostEntry] = []
        by_service: dict[str, float] = {}
        by_region: dict[str, float] = {}
        total = 0.0

        for result in response.get("ResultsByTime", []):
            for group in result.get("Groups", []):
                keys = group.get("Keys", [])
                service = keys[0] if keys else "Unknown"
                region = keys[1] if len(keys) > 1 else ""
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                total += amount
                by_service[service] = by_service.get(service, 0.0) + amount
                if region:
                    by_region[region] = by_region.get(region, 0.0) + amount
                entries.append(
                    CostEntry(
                        provider="aws",
                        account_id=account_id,
                        account_name=account_id,
                        service=service,
                        region=region,
                        amount=amount,
                    )
                )

        return CostSummary(
            provider="aws",
            start_date=start_date,
            end_date=end_date,
            total_usd=total,
            by_service=by_service,
            by_account={account_id: total},
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
        group_by = group_by or ["SERVICE"]
        ce_group_by = [{"Type": "DIMENSION", "Key": k} for k in group_by]

        targets = self._role_arns if self._role_arns else [None]
        merged = CostSummary(
            provider="aws",
            start_date=start_date,
            end_date=end_date,
            total_usd=0.0,
            by_service={},
            by_account={},
            by_region={},
            entries=[],
        )

        for role_arn in targets:
            ce = self._make_client(role_arn)
            account_id = self._account_id(role_arn)

            kwargs: dict[str, Any] = dict(
                TimePeriod={
                    "Start": start_date.isoformat(),
                    "End": end_date.isoformat(),
                },
                Granularity=granularity,
                Metrics=["UnblendedCost"],
                GroupBy=ce_group_by,
            )
            if filters:
                kwargs["Filter"] = filters

            # paginate
            results: list[dict] = []
            try:
                while True:
                    resp = ce.get_cost_and_usage(**kwargs)
                    results.extend(resp.get("ResultsByTime", []))
                    token = resp.get("NextPageToken")
                    if not token:
                        break
                    kwargs["NextPageToken"] = token
            except Exception as exc:
                err_code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else type(exc).__name__
                if "DataUnavailableException" in err_code or "DataUnavailableException" in str(exc):
                    raise RuntimeError(
                        "AWS Cost Explorer data is not yet available for this account. "
                        "Cost Explorer needs up to 24 hours to backfill data after it is first enabled. "
                        "Try again tomorrow, or check the AWS Console > Billing > Cost Explorer."
                    ) from exc
                raise

            summary = self._build_summary(
                account_id, start_date, end_date, {"ResultsByTime": results}
            )
            # merge into combined
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
        if self._role_arns:
            return [
                {"id": arn.split(":")[4], "name": arn.split(":")[4]}
                for arn in self._role_arns
            ]
        account_id = self._account_id()
        return [{"id": account_id, "name": account_id}]
