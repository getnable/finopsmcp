#!/usr/bin/env python3
"""
finops setup: interactive CLI wizard for configuring providers securely.

Usage:
  finops setup                        # walk through all providers
  finops setup aws                    # configure AWS only
  finops setup aws --iam-template     # print least-privilege IAM CloudFormation
  finops setup aws --iam-terraform    # print least-privilege IAM Terraform snippet
  finops setup aws --check-scope      # verify your AWS key is read-only
  finops setup azure                  # configure Azure only
  finops setup gcp                    # configure GCP only
  finops setup slack                  # configure Slack notifications
  finops setup teams                  # configure Teams notifications
  finops setup sso                    # configure enterprise SSO (OIDC / Okta / Azure AD)
  finops setup vault list             # list stored credential keys
  finops setup vault delete KEY       # delete a stored credential
  finops setup vault rotate           # rotate the master encryption key
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

_VALID_AWS_REGIONS = {
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-west-1", "eu-west-2", "eu-west-3", "eu-central-1", "eu-north-1",
    "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "ap-northeast-2",
    "ap-south-1", "ca-central-1", "sa-east-1", "me-south-1", "af-south-1",
}


def setup_aws() -> None:
    _section("AWS: Cost Explorer")

    print("""  Before entering credentials, make sure your IAM user or role has
  these permissions. Without them, cost queries will fail.

  Required IAM permissions:
    ce:GetCostAndUsage
    ce:GetCostForecast
    ce:GetDimensionValues
    sts:GetCallerIdentity

  Quickest way to add them:
    1. Open https://console.aws.amazon.com/iam
    2. Go to Users → your user → Add permissions → Attach policies
    3. Search for "AWSBillingReadOnlyAccess" and attach it
       (or attach a custom policy with just the ce:* actions above)

  Already done? Press Enter to continue.
""")
    input("  → ")

    print("  Choose authentication method:")
    print("  1) IAM Access Key (simple, works for personal accounts)")
    print("  2) IAM Identity Center / SSO (recommended for teams)")
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
        print("""
  Create an access key:
    1. IAM → Users → your user → Security credentials
    2. Access keys → Create access key → choose "Other" → Create
    3. Copy both values below (the secret is only shown once)
""")
        access_key = _prompt("  AWS Access Key ID (starts with AKIA...)")
        while not access_key.startswith("AK") or len(access_key) < 16:
            _warn("That doesn't look like a valid Access Key ID (should start with AKIA and be 20 chars)")
            access_key = _prompt("  AWS Access Key ID")

        secret_key = _prompt("  AWS Secret Access Key", secret=True)
        while len(secret_key) < 20:
            _warn("That doesn't look like a valid Secret Access Key")
            secret_key = _prompt("  AWS Secret Access Key", secret=True)

        # Region with validation
        while True:
            region = _prompt("  AWS region (press Enter for us-east-1)", default="us-east-1")
            if region in _VALID_AWS_REGIONS:
                break
            _warn(f"'{region}' is not a valid AWS region. Examples: us-east-1, us-west-2, eu-west-1")

        role_arns = _prompt("  Role ARNs for additional accounts (comma-separated, or blank)")
        vault.store("AWS_ACCESS_KEY_ID", access_key)
        vault.store("AWS_SECRET_ACCESS_KEY", secret_key)
        vault.store("AWS_DEFAULT_REGION", region)
        if role_arns:
            vault.store("AWS_ROLE_ARNS", role_arns)
        _ok("AWS credentials stored in vault")

    # Test connection + Cost Explorer permissions
    try:
        import boto3
        from datetime import date, timedelta
        vault.load_to_env()
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

        sts = boto3.client("sts", region_name=region)
        identity = sts.get_caller_identity()
        _ok(f"Connection verified: account {identity['Account']}")

        # Check Cost Explorer access specifically — this is the core permission
        ce_ok = False
        try:
            ce = boto3.client("ce", region_name="us-east-1")
            end = date.today()
            start = end - timedelta(days=1)
            ce.get_cost_and_usage(
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                Granularity="DAILY",
                Metrics=["UnblendedCost"],
            )
            _ok("Cost Explorer access confirmed")
            ce_ok = True
        except Exception as ce_err:
            err = str(ce_err)
            if "AccessDenied" in err or "AuthFailure" in err:
                print()
                _warn("This key is missing ce:GetCostAndUsage. Cost queries will fail.")
                print("""
  Add this inline policy to your IAM user or role:

  {
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": [
        "ce:GetCostAndUsage",
        "ce:GetCostForecast",
        "ce:GetDimensionValues"
      ],
      "Resource": "*"
    }]
  }

  Or run: finops setup aws --iam-template
  to generate a full least-privilege CloudFormation template.
