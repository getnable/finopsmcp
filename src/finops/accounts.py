"""
Multi-account AWS registry.

Accounts are stored in ~/.finops-mcp/accounts.yaml. Each entry can specify
a cross-account IAM role, an AWS CLI profile, or fall back to default credentials.

Usage:
    from finops.accounts import list_accounts, get_account, get_boto3_session
"""
from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_ACCOUNTS_FILE = Path(os.environ.get("FINOPS_ACCOUNTS_FILE", Path.home() / ".finops-mcp" / "accounts.yaml"))


@dataclass
class AccountConfig:
    name: str
    account_id: str = ""
    region: str = "us-east-1"
    role_arn: str = ""      # cross-account role to assume
    profile: str = ""       # AWS CLI profile name
    tags: dict = field(default_factory=dict)


def _load_yaml() -> dict:
    """Load accounts.yaml, returning an empty structure if it doesn't exist."""
    if not _ACCOUNTS_FILE.exists():
        return {"accounts": [], "default_account": ""}
    try:
        import yaml
        return yaml.safe_load(_ACCOUNTS_FILE.read_text()) or {"accounts": [], "default_account": ""}
    except Exception:
        return {"accounts": [], "default_account": ""}


def _save_yaml(data: dict) -> None:
    _ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml
        _ACCOUNTS_FILE.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))
    except ImportError:
        # yaml not installed — write minimal format manually
        import json
        _ACCOUNTS_FILE.write_text(json.dumps(data, indent=2))
    _ACCOUNTS_FILE.chmod(0o600)


def list_accounts() -> list[AccountConfig]:
    """Return all configured accounts."""
    data = _load_yaml()
    result = []
    for entry in data.get("accounts", []):
        if not isinstance(entry, dict):
            continue
        result.append(AccountConfig(
            name=entry.get("name", ""),
            account_id=entry.get("account_id", ""),
            region=entry.get("region", "us-east-1"),
            role_arn=entry.get("role_arn", ""),
            profile=entry.get("profile", ""),
            tags=entry.get("tags", {}),
        ))
    return result


def get_account(name: str) -> AccountConfig | None:
    """Return a specific account by name, or None if not found."""
    for acct in list_accounts():
        if acct.name == name:
            return acct
    return None


def get_default_account() -> AccountConfig | None:
    """Return the default account, or the first account if no default is set."""
    data = _load_yaml()
    default_name = data.get("default_account", "")
    accounts = list_accounts()
    if not accounts:
        return None
    if default_name:
        for acct in accounts:
            if acct.name == default_name:
                return acct
    return accounts[0]


def add_account(account: AccountConfig) -> None:
    """Add or replace an account entry by name."""
    data = _load_yaml()
    entries = data.get("accounts", [])
    # Replace if name already exists
    entries = [e for e in entries if isinstance(e, dict) and e.get("name") != account.name]
    entry: dict[str, Any] = {"name": account.name}
    if account.account_id:
        entry["account_id"] = account.account_id
    entry["region"] = account.region
    if account.role_arn:
        entry["role_arn"] = account.role_arn
    if account.profile:
        entry["profile"] = account.profile
    if account.tags:
        entry["tags"] = account.tags
    entries.append(entry)
    data["accounts"] = entries
    _save_yaml(data)


def remove_account(name: str) -> bool:
    """Remove an account by name. Returns True if it existed."""
    data = _load_yaml()
    before = len(data.get("accounts", []))
    data["accounts"] = [e for e in data.get("accounts", []) if isinstance(e, dict) and e.get("name") != name]
    if data.get("default_account") == name:
        data["default_account"] = ""
    _save_yaml(data)
    return len(data["accounts"]) < before


def set_default_account(name: str) -> bool:
    """Set the default account. Returns False if the account doesn't exist."""
    if not get_account(name):
        return False
    data = _load_yaml()
    data["default_account"] = name
    _save_yaml(data)
    return True


def get_boto3_session(account: AccountConfig):
    """
    Return a boto3 Session for the given account.

    Priority:
      1. role_arn: assume the cross-account role via STS
      2. profile: use the named AWS CLI profile
      3. fallback: default credential chain
    """
    import boto3

    if account.role_arn:
        sts = boto3.client("sts")
        resp = sts.assume_role(
            RoleArn=account.role_arn,
            RoleSessionName=f"nable-{account.name}-{int(time.time())}",
            DurationSeconds=3600,
        )
        creds = resp["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=account.region,
        )

    if account.profile:
        return boto3.Session(profile_name=account.profile, region_name=account.region)

    return boto3.Session(region_name=account.region)


def discover_org_accounts(add_all: bool = False, role_name: str = "FinOpsReadOnly") -> list[dict]:
    """
    Use AWS Organizations to discover member accounts.

    Returns a list of dicts: {account_id, account_name, email}.
    If add_all is True, all accounts are added to the registry with role assumption.
    """
    import boto3
    org = boto3.client("organizations", region_name="us-east-1")
    accounts = []
    paginator = org.get_paginator("list_accounts")
    for page in paginator.paginate():
        for acct in page["Accounts"]:
            if acct["Status"] != "ACTIVE":
                continue
            accounts.append({
                "account_id": acct["Id"],
                "account_name": acct["Name"],
                "email": acct.get("Email", ""),
            })

    if add_all:
        for acct in accounts:
            cfg = AccountConfig(
                name=acct["account_name"].lower().replace(" ", "-"),
                account_id=acct["account_id"],
                region="us-east-1",
                role_arn=f"arn:aws:iam::{acct['account_id']}:role/{role_name}",
            )
            add_account(cfg)

    return accounts
