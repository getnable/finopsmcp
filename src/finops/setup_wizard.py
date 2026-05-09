#!/usr/bin/env python3
"""
finops setup — interactive CLI wizard for configuring providers securely.

Usage:
  finops setup                  # walk through all providers
  finops setup aws              # configure AWS only
  finops setup azure            # configure Azure only
  finops setup gcp              # configure GCP only
  finops setup slack            # configure Slack notifications
  finops setup teams            # configure Teams notifications
  finops setup vault list       # list stored credential keys
  finops setup vault delete KEY # delete a stored credential
  finops setup vault rotate     # rotate the master encryption key
"""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path


def _prompt(msg: str, secret: bool = False, default: str = "") -> str:
    if default:
        msg = f"{msg} [{default}]"
    msg += ": "
    val = getpass.getpass(msg) if secret else input(msg)
    return val.strip() or default


def _section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _warn(msg: str) -> None:
    print(f"  ⚠  {msg}")


def _err(msg: str) -> None:
    print(f"  ✗ {msg}", file=sys.stderr)


# ── Provider wizards ──────────────────────────────────────────────────────────

def setup_aws() -> None:
    _section("AWS — Cost Explorer")
    print("  Choose authentication method:")
    print("  1) IAM Access Key (simple)")
    print("  2) IAM Identity Center / SSO (recommended)")
    choice = _prompt("  Choice", default="1")

    from .security.vault import Vault
    vault = Vault.default()

    if choice == "2":
        start_url = _prompt("  SSO Start URL (e.g. https://myco.awsapps.com/start)")
        region = _prompt("  SSO region", default="us-east-1")
        account_id = _prompt("  AWS account ID to use")
        role_name = _prompt("  IAM role name (e.g. ReadOnlyAccess)")
        from .security.oauth.aws import start_device_flow, poll_for_token, store_sso_credentials
        print("\n  Starting device authorization flow...")
        flow = start_device_flow(start_url, region)
        tokens = poll_for_token(flow)
        store_sso_credentials(tokens, region, account_id, role_name)
    else:
        access_key = _prompt("  AWS Access Key ID")
        secret_key = _prompt("  AWS Secret Access Key", secret=True)
        region = _prompt("  Default region", default="us-east-1")
        role_arns = _prompt("  Role ARNs for additional accounts (comma-separated, or blank)")
        vault.store("AWS_ACCESS_KEY_ID", access_key)
        vault.store("AWS_SECRET_ACCESS_KEY", secret_key)
        vault.store("AWS_DEFAULT_REGION", region)
        if role_arns:
            vault.store("AWS_ROLE_ARNS", role_arns)
        _ok("AWS credentials stored in vault")

    # Test connection
    try:
        import boto3
        vault.load_to_env()
        sts = boto3.client("sts", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
        identity = sts.get_caller_identity()
        _ok(f"Connection verified — account {identity['Account']}")
    except Exception as e:
        _warn(f"Connection test failed: {e}")


def setup_azure() -> None:
    _section("Azure — Cost Management")
    print("  Choose authentication method:")
    print("  1) Service Principal (recommended for production)")
    print("  2) Device code flow (browser required)")
    choice = _prompt("  Choice", default="1")

    sub_ids_raw = _prompt("  Subscription IDs (comma-separated)")
    sub_ids = [s.strip() for s in sub_ids_raw.split(",") if s.strip()]
    tenant_id = _prompt("  Tenant ID")

    from .security.oauth.azure import store_service_principal, start_device_flow, poll_for_token, store_credentials

    if choice == "1":
        client_id = _prompt("  Client (App) ID")
        client_secret = _prompt("  Client Secret", secret=True)
        try:
            store_service_principal(tenant_id, client_id, client_secret, sub_ids)
        except Exception as e:
            _err(f"Failed: {e}")
            return
    else:
        state = start_device_flow(tenant_id)
        result = poll_for_token(state)
        store_credentials(result, tenant_id, sub_ids)


def setup_gcp() -> None:
    _section("GCP — Cloud Billing")
    key_path = _prompt("  Path to service account JSON key file")
    billing_ids_raw = _prompt("  Billing account IDs (comma-separated, format: XXXXXX-XXXXXX-XXXXXX)")
    billing_ids = [b.strip() for b in billing_ids_raw.split(",") if b.strip()]
    bq_table = _prompt("  BigQuery billing export table (optional, e.g. project.dataset.table)", default="")

    from .security.oauth.gcp import import_service_account_key, store_billing_accounts
    try:
        import_service_account_key(key_path)
        store_billing_accounts(billing_ids, bq_table or None)
    except Exception as e:
        _err(f"Failed: {e}")


def setup_saas_api_key(provider_name: str, env_vars: list[tuple[str, str, bool]]) -> None:
    """Generic wizard for API-key SaaS providers."""
    _section(f"{provider_name}")
    from .security.vault import Vault
    vault = Vault.default()
    for env_key, label, is_secret in env_vars:
        val = _prompt(f"  {label}", secret=is_secret)
        if val:
            vault.store(env_key, val)
    _ok(f"{provider_name} credentials stored in vault")


def setup_slack() -> None:
    _section("Slack — Cost Alerts & Daily Digest")
    print("  Choose method:")
    print("  1) Incoming Webhook (simpler)")
    print("  2) Bot Token (richer, supports buttons)")
    choice = _prompt("  Choice", default="1")
    from .security.vault import Vault
    vault = Vault.default()
    if choice == "1":
        url = _prompt("  Webhook URL (from Slack App → Incoming Webhooks)", secret=True)
        vault.store("SLACK_WEBHOOK_URL", url)
    else:
        token = _prompt("  Bot Token (xoxb-...)", secret=True)
        channel = _prompt("  Channel (e.g. #finops-alerts)", default="#finops-alerts")
        vault.store("SLACK_BOT_TOKEN", token)
        vault.store("SLACK_CHANNEL", channel)
    digest_time = _prompt("  Daily digest time (UTC, HH:MM)", default="09:00")
    vault.store("FINOPS_DIGEST_CRON", f"{digest_time.split(':')[1]} {digest_time.split(':')[0]} * * *")
    _ok("Slack configured")


def setup_teams() -> None:
    _section("Microsoft Teams — Cost Alerts & Daily Digest")
    from .security.vault import Vault
    vault = Vault.default()
    url = _prompt("  Incoming Webhook URL (from Teams channel → Connectors)", secret=True)
    vault.store("TEAMS_WEBHOOK_URL", url)
    digest_time = _prompt("  Daily digest time (UTC, HH:MM)", default="09:00")
    vault.store("FINOPS_DIGEST_CRON", f"{digest_time.split(':')[1]} {digest_time.split(':')[0]} * * *")
    _ok("Teams configured")


# ── Vault management ──────────────────────────────────────────────────────────

def vault_list() -> None:
    from .security.vault import Vault
    vault = Vault.default()
    keys = vault.list_keys()
    if not keys:
        print("  Vault is empty")
        return
    print(f"\n  {len(keys)} stored credential(s):\n")
    for k in keys:
        print(f"    • {k}")


def vault_delete(key_name: str) -> None:
    from .security.vault import Vault
    vault = Vault.default()
    if vault.delete(key_name):
        _ok(f"Deleted: {key_name}")
    else:
        _err(f"Key not found: {key_name}")


def vault_rotate() -> None:
    _section("Vault Key Rotation")
    print("  This re-encrypts all credentials with a new master key.")
    print("  The old key will be replaced in the OS keyring and/or key file.")
    confirm = _prompt("  Type 'rotate' to confirm")
    if confirm != "rotate":
        print("  Cancelled.")
        return
    from cryptography.fernet import Fernet
    from .security.vault import Vault
    new_key = Fernet.generate_key()
    vault = Vault.default()
    vault.rotate_key(new_key)
    # Save new key
    if not vault._save_keyring(new_key):
        key_path = Path(os.environ.get("FINOPS_DATA_DIR", Path.home() / ".finops")) / "vault.key"
        key_path.write_bytes(new_key)
        import stat
        key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    _ok("Key rotation complete")


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main(args: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="finops setup", description="FinOps MCP provider configuration wizard")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("aws")
    sub.add_parser("azure")
    sub.add_parser("gcp")
    sub.add_parser("datadog")
    sub.add_parser("snowflake")
    sub.add_parser("github")
    sub.add_parser("stripe")
    sub.add_parser("mongodb")
    sub.add_parser("twilio")
    sub.add_parser("cloudflare")
    sub.add_parser("vercel")
    sub.add_parser("slack")
    sub.add_parser("teams")

    vault_p = sub.add_parser("vault")
    vault_p.add_argument("action", choices=["list", "delete", "rotate"])
    vault_p.add_argument("key", nargs="?", default="")

    parsed = parser.parse_args(args)

    print("\n  ╔═══════════════════════════════════╗")
    print("  ║   FinOps MCP — Setup Wizard       ║")
    print("  ╚═══════════════════════════════════╝")
    print("  All credentials are encrypted in ~/.finops/vault.db\n")

    dispatch = {
        "aws": setup_aws,
        "azure": setup_azure,
        "gcp": setup_gcp,
        "slack": setup_slack,
        "teams": setup_teams,
        "datadog": lambda: setup_saas_api_key("Datadog", [
            ("DATADOG_API_KEY", "API Key", True),
            ("DATADOG_APP_KEY", "Application Key", True),
            ("DATADOG_SITE", "Site (datadoghq.com or datadoghq.eu)", False),
        ]),
        "snowflake": lambda: setup_saas_api_key("Snowflake", [
            ("SNOWFLAKE_ACCOUNT", "Account identifier (e.g. xy12345.us-east-1)", False),
            ("SNOWFLAKE_USER", "Username", False),
            ("SNOWFLAKE_PASSWORD", "Password", True),
            ("SNOWFLAKE_ROLE", "Role (default: ACCOUNTADMIN)", False),
            ("SNOWFLAKE_CREDIT_PRICE", "Credit price USD (your contract rate, optional)", False),
        ]),
        "github": lambda: setup_saas_api_key("GitHub", [
            ("GITHUB_TOKEN", "Personal Access Token (github_pat_...)", True),
            ("GITHUB_ORGS", "Organization names (comma-separated)", False),
        ]),
        "stripe": lambda: setup_saas_api_key("Stripe", [
            ("STRIPE_SECRET_KEY", "Secret Key (sk_live_...)", True),
        ]),
        "mongodb": lambda: setup_saas_api_key("MongoDB Atlas", [
            ("MONGODB_ATLAS_PUBLIC_KEY", "Public Key", False),
            ("MONGODB_ATLAS_PRIVATE_KEY", "Private Key", True),
            ("MONGODB_ATLAS_ORG_IDS", "Organization IDs (comma-separated)", False),
        ]),
        "twilio": lambda: setup_saas_api_key("Twilio", [
            ("TWILIO_ACCOUNT_SID", "Account SID (ACxxxx...)", False),
            ("TWILIO_AUTH_TOKEN", "Auth Token", True),
        ]),
        "cloudflare": lambda: setup_saas_api_key("Cloudflare", [
            ("CLOUDFLARE_API_TOKEN", "API Token", True),
            ("CLOUDFLARE_ACCOUNT_ID", "Account ID", False),
        ]),
        "vercel": lambda: setup_saas_api_key("Vercel", [
            ("VERCEL_TOKEN", "Access Token", True),
            ("VERCEL_TEAM_ID", "Team ID (optional, blank for personal)", False),
        ]),
    }

    if parsed.cmd == "vault":
        if parsed.action == "list":
            vault_list()
        elif parsed.action == "delete":
            if not parsed.key:
                _err("Specify a key name: finops setup vault delete KEY")
            else:
                vault_delete(parsed.key)
        elif parsed.action == "rotate":
            vault_rotate()
    elif parsed.cmd in dispatch:
        dispatch[parsed.cmd]()
    else:
        # Interactive full setup
        providers = ["aws", "azure", "gcp", "datadog", "snowflake", "github", "stripe", "mongodb", "twilio", "cloudflare", "vercel", "slack", "teams"]
        print("  Which providers would you like to configure?")
        for i, p in enumerate(providers, 1):
            print(f"  {i:2d}) {p}")
        raw = _prompt("\n  Enter numbers (comma-separated) or 'all'", default="all")
        if raw.lower() == "all":
            selected = providers
        else:
            indices = [int(x.strip()) - 1 for x in raw.split(",") if x.strip().isdigit()]
            selected = [providers[i] for i in indices if 0 <= i < len(providers)]
        for p in selected:
            try:
                dispatch[p]()
            except KeyboardInterrupt:
                print("\n  Skipped.")

    print("\n  Done. Run 'finops-mcp' to start the MCP server.\n")


if __name__ == "__main__":
    main(sys.argv[1:])
