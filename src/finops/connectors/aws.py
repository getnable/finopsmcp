from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any

from .base import BaseConnector, CostEntry, CostSummary


class AWSConnector(BaseConnector):
    provider = "aws"

    def __init__(self, session=None) -> None:
        """
        session: optional boto3.Session to use for all calls.
        If not provided, falls back to environment-based role ARNs or default credentials.
        """
        self._session = session  # set when created for a specific account
        self._role_arns: list[str] = [
            arn.strip()
            for arn in os.getenv("AWS_ROLE_ARNS", "").split(",")
            if arn.strip()
        ]
        _MAX_ROLES = int(os.getenv("FINOPS_MAX_ROLE_ARNS", "50"))
        if len(self._role_arns) > _MAX_ROLES:
            import logging as _log
            _log.getLogger("finops.aws").warning(
                "AWS_ROLE_ARNS has %d entries; capping at %d. Set FINOPS_MAX_ROLE_ARNS to override.",
                len(self._role_arns), _MAX_ROLES,
            )
            self._role_arns = self._role_arns[:_MAX_ROLES]

    async def is_configured(self) -> bool:
        try:
            import boto3  # noqa: F401

            if self._session:
                # Validate that the injected session has working credentials
                creds = self._session.get_credentials()
                return creds is not None

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

        # GovCloud note: AWS Cost Explorer is available in GovCloud regions.
        # Valid GovCloud CE endpoints: us-gov-west-1, us-gov-east-1.
        # Set AWS_DEFAULT_REGION or pass region_name to target GovCloud.
        # No code changes needed — boto3 respects the standard region resolution chain.
        _region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

        # If a session was injected (via account registry), use it directly
        if self._session and not role_arn:
            return self._session.client("ce", region_name=_region)

        if role_arn:
            sts = boto3.client("sts")
            assumed = sts.assume_role(RoleArn=role_arn, RoleSessionName="finops-mcp")
            creds = assumed["Credentials"]
            return boto3.client(
                "ce",
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name=_region,
            )
        return boto3.client("ce", region_name=_region)

    def _account_id(self, role_arn: str | None = None) -> str:
        import boto3

        if role_arn:
            return role_arn.split(":")[4]
        if self._session:
            sts = self._session.client("sts")
        else:
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

        from datetime import timedelta
        _earliest = end_date - timedelta(days=int(os.getenv("FINOPS_MAX_LOOKBACK_DAYS", "365")))
        if start_date < _earliest:
            start_date = _earliest

        # Read-through cache: Cost Explorer bills $0.01 per request, and an agentic
        # session re-asks the same cost question repeatedly. Serve a 12h-fresh copy
        # so repeat queries within a conversation cost nothing. CE data only
        # refreshes a few times a day, so this never serves stale numbers.
        import copy as _copy

        from .. import cache as _cache
        _ck = _cache.make_key(
            "aws.get_costs",
            ",".join(sorted(self._role_arns)) if self._role_arns else "default",
            start_date.isoformat(), end_date.isoformat(),
            granularity, ",".join(group_by),
            repr(filters) if filters else "",
        )
        _hit = _cache.get(_ck)
        if _hit is not None:
            # Return an independent copy so a caller mutating the result cannot
            # poison the cached entry for the next call.
            return _copy.deepcopy(_hit)

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
                err_str = str(exc)
                if "DataUnavailableException" in err_code or "DataUnavailableException" in err_str:
                    raise RuntimeError(
                        "AWS Cost Explorer data is not yet available for this account. "
                        "Cost Explorer needs up to 24 hours to backfill data after it is first enabled. "
                        "Try again tomorrow, or check the AWS Console > Billing > Cost Explorer."
                    ) from exc
                if err_code in ("InvalidClientTokenId", "AuthFailure") or "InvalidClientTokenId" in err_str:
                    raise RuntimeError(
                        "AWS credentials are invalid. Check your AWS_ACCESS_KEY_ID and "
                        "AWS_SECRET_ACCESS_KEY, then run: finops setup aws"
                    ) from exc
                if err_code == "AccessDenied" or "AccessDenied" in err_str:
                    raise RuntimeError(
                        "AWS credentials are valid but missing Cost Explorer permissions. "
                        "Add ce:GetCostAndUsage to your IAM policy, or run: finops setup aws --iam-template"
                    ) from exc
                if err_code == "ExpiredTokenException" or "ExpiredToken" in err_str:
                    raise RuntimeError(
                        "AWS session token has expired. Re-run: finops setup aws"
                    ) from exc
                raise

            # If CE returned rows but all costs are zero, this is a free/new account
            # with no actual spend. Flag it so callers can give a clear message
            # instead of misdiagnosing it as a config error.
            if results and all(
                float(r.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0)) == 0.0
                for r in results
            ):
                merged._zero_spend_account = True

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

        # Store a copy so later mutation of the returned object cannot reach the cache.
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
                "line_item_unblended_cost": entry.amount,
                "line_item_blended_cost": entry.amount,
                "pricing_public_on_demand_cost": entry.amount,
                "product_servicename": entry.service,
                "line_item_product_code": entry.service,
                "product_region": entry.region,
                "line_item_usage_account_id": entry.account_id,
                "line_item_line_item_type": "Usage",
                "bill_billing_period_start_date": period_start.isoformat(),
                "bill_billing_period_end_date": period_end.isoformat(),
                "line_item_usage_start_date": period_start.isoformat(),
                "line_item_usage_end_date": period_end.isoformat(),
                "resource_tags": entry.tags,
            }
            records.append(normalize("aws", raw))
        return records

    async def list_accounts(self) -> list[dict[str, str]]:
        if self._role_arns:
            return [
                {"id": arn.split(":")[4], "name": arn.split(":")[4]}
                for arn in self._role_arns
            ]
        account_id = self._account_id()
        return [{"id": account_id, "name": account_id}]