""")
            elif "DataUnavailableException" in err:
                _warn("Cost Explorer enabled but data not ready yet. AWS takes up to 24h to backfill. Try again tomorrow.")
                ce_ok = True  # credentials work, data just not ready yet

        try:
            from . import telemetry as _tel
            _tel._send_event(_tel._get_install_id(), "provider_connected", {
                "provider": "aws",
                "ce_access": ce_ok,
                "auth_method": "sso" if choice == "2" else "access_key",
            })
        except Exception:
            pass

    except Exception as e:
        _warn(f"Connection test failed: {e}")
        try:
            from . import telemetry as _tel
            # Capture boto3 ClientError code (e.g. InvalidClientTokenId, AccessDenied)
            # for plain exceptions just use the class name
            error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "") if hasattr(e, "response") else ""
            _tel._send_event(_tel._get_install_id(), "provider_connect_failed", {
                "provider": "aws",
                "error_type": type(e).__name__,
                "error_code": error_code or type(e).__name__,
            })
        except Exception:
            pass


def setup_azure() -> None:
    _section("Azure: Cost Management")
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
            try:
                from . import telemetry as _tel
                _tel._send_event(_tel._get_install_id(), "provider_connect_failed", {
                    "provider": "azure",
                    "error_type": type(e).__name__,
                })
            except Exception:
                pass
            return
    else:
        state = start_device_flow(tenant_id)
        result = poll_for_token(state)
        store_credentials(result, tenant_id, sub_ids)

    _ok("Azure credentials stored")
    try:
        from . import telemetry as _tel
        _tel._send_event(_tel._get_install_id(), "provider_connected", {
            "provider": "azure",
            "auth_method": "service_principal" if choice == "1" else "device_flow",
            "subscription_count": len(sub_ids),
        })
    except Exception:
        pass


def setup_gcp() -> None:
    _section("GCP: Cloud Billing")
    key_path = _prompt("  Path to service account JSON key file")
    billing_ids_raw = _prompt("  Billing account IDs (comma-separated, format: XXXXXX-XXXXXX-XXXXXX)")
    billing_ids = [b.strip() for b in billing_ids_raw.split(",") if b.strip()]
    bq_table = _prompt("  BigQuery billing export table (optional, e.g. project.dataset.table)", default="")

    from .security.oauth.gcp import import_service_account_key, store_billing_accounts
    try:
        import_service_account_key(key_path)
        store_billing_accounts(billing_ids, bq_table or None)
        _ok("GCP credentials stored")
        try:
            from . import telemetry as _tel
            _tel._send_event(_tel._get_install_id(), "provider_connected", {
                "provider": "gcp",
                "billing_account_count": len(billing_ids),
                "has_bq_export": bool(bq_table),
            })
        except Exception:
            pass
    except Exception as e:
        _err(f"Failed: {e}")
        try:
            from . import telemetry as _tel
            _tel._send_event(_tel._get_install_id(), "provider_connect_failed", {
                "provider": "gcp",
                "error_type": type(e).__name__,
            })
        except Exception:
            pass


def setup_saas_api_key(provider_name: str, env_vars: list[tuple[str, str, bool]]) -> None:
    """Generic wizard for API-key SaaS providers."""
    _section(f"{provider_name}")
    from .security.vault import Vault
    vault = Vault.default()
    stored_any = False
    for env_key, label, is_secret in env_vars:
        val = _prompt(f"  {label}", secret=is_secret)
        if val:
            vault.store(env_key, val)
            stored_any = True
    _ok(f"{provider_name} credentials stored in vault")
    if stored_any:
        try:
            from . import telemetry as _tel
            _tel._send_event(_tel._get_install_id(), "provider_connected", {
                "provider": provider_name.lower().replace(" ", "_"),
            })
        except Exception:
            pass


def setup_sso() -> None:
    """Enterprise SSO wizard — configures OIDC for Okta, Azure AD, Google Workspace, etc."""
    _section("Enterprise SSO: OIDC Configuration")
    print("""
  nable supports SSO via OIDC (OpenID Connect).
  Compatible with: Okta, Azure AD / Entra ID, Google Workspace, Auth0, Ping Identity.

  Before running this wizard:
    1. Create an OAuth2 app in your IdP
    2. Set the redirect URI to: https://getnable.com/api/sso/oidc-callback
    3. Enable the "groups" claim in the ID token (steps vary by IdP, see docs)
