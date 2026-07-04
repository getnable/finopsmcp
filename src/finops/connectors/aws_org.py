"""
AWS Organizations cost rollup — multi-account FinOps at org scale.

Uses AWS Organizations API to discover all member accounts, then fans out
Cost Explorer queries across each account using STS AssumeRole.

Setup:
  1. You must be running as (or have credentials for) the management account
     (or delegated admin account with Cost Explorer access).
  2. Each member account needs a role that trusts your management account:
       Role name: FinOpsReadOnly  (configurable via FINOPS_ORG_ROLE_NAME)
       Policy: ReadOnlyAccess + ce:Get* + ce:Describe*

  Or, if using AWS Organizations consolidated billing with Cost Explorer
  enabled at the management level, no cross-account roles are needed —
  the management account CE API returns data for all accounts.

Environment variables:
  AWS_PROFILE / AWS_ACCESS_KEY_ID etc.  — standard AWS credentials
  FINOPS_ORG_ROLE_NAME                  — role to assume in member accounts
                                          (default: FinOpsReadOnly)
  FINOPS_ORG_MANAGEMENT_ACCOUNT        — override management account ID
  FINOPS_ORG_MAX_ACCOUNTS              — cap how many accounts to query (default: 200)
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

_ROLE_NAME = os.environ.get("FINOPS_ORG_ROLE_NAME", "FinOpsReadOnly")
_MAX_ACCOUNTS = int(os.environ.get("FINOPS_ORG_MAX_ACCOUNTS", "200"))


# ── AWS client factories ──────────────────────────────────────────────────────

def _org_client() -> Any:
    import boto3
    return boto3.client("organizations", region_name="us-east-1")


def _ce_client(account_id: str | None = None, role_name: str = _ROLE_NAME) -> Any:
    """
    Return a Cost Explorer client. If account_id is given and differs from
    the caller's account, assume the FinOpsReadOnly role in that account.
    """
    import boto3
    if not account_id:
        return boto3.client("ce", region_name="us-east-1")

    # Get caller account to decide if cross-account needed
    try:
        sts = boto3.client("sts")
        caller_account = sts.get_caller_identity()["Account"]
    except Exception:
        return boto3.client("ce", region_name="us-east-1")

    if caller_account == account_id:
        return boto3.client("ce", region_name="us-east-1")

    # Cross-account assume role
    try:
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        sts = boto3.client("sts")
        assumed = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName="nable-finops",
            DurationSeconds=900,
        )
        creds = assumed["Credentials"]
        return boto3.client(
            "ce",
            region_name="us-east-1",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
    except Exception as e:
        log.warning("Could not assume role in %s: %s — falling back to management account CE", account_id, e)
        return boto3.client("ce", region_name="us-east-1")


# ── Organization discovery ────────────────────────────────────────────────────

def list_org_accounts(sync_to_db: bool = True) -> list[dict[str, Any]]:
    """
    List all active accounts in the AWS Organization.
    Optionally syncs discovered accounts to the org_accounts DB table.
    """
    try:
        org = _org_client()
    except Exception as e:
        log.warning("AWS Organizations not available: %s", e)
        return []

    accounts = []
    paginator = org.get_paginator("list_accounts")
    try:
        for page in paginator.paginate():
            for acct in page["Accounts"]:
                if acct["Status"] != "ACTIVE":
                    continue
                accounts.append({
                    "account_id": acct["Id"],
                    "account_name": acct["Name"],
                    "email": acct.get("Email", ""),
                    "status": acct["Status"],
                    "joined_at": acct.get("JoinedTimestamp", ""),
                })
                if len(accounts) >= _MAX_ACCOUNTS:
                    log.info("Hit _MAX_ACCOUNTS cap (%d)", _MAX_ACCOUNTS)
                    break
    except Exception as e:
        log.error("list_accounts failed: %s", e)
        return []

    # Identify management account
    try:
        org_info = org.describe_organization()["Organization"]
        mgmt_id = org_info.get("MasterAccountId", "")
    except Exception:
        mgmt_id = ""

    for acct in accounts:
        acct["is_management_account"] = acct["account_id"] == mgmt_id

    if sync_to_db:
        _sync_accounts_to_db(accounts, mgmt_id)

    return accounts


def _sync_accounts_to_db(accounts: list[dict], mgmt_id: str) -> None:
    """
    Upsert org accounts into the DB.

    Complexity: O(1) queries instead of O(n) — one SELECT to load all existing
    account IDs into a set, then one bulk INSERT for new accounts + one UPDATE
    per changed account (all in a single transaction).
    """
    try:
        from ..storage.db import org_accounts, get_engine
        from sqlalchemy import select, update, insert

        if not accounts:
            return

        now = date.today().isoformat()
        engine = get_engine()

        with engine.begin() as conn:
            # Single query: load all existing AWS account IDs into a set — O(1) round-trip
            existing_ids: set[str] = {
                r.account_id for r in conn.execute(
                    select(org_accounts.c.account_id).where(
                        org_accounts.c.cloud_provider == "aws"
                    )
                ).fetchall()
            }

            new_accounts = [a for a in accounts if a["account_id"] not in existing_ids]
            existing_accounts = [a for a in accounts if a["account_id"] in existing_ids]

            # Batch INSERT all new accounts in one execute() call
            if new_accounts:
                conn.execute(insert(org_accounts), [
                    dict(
                        cloud_provider="aws",
                        account_id=a["account_id"],
                        account_name=a["account_name"],
                        status=a["status"],
                        tags="{}",
                        assume_role_arn=f"arn:aws:iam::{a['account_id']}:role/{_ROLE_NAME}",
                        last_synced=now,
                        is_management_account=a.get("is_management_account", False),
                    )
                    for a in new_accounts
                ])

            # UPDATE existing accounts (name/status may have changed)
            for a in existing_accounts:
                conn.execute(
                    update(org_accounts)
                    .where(org_accounts.c.account_id == a["account_id"])
                    .values(
                        account_name=a["account_name"],
                        status=a["status"],
                        last_synced=now,
                    )
                )

    except Exception as e:
        log.warning("Failed to sync accounts to DB: %s", e)


# ── Cost rollup ───────────────────────────────────────────────────────────────

def _get_date_range(days_back: int = 30) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=days_back)
    return start.isoformat(), end.isoformat()


def _account_spend(
    account_id: str,
    start: str,
    end: str,
    granularity: str = "MONTHLY",
) -> dict[str, Any]:
    """Fetch total spend + top services for one account."""
    try:
        ce = _ce_client(account_id)

        # Total spend with account filter (management CE supports this)
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity=granularity,
            Filter={"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account_id]}},
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            Metrics=["UnblendedCost"],
        )

        total = 0.0
        by_service: dict[str, float] = {}
        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                svc = group["Keys"][0]
                amt = float(group["Metrics"]["UnblendedCost"]["Amount"])
                total += amt
                by_service[svc] = by_service.get(svc, 0) + amt

        top_services = sorted(by_service.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            "account_id": account_id,
            "total_usd": round(total, 2),
            "top_services": [{"service": s, "amount_usd": round(a, 2)} for s, a in top_services],
            "error": None,
        }
    except Exception as e:
        log.warning("Spend fetch failed for %s: %s", account_id, e)
        return {"account_id": account_id, "total_usd": 0.0, "top_services": [], "error": str(e)}


def org_cost_summary(
    days_back: int = 30,
    include_zero_spend: bool = False,
) -> dict[str, Any]:
    """
    Fan out cost queries across all org accounts and return an aggregated
    summary sorted by spend (highest first).

    Read-through cached (12h, like the per-connector cost path). Without this the
    org rollup re-hit Cost Explorer on every call, and top_spending_accounts (which
    delegates here) plus the weekly digest each triggered another full CE query for
    data that refreshes only ~3x/day.
    """
    from .. import cache as _cache
    _ck = _cache.make_key("aws_org.org_cost_summary", days_back, include_zero_spend)
    _hit = _cache.get(_ck)
    if _hit is not None:
        import copy as _copy
        return _copy.deepcopy(_hit)

    result = _org_cost_summary_uncached(days_back, include_zero_spend)
    # Only cache a real rollup, never a transient error payload.
    if isinstance(result, dict) and not result.get("error"):
        import copy as _copy
        _cache.set(_ck, _copy.deepcopy(result), _cache.COST_TTL)
    return result


def _org_cost_summary_uncached(
    days_back: int = 30,
    include_zero_spend: bool = False,
) -> dict[str, Any]:
    start, end = _get_date_range(days_back)

    # Try management account CE first — it can see all accounts without assume-role
    try:
        import boto3
        ce = boto3.client("ce", region_name="us-east-1")
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            GroupBy=[
                {"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"},
                {"Type": "DIMENSION", "Key": "SERVICE"},
            ],
            Metrics=["UnblendedCost"],
        )

        # Aggregate by account
        account_totals: dict[str, float] = {}
        account_services: dict[str, dict[str, float]] = {}
        for period in resp.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                acct_id = group["Keys"][0]
                svc     = group["Keys"][1]
                amt     = float(group["Metrics"]["UnblendedCost"]["Amount"])
                account_totals[acct_id] = account_totals.get(acct_id, 0) + amt
                if acct_id not in account_services:
                    account_services[acct_id] = {}
                account_services[acct_id][svc] = account_services[acct_id].get(svc, 0) + amt

        # Get account names from DB or org API
        account_names = _load_account_names()

        org_total = sum(account_totals.values())
        accounts_out = []
        for acct_id, total in sorted(account_totals.items(), key=lambda x: x[1], reverse=True):
            if not include_zero_spend and total < 0.01:
                continue
            svcs = account_services.get(acct_id, {})
            top5 = sorted(svcs.items(), key=lambda x: x[1], reverse=True)[:5]
            accounts_out.append({
                "account_id": acct_id,
                "account_name": account_names.get(acct_id, acct_id),
                "total_usd": round(total, 2),
                "pct_of_org": round(total / org_total * 100, 1) if org_total else 0,
                "top_services": [{"service": s, "amount_usd": round(a, 2)} for s, a in top5],
            })

        return {
            "period_start": start,
            "period_end": end,
            "org_total_usd": round(org_total, 2),
            "account_count": len(accounts_out),
            "accounts": accounts_out,
            "method": "management_account_ce",
        }

    except Exception as e:
        log.warning("Management CE rollup failed (%s) — falling back to per-account queries", e)
        return _fallback_per_account_rollup(start, end, days_back)


def _fallback_per_account_rollup(start: str, end: str, days_back: int) -> dict[str, Any]:
    """Fall back to per-account CE queries via AssumeRole if management CE fails."""
    accounts = list_org_accounts(sync_to_db=True)
    if not accounts:
        return {"error": "No accounts found in organization", "org_total_usd": 0}

    results = []
    for acct in accounts:
        data = _account_spend(acct["account_id"], start, end)
        data["account_name"] = acct["account_name"]
        results.append(data)

    results.sort(key=lambda x: x["total_usd"], reverse=True)
    org_total = sum(r["total_usd"] for r in results)
    for r in results:
        r["pct_of_org"] = round(r["total_usd"] / org_total * 100, 1) if org_total else 0

    return {
        "period_start": start,
        "period_end": end,
        "org_total_usd": round(org_total, 2),
        "account_count": len(results),
        "accounts": results,
        "method": "per_account_assume_role",
    }


def _load_account_names() -> dict[str, str]:
    """Load account names from DB (populated by list_org_accounts)."""
    try:
        from ..storage.db import org_accounts, get_engine
        from sqlalchemy import select
        with get_engine().connect() as conn:
            rows = conn.execute(select(org_accounts.c.account_id, org_accounts.c.account_name)).fetchall()
        return {r.account_id: r.account_name for r in rows}
    except Exception:
        return {}


def top_spending_accounts(limit: int = 10, days_back: int = 30) -> list[dict[str, Any]]:
    """Return the top N highest-spending accounts in the org."""
    summary = org_cost_summary(days_back=days_back)
    accounts = summary.get("accounts", [])
    return accounts[:limit]


def account_anomalies(days_back: int = 30) -> list[dict[str, Any]]:
    """
    Compare this period's spend to previous period across all accounts.
    Returns accounts with significant spend changes.
    """
    start, end = _get_date_range(days_back)
    prev_start, prev_end = _get_date_range(days_back * 2)
    # prev_end should be start to avoid overlap
    prev_end = start

    try:
        import boto3
        ce = boto3.client("ce", region_name="us-east-1")

        from .. import cache as _cache

        def period_totals(s: str, e: str) -> dict[str, float]:
            # Cache per date window. The previous-period window is stable history and
            # is reused across repeated anomaly checks; the current window refreshes
            # only ~3x/day, so a 12h TTL never costs accuracy.
            _ck = _cache.make_key("aws_org.period_totals", s, e)
            _hit = _cache.get(_ck)
            if _hit is not None:
                return dict(_hit)
            resp = ce.get_cost_and_usage(
                TimePeriod={"Start": s, "End": e},
                Granularity="MONTHLY",
                GroupBy=[{"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"}],
                Metrics=["UnblendedCost"],
            )
            totals: dict[str, float] = {}
            for period in resp.get("ResultsByTime", []):
                for group in period.get("Groups", []):
                    acct = group["Keys"][0]
                    amt  = float(group["Metrics"]["UnblendedCost"]["Amount"])
                    totals[acct] = totals.get(acct, 0) + amt
            _cache.set(_ck, dict(totals), _cache.COST_TTL)
            return totals

        current = period_totals(start, end)
        previous = period_totals(prev_start, prev_end)
        account_names = _load_account_names()

        anomalies = []
        for acct_id, cur_amt in current.items():
            prev_amt = previous.get(acct_id, 0)
            if prev_amt < 10:  # ignore tiny baselines
                continue
            pct_change = (cur_amt - prev_amt) / prev_amt * 100
            if abs(pct_change) >= 20:  # ≥20% change is notable
                anomalies.append({
                    "account_id": acct_id,
                    "account_name": account_names.get(acct_id, acct_id),
                    "current_usd": round(cur_amt, 2),
                    "previous_usd": round(prev_amt, 2),
                    "pct_change": round(pct_change, 1),
                    "direction": "spike" if pct_change > 0 else "drop",
                })

        return sorted(anomalies, key=lambda x: abs(x["pct_change"]), reverse=True)

    except Exception as e:
        log.error("Account anomaly detection failed: %s", e)
        return []


def ou_cost_breakdown(days_back: int = 30) -> list[dict[str, Any]]:
    """
    Break costs down by Organizational Unit (OU). Useful for chargeback
    by business unit when OU = department / team.
    """
    try:
        org = _org_client()
        accounts = list_org_accounts(sync_to_db=False)
        account_to_ou: dict[str, str] = {}

        for acct in accounts:
            try:
                parents = org.list_parents(ChildId=acct["account_id"])["Parents"]
                if parents:
                    ou_id = parents[0]["Id"]
                    # Try to get OU name
                    try:
                        ou_info = org.describe_organizational_unit(OrganizationalUnitId=ou_id)
                        ou_name = ou_info["OrganizationalUnit"]["Name"]
                    except Exception:
                        ou_name = ou_id
                    account_to_ou[acct["account_id"]] = ou_name
            except Exception:
                account_to_ou[acct["account_id"]] = "Root"

        summary = org_cost_summary(days_back=days_back)
        ou_totals: dict[str, float] = {}
        ou_accounts: dict[str, list] = {}

        for acct in summary.get("accounts", []):
            ou = account_to_ou.get(acct["account_id"], "Unknown")
            ou_totals[ou] = ou_totals.get(ou, 0) + acct["total_usd"]
            if ou not in ou_accounts:
                ou_accounts[ou] = []
            ou_accounts[ou].append(acct)

        org_total = summary.get("org_total_usd", 0)
        return sorted([
            {
                "ou_name": ou,
                "total_usd": round(total, 2),
                "pct_of_org": round(total / org_total * 100, 1) if org_total else 0,
                "account_count": len(ou_accounts.get(ou, [])),
                "accounts": ou_accounts.get(ou, []),
            }
            for ou, total in ou_totals.items()
        ], key=lambda x: x["total_usd"], reverse=True)

    except Exception as e:
        log.error("OU breakdown failed: %s", e)
        return []