""")
    from .security.vault import Vault
    vault = Vault.default()

    issuer = _prompt("  OIDC Issuer URL (e.g. https://company.okta.com, https://login.microsoftonline.com/<tenant-id>/v2.0)")
    client_id = _prompt("  Client ID")
    client_secret = _prompt("  Client Secret", secret=True)
    redirect_uri = _prompt(
        "  Redirect URI",
        default="https://getnable.com/api/sso/oidc-callback",
    )
    groups_claim = _prompt("  Groups claim name in JWT", default="groups")
    default_role = _prompt("  Default role for users not in any mapped group (viewer/analyst/admin)", default="viewer")

    print("""
  Map IdP groups to nable roles. Enter group names exactly as they appear in your IdP.
  Press Enter to skip a role (those users will get the default role above).
""")
    admin_groups = _prompt("  Group names for admin role (comma-separated, or blank)")
    analyst_groups = _prompt("  Group names for analyst role (comma-separated, or blank)")
    viewer_groups = _prompt("  Group names for viewer role (comma-separated, or blank)")

    # Build OIDC_ROLE_MAP JSON
    import json
    role_map: dict[str, str] = {}
    for g in [x.strip() for x in admin_groups.split(",") if x.strip()]:
        role_map[g] = "admin"
    for g in [x.strip() for x in analyst_groups.split(",") if x.strip()]:
        role_map[g] = "analyst"
    for g in [x.strip() for x in viewer_groups.split(",") if x.strip()]:
        role_map[g] = "viewer"

    if issuer:
        vault.store("OIDC_ISSUER", issuer)
    if client_id:
        vault.store("OIDC_CLIENT_ID", client_id)
    if client_secret:
        vault.store("OIDC_CLIENT_SECRET", client_secret)
    if redirect_uri:
        vault.store("OIDC_REDIRECT_URI", redirect_uri)
    if groups_claim:
        vault.store("OIDC_GROUPS_CLAIM", groups_claim)
    if default_role:
        vault.store("OIDC_DEFAULT_ROLE", default_role)
    if role_map:
        vault.store("OIDC_ROLE_MAP", json.dumps(role_map))
    vault.store("OIDC_PLAN", "pro")

    _ok("SSO configuration stored in vault")
    print(f"""
  Role map configured: {json.dumps(role_map, indent=4) if role_map else "(none: all SSO users → {default_role})"}

  Next steps:
    1. Export these env vars to your Vercel project:
         OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET
         OIDC_REDIRECT_URI, OIDC_GROUPS_CLAIM, OIDC_ROLE_MAP, OIDC_DEFAULT_ROLE
    2. Test the flow: https://getnable.com/api/sso/oidc-start
    3. Users in your IdP will automatically receive a Team license key on first login.

  For Azure AD: set OIDC_GROUPS_CLAIM=roles (not "groups") and assign app roles in the manifest.
  For Okta: set OIDC_GROUPS_CLAIM=groups and add the Groups claim to your authorization server.
""")


def setup_slack() -> None:
    _section("Slack: Cost Alerts and Daily Digest")
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
    _section("Microsoft Teams: Cost Alerts and Daily Digest")
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
    import sys as _sys
    if args is None:
        args = _sys.argv[1:]

    # Allow `finops setup` and `finops setup aws` as aliases —
    # strip the leading "setup" so both `finops aws` and `finops setup aws` work.
    if args and args[0] == "setup":
        args = args[1:]

    from .welcome import show_welcome
    show_welcome()

    # Track setup wizard start
    try:
        from . import telemetry as _tel
        _tel._send_event(_tel._get_install_id(), "setup_wizard_started", {
            "subcommand": args[0] if args else "interactive",
        })
    except Exception:
        pass

    import argparse
    import logging
    # Silence noisy third-party loggers during interactive setup
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(prog="finops setup", description="FinOps MCP provider configuration wizard")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("aws")
    sub.add_parser("azure")
    sub.add_parser("gcp")
    sub.add_parser("datadog")
    sub.add_parser("langfuse")
    sub.add_parser("snowflake")
    sub.add_parser("github")
    sub.add_parser("stripe")
    sub.add_parser("mongodb")
    sub.add_parser("twilio")
    sub.add_parser("cloudflare")
    sub.add_parser("vercel")
    sub.add_parser("slack")
    sub.add_parser("teams")
    sub.add_parser("sso")
    sub.add_parser("openai")
    sub.add_parser("anthropic")
    sub.add_parser("cohere")
    sub.add_parser("mistral")
    sub.add_parser("newrelic")
    sub.add_parser("pagerduty")
    sub.add_parser("claude")    # configure Claude Desktop MCP entry
    sub.add_parser("aws-cur")   # deploy CUR CloudFormation stack
    infra_p = sub.add_parser("infra")  # overview of all connector setup packages
    infra_p.add_argument("provider", nargs="?", default="", help="Show setup for a specific provider")

    iam_p = sub.add_parser("iam-template")
    iam_p.add_argument("action", choices=["terraform", "cloudformation"], nargs="?", default="cloudformation")

    vault_p = sub.add_parser("vault")
    vault_p.add_argument("action", choices=["list", "delete", "rotate"])
    vault_p.add_argument("key", nargs="?", default="")

    parsed = parser.parse_args(args)
    # Ensure optional attrs exist for all subparsers (only `vault` defines `action`/`key`)
    if not hasattr(parsed, "action"):
        parsed.action = None
    if not hasattr(parsed, "key"):
        parsed.key = ""

    print("\n  nable setup: all credentials stay on your machine\n")

    dispatch = {
        "aws": setup_aws,
        "azure": setup_azure,
        "gcp": setup_gcp,
        "slack": setup_slack,
        "teams": setup_teams,
        "sso": setup_sso,
        "datadog": lambda: setup_saas_api_key("Datadog", [
            ("DATADOG_API_KEY", "API Key", True),
            ("DATADOG_APP_KEY", "Application Key", True),
            ("DATADOG_SITE", "Site (datadoghq.com or datadoghq.eu)", False),
        ]),
        "langfuse": lambda: setup_saas_api_key("Langfuse", [
            ("LANGFUSE_PUBLIC_KEY", "Public Key (pk-lf-...)", True),
            ("LANGFUSE_SECRET_KEY", "Secret Key (sk-lf-...)", True),
            ("LANGFUSE_HOST", "Host URL (leave blank for cloud.langfuse.com)", False),
        ]),
        "snowflake": lambda: setup_saas_api_key("Snowflake", [
            ("SNOWFLAKE_ACCOUNT", "Account identifier (e.g. xy12345.us-east-1)", False),
            ("SNOWFLAKE_USER", "Username", False),
            ("SNOWFLAKE_PASSWORD", "Password", True),
            ("SNOWFLAKE_WAREHOUSE", "Warehouse name (e.g. COMPUTE_WH)", False),
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
        # ── AI / LLM providers ────────────────────────────────────────────────
        "openai": lambda: setup_saas_api_key("OpenAI", [
            ("OPENAI_API_KEY", "API Key (sk-...)", True),
            ("OPENAI_ADMIN_KEY", "Admin/Org Key for billing data (sk-admin-..., optional)", True),
            ("OPENAI_ORG_ID", "Organization ID (org-..., optional)", False),
        ]),
        "anthropic": lambda: setup_saas_api_key("Anthropic", [
            ("ANTHROPIC_API_KEY", "API Key (sk-ant-...)", True),
            ("ANTHROPIC_ADMIN_KEY", "Admin Key for org usage data (optional)", True),
            ("ANTHROPIC_ORGANIZATION_ID", "Organization ID (optional)", False),
        ]),
        "cohere": lambda: setup_saas_api_key("Cohere", [
            ("COHERE_API_KEY", "API Key", True),
        ]),
        "mistral": lambda: setup_saas_api_key("Mistral AI", [
            ("MISTRAL_API_KEY", "API Key", True),
        ]),
        "newrelic": lambda: setup_saas_api_key("New Relic", [
            ("NEW_RELIC_API_KEY", "API Key (NRAK-...)", True),
            ("NEW_RELIC_ACCOUNT_ID", "Account ID", False),
        ]),
        "pagerduty": lambda: setup_saas_api_key("PagerDuty", [
            ("PAGERDUTY_API_KEY", "API Key", True),
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
        return
    elif parsed.cmd == "iam-template":
        # Standalone alias: finops setup iam-template
        from .security.iam_setup import print_iam_template
        fmt = "terraform" if (parsed.action == "terraform") else "cloudformation"
        print_iam_template(fmt)
        return
    elif parsed.cmd == "aws" and parsed.action in ("--iam-template", "iam-template"):
        from .security.iam_setup import print_iam_template
        print_iam_template("cloudformation")
        return
    elif parsed.cmd == "aws" and parsed.action in ("--iam-terraform", "iam-terraform"):
        from .security.iam_setup import print_iam_template
        print_iam_template("terraform")
        return
    elif parsed.cmd == "aws" and parsed.action in ("--check-scope", "check-scope"):
        from .security.iam_setup import check_credential_scope
        print("\n  Checking AWS credential scope...\n")
        result = check_credential_scope()
        if "error" in result:
            _err(result["error"])
            return
        print(f"  Account:      {result['account_id']}")
        print(f"  Identity:     {result['identity_arn']}")
        print(f"  Correctly scoped: {'✓ Yes' if result['scoped_correctly'] else '✗ No'}")
        if result.get("required_denied"):
            print(f"\n  Missing permissions ({len(result['required_denied'])}):")
            for a in result["required_denied"]:
                print(f"    ✗ {a}")
        if result.get("dangerous_allowed"):
            print(f"\n  ⚠ Over-provisioned: write permissions detected:")
            for a in result["dangerous_allowed"]:
                print(f"    ⚠ {a}")
            print()
            print("  Run `finops setup aws --iam-template` to generate a scoped policy.")
        elif not result.get("required_denied"):
            print("\n  ✓ Credentials are read-only and correctly scoped for nable.")
        print()
        return
    elif parsed.cmd == "claude":
        _configure_claude_desktop()
        return
    elif parsed.cmd == "aws-cur":
        _run_aws_cur_setup()
        return
    elif parsed.cmd == "infra":
        _run_infra_overview(getattr(parsed, "provider", ""))
        return
    elif parsed.cmd in dispatch:
        dispatch[parsed.cmd]()
    else:
        # Interactive full setup
        providers = ["aws", "azure", "gcp", "openai", "anthropic", "datadog", "langfuse", "snowflake", "github", "stripe", "mongodb", "twilio", "cloudflare", "vercel", "cohere", "mistral", "newrelic", "pagerduty", "slack", "teams"]
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

    # Always offer to configure Claude Desktop at the end of setup
    _configure_claude_desktop()

    print("\n  Done. Restart Claude Desktop and ask: 'What are my AWS costs this month?'")
    print("  To add more providers later: finops setup")
    print("  Full docs: https://getnable.com/docs\n")
    _offer_email_signup()

    # Fire setup_completed event
    try:
        from . import telemetry as _tel
        _tel._send_event(_tel._get_install_id(), "setup_completed", {
            "subcommand": args[0] if args else "interactive",
        })
    except Exception:
        pass


# ── Post-setup email capture ──────────────────────────────────────────────────

def _offer_email_signup() -> None:
    """
    Offer a free weekly cost digest and capture email for follow-up.
    Non-blocking: any error is silently skipped.
    """
    # Skip if already captured in this session or previously declined
    sentinel = Path.home() / ".config" / "finops" / ".email_captured"
    if sentinel.exists():
        return

    print("─" * 60)
    print("  Get a free weekly cost digest in your inbox.")
    print("  nable emails you every Monday with your top spend drivers,")
    print("  anomalies, and rightsizing opportunities.")
    print()
    try:
        email = input("  Your email (Enter to skip): ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return

    if not email or "@" not in email:
        # User skipped — mark so we don't ask again this install
        try:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text("skipped\n")
        except Exception:
            pass
        return

    try:
        import urllib.request
        import json as _json
        req = urllib.request.Request(
            "https://getnable.com/api/subscribe",
            data=_json.dumps({"email": email, "source": "setup_wizard"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        _ok(f"Subscribed. First digest lands Monday.")
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(f"{email}\n")
    except Exception:
        # Don't block setup if the request fails
        _ok("Got it. We'll be in touch.")
        try:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text(f"{email}\n")
        except Exception:
            pass

    print()


# ── Claude Desktop auto-configuration ─────────────────────────────────────────

def _run_aws_cur_setup() -> None:
    """
    Interactive AWS CUR CloudFormation deployment.
    Called by: finops setup aws-cur
    """
    from .setup.cloud_infra import setup_aws_cur
    from .security.vault import Vault

    env_vars = setup_aws_cur()
    if not env_vars:
        return

    # Offer to store the outputs in the nable vault
    store = _prompt(
        "\n  Store these in the nable credential vault? [Y/n]", default="y"
    ).lower()
    if store in ("y", "yes", ""):
        vault = Vault.default()
        for key, val in env_vars.items():
            if val:
                vault.store(key, val)
        print("\n  ✓  Stored in vault. CUR line-item queries are now available.")
        print("  ✓  Restart Claude Desktop to pick up the new env vars.\n")

        try:
            from . import telemetry as _tel
            _tel._send_event(_tel._get_install_id(), "provider_connected", {
                "provider": "aws_cur",
                "via": "cloudformation",
            })
        except Exception:
            pass
    else:
        print("\n  Add the env vars above to your Claude Desktop config or .env manually.\n")


def _run_infra_overview(provider: str = "") -> None:
    """
    Show the connector registry overview or a specific provider's setup guide.
    Called by: finops setup infra [provider]
    """
    from .setup.cloud_infra import (
        print_connector_overview,
        print_saas_setup,
        AzureSetupPackage,
        GCPSetupPackage,
        SnowflakeSetupPackage,
    )

    if not provider:
        print_connector_overview()
        print(
            "  Run 'finops setup infra <provider>' for detailed setup steps.\n"
            "  Run 'finops setup aws-cur' to deploy the CUR pipeline interactively.\n"
        )
        return

    p = provider.lower()
    if p == "aws" or p == "aws-cur":
        print(
            "\n  AWS uses your existing credential chain (IAM role, ~/.aws/credentials, env vars).\n"
            "  For line-item CUR detail, run: finops setup aws-cur\n"
        )
    elif p == "azure":
        sub_id = _prompt("  Azure Subscription ID (leave blank to skip validation)")
        AzureSetupPackage().print_setup_commands(sub_id or "<your-subscription-id>")
    elif p == "gcp":
        project = _prompt("  GCP Project ID")
        billing = _prompt("  Billing Account ID (format: XXXXXX-XXXXXX-XXXXXX)")
        GCPSetupPackage().print_setup_commands(
            project or "<project-id>",
            billing or "<billing-account-id>",
        )
    elif p == "snowflake":
        account = _prompt("  Snowflake account identifier (e.g. xy12345.us-east-1)")
        SnowflakeSetupPackage().print_setup_sql(account or "<account>")
    else:
        print_saas_setup(p)


def _configure_claude_desktop() -> None:
    """
    Auto-detect claude_desktop_config.json and inject the nable MCP server
    with the correct absolute path to finops-mcp.

    This is the #1 reason nable doesn't work on company computers. Claude
    Desktop is a GUI app that doesn't inherit the user's shell PATH, so
    'finops-mcp' as a bare command fails unless it's in /usr/bin or /bin.

    We resolve the absolute path at setup time and write it to the config.
    """
    try:
        _configure_claude_desktop_inner()
    except (KeyboardInterrupt, EOFError):
        print("\n  Claude Desktop configuration skipped.")
    except Exception as e:
        _warn(f"Claude Desktop auto-config failed: {e}")
        print("  Run 'finops setup claude' to try again, or configure manually.")


def _configure_claude_desktop_inner() -> None:
    """Inner implementation -- called by _configure_claude_desktop with error handling."""
    import json
    import shutil

    # Platform-specific config paths
    config_paths = [
        # macOS
        Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        # Windows
        Path.home() / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json",
        # Linux (some distributions)
        Path.home() / ".config" / "Claude" / "claude_desktop_config.json",
        Path.home() / ".config" / "claude-desktop" / "claude_desktop_config.json",
    ]

    config_path = next((p for p in config_paths if p.parent.exists()), None)

    if config_path is None:
        print("\n  ──────────────────────────────────────────────────")
        print("  Claude Desktop not found. To finish setup manually:")
        print("  1. Install Claude Desktop from https://claude.ai/download")
        print("  2. Run 'finops setup claude' after installing")
        print("  ──────────────────────────────────────────────────")
        return

    # Determine the best launch strategy:
    # 1. uvx  — isolated venv, works in corporate environments with no PATH issues
    # 2. absolute path to finops-mcp binary
    uvx_bin = shutil.which("uvx")
    finops_bin = shutil.which("finops-mcp")
    if not finops_bin:
        finops_bin = str(Path(sys.executable).parent / "finops-mcp")

    if uvx_bin:
        mcp_entry = {"command": uvx_bin, "args": ["finops-mcp"]}
        display_cmd = f"uvx finops-mcp  (uvx found at {uvx_bin})"
    else:
        mcp_entry = {"command": finops_bin}
        display_cmd = finops_bin

    # Load existing config or start fresh
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
        except (json.JSONDecodeError, OSError):
            config = {}
    else:
        config = {}

    config.setdefault("mcpServers", {})

    existing = config["mcpServers"].get("finops", {})
    # Already up-to-date?
    if existing == mcp_entry:
        _ok(f"Claude Desktop already configured: {display_cmd}")
        return

    print(f"\n  ──────────────────────────────────────────────────")
    print(f"  Configure Claude Desktop to use nable?")
    print(f"  Config file: {config_path}")
    print(f"  Command:     {display_cmd}")
    if uvx_bin:
        print(f"  (uvx mode: works on corporate machines without PATH changes)")
    if existing:
        print(f"  (replaces existing: {existing.get('command', '?')})")
    print(f"  ──────────────────────────────────────────────────")

    try:
        ans = _prompt("  Write config? [Y/n]", default="y").lower()
    except (KeyboardInterrupt, EOFError):
        print("\n  Skipped. Add manually:")
        _print_manual_config(mcp_entry)
        return

    if ans not in ("y", "yes", ""):
        print("  Skipped. Add manually:")
        _print_manual_config(mcp_entry)
        return

    config["mcpServers"]["finops"] = mcp_entry

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    _ok(f"Claude Desktop configured → {config_path}")
    print("  Restart Claude Desktop for the changes to take effect.")


def _print_manual_config(mcp_entry: dict) -> None:
    import json
    snippet = json.dumps({"mcpServers": {"finops": mcp_entry}}, indent=2)
    print(f"\n  Add to your claude_desktop_config.json:\n")
    for line in snippet.splitlines():
        print(f"    {line}")
    print()


if __name__ == "__main__":
    main(sys.argv[1:])
