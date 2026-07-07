"""
nable one-command cloud infrastructure provisioning.

Instead of asking users to paste API keys and manually create billing exports,
this module deploys the required infrastructure for each cloud provider and
outputs the exact env vars to add to their config.

Provider strategy
─────────────────
AWS      Deploy CloudFormation stack (CUR + Glue + Athena + IAM role).
         Uses existing boto3 credentials — zero new secrets required.

Azure    Generate a single `az` CLI command that creates a service principal
         with Billing Reader + Cost Management Reader, then stores the output.

GCP      Generate `gcloud` commands that create a service account, enable
         billing export to BigQuery, and grant the required IAM roles.

Snowflake  Generate a SQL script run as ACCOUNTADMIN that creates a read-only
           role + user with SNOWFLAKE.ACCOUNT_USAGE access.

SaaS     Guided API key creation with direct links, scope validation,
         and immediate read-back to confirm the key works.

The goal: 10 connectors configured in under an hour, all with least-privilege
credentials and no manual console navigation beyond initial auth.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

log = logging.getLogger("finops.setup.cloud_infra")

# Path to the bundled CloudFormation template.
# Resolves correctly whether running from source or installed as a wheel.
def _template_path() -> Path:
    # Installed wheel: templates/ is two levels above the finops package
    candidates = [
        Path(__file__).parent.parent.parent.parent / "templates" / "aws-cur-setup.yaml",  # dev
        Path(__file__).parent.parent / "templates" / "aws-cur-setup.yaml",                # alt layout
    ]
    # Also check alongside the installed package directory
    import importlib.resources as _pkg_res
    try:
        # Python 3.9+ importlib.resources
        ref = _pkg_res.files("finops") / "../../../templates/aws-cur-setup.yaml"
        candidates.append(Path(str(ref)))
    except Exception:
        pass
    for p in candidates:
        if p.exists():
            return p.resolve()
    return candidates[0]  # will fail with a clear error in setup_aws_cur

_TEMPLATE_PATH = _template_path()
_CF_STACK_NAME = "nable-cur-pipeline"
_CF_REGION     = "us-east-1"   # CUR reports only deliver to us-east-1


# ── Helpers ───────────────────────────────────────────────────────────────────

def _print(msg: str, indent: int = 0) -> None:
    print("  " * indent + msg)


def _ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m  {msg}")


def _warn(msg: str) -> None:
    print(f"  \033[33m!\033[0m  {msg}")


def _err(msg: str) -> None:
    print(f"  \033[31m✗\033[0m  {msg}")


def _header(msg: str) -> None:
    print(f"\n  \033[1m{msg}\033[0m")


def _prompt(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"  {msg}{suffix}: ").strip()
        return val or default
    except (KeyboardInterrupt, EOFError):
        return default


# ══════════════════════════════════════════════════════════════════════════════
# AWS — CloudFormation deployment
# ══════════════════════════════════════════════════════════════════════════════

def setup_aws_cur(
    auto_deploy: bool = False,
    open_console: bool = True,
) -> dict[str, str]:
    """
    Deploy the nable CUR pipeline via CloudFormation.

    Returns a dict of env vars to store (empty dict on failure/skip).

    Flow:
      1. Check AWS credentials + account ID
      2. Check if the stack already exists
      3. Either deploy directly via boto3 (auto_deploy=True) or open
         the CloudFormation console with the template pre-loaded
      4. Poll for stack completion and extract outputs
      5. Return env var dict
    """
    _header("AWS — Cost and Usage Report (CUR) pipeline")
    _print("This deploys a CloudFormation stack that creates:", 1)
    _print("• S3 bucket for CUR Parquet files (~13 months retained)", 2)
    _print("• Daily CUR v2 report with resource-level detail", 2)
    _print("• Glue crawler (runs 6 AM UTC, keeps Athena schema in sync)", 2)
    _print("• Athena workgroup with 10 GB query safety limit", 2)
    _print("• Read-only IAM role (nable-cost-reader)", 2)
    _print("Cost: ~$2-5/mo for S3 + Athena queries (typically <$1 for small accounts)", 1)

    # ── Check credentials ────────────────────────────────────────────────────
    try:
        import boto3
        from botocore.exceptions import ClientError, NoCredentialsError
    except ImportError:
        _err("boto3 not installed. Run: pip install boto3")
        return {}

    try:
        sts = boto3.client("sts", region_name=_CF_REGION)
        identity = sts.get_caller_identity()
        account_id = identity["Account"]
        arn = identity["Arn"]
        _ok(f"AWS credentials valid — account {account_id} ({arn.split('/')[-1]})")
    except Exception as e:
        _err(f"AWS credentials not found or invalid: {e}")
        _print("Run 'aws configure' or set AWS_PROFILE / AWS_ACCESS_KEY_ID", 1)
        return {}

    # ── Check if stack already exists ────────────────────────────────────────
    cf = boto3.client("cloudformation", region_name=_CF_REGION)
    stack_exists = False
    stack_status = ""
    try:
        resp = cf.describe_stacks(StackName=_CF_STACK_NAME)
        stack_status = resp["Stacks"][0]["StackStatus"]
        stack_exists = True
        if stack_status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
            _ok(f"Stack '{_CF_STACK_NAME}' already deployed ({stack_status})")
            return _extract_cf_outputs(cf)
        elif stack_status.endswith("_IN_PROGRESS"):
            _warn(f"Stack is currently {stack_status} — polling for completion...")
            return _poll_cf_stack(cf)
        elif stack_status.endswith("_FAILED") or stack_status == "ROLLBACK_COMPLETE":
            _warn(f"Stack in failed state ({stack_status}) — will attempt re-deploy")
    except Exception:
        pass  # Stack doesn't exist

    # ── Load template ────────────────────────────────────────────────────────
    if not _TEMPLATE_PATH.exists():
        _err(f"CloudFormation template not found at {_TEMPLATE_PATH}")
        _err("Reinstall finops-mcp to restore bundled templates.")
        return {}

    template_body = _TEMPLATE_PATH.read_text()

    # ── Deploy ───────────────────────────────────────────────────────────────
    if auto_deploy:
        return _deploy_cf_stack(cf, template_body, account_id, stack_exists)
    else:
        return _open_cf_console(template_body, account_id)


def _deploy_cf_stack(
    cf: Any,
    template_body: str,
    account_id: str,
    stack_exists: bool,
) -> dict[str, str]:
    """Deploy or update the CF stack directly via boto3."""
    _print("")
    params = [
        {"ParameterKey": "ReportName", "ParameterValue": "nable-cur"},
        {"ParameterKey": "EnableSplitCost", "ParameterValue": "true"},
    ]
    capabilities = ["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM"]

    try:
        if stack_exists:
            _print("Updating existing stack...", 1)
            cf.update_stack(
                StackName=_CF_STACK_NAME,
                TemplateBody=template_body,
                Parameters=params,
                Capabilities=capabilities,
            )
        else:
            _print("Creating stack (takes ~2 minutes)...", 1)
            cf.create_stack(
                StackName=_CF_STACK_NAME,
                TemplateBody=template_body,
                Parameters=params,
                Capabilities=capabilities,
                OnFailure="ROLLBACK",
                Tags=[
                    {"Key": "ManagedBy", "Value": "nable-finops-mcp"},
                    {"Key": "Purpose", "Value": "CostAndUsageReport"},
                ],
            )
        return _poll_cf_stack(cf)
    except Exception as e:
        if "No updates are to be performed" in str(e):
            _ok("Stack is already up to date")
            return _extract_cf_outputs(cf)
        _err(f"CloudFormation deployment failed: {e}")
        return {}


def _open_cf_console(template_body: str, account_id: str) -> dict[str, str]:
    """
    Open the AWS CloudFormation console with the template pre-loaded.
    Falls back to printing the deploy command if browser not available.
    """
    # Write template to a temp file for the console upload approach
    tmp = Path.home() / ".finops-mcp" / "aws-cur-setup.yaml"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(template_body)

    console_url = (
        f"https://console.aws.amazon.com/cloudformation/home"
        f"?region={_CF_REGION}"
        f"#/stacks/create/review"
        f"?stackName={_CF_STACK_NAME}"
    )

    _print("")
    _print("Option A — Deploy via AWS CLI (recommended, takes ~2 min):", 1)
    _print(
        f"  aws cloudformation deploy \\\n"
        f"    --template-file {tmp} \\\n"
        f"    --stack-name {_CF_STACK_NAME} \\\n"
        f"    --region {_CF_REGION} \\\n"
        f"    --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \\\n"
        f"    --parameter-overrides ReportName=nable-cur EnableSplitCost=true",
        1,
    )

    _print("")
    _print("Option B — CloudFormation Console:", 1)
    _print(f"  1. Go to: {console_url}", 2)
    _print(f"  2. Upload template: {tmp}", 2)
    _print(f"  3. Click through defaults and deploy", 2)

    try_deploy = _prompt(
        "\nDeploy now via AWS CLI? [Y/n]", default="y"
    ).lower()

    if try_deploy in ("y", "yes", ""):
        cf = None
        try:
            import boto3
            cf = boto3.client("cloudformation", region_name=_CF_REGION)
            return _deploy_cf_stack(cf, template_body, account_id, stack_exists=False)
        except Exception as e:
            _err(f"Direct deploy failed: {e}")
            _print("Try the AWS CLI command above manually, then re-run 'finops setup'", 1)
            return {}

    _print("Run the AWS CLI command above, then re-run 'finops setup' to store the outputs.", 1)
    return {}


def _poll_cf_stack(cf: Any, timeout: int = 300) -> dict[str, str]:
    """Poll CF stack until complete or failed. Returns env var dict."""
    _print("  Waiting for stack...", 1)
    deadline = time.time() + timeout
    dots = 0
    while time.time() < deadline:
        try:
            resp = cf.describe_stacks(StackName=_CF_STACK_NAME)
            status = resp["Stacks"][0]["StackStatus"]
            if status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
                print()
                _ok(f"Stack deployed successfully ({status})")
                return _extract_cf_outputs(cf)
            elif "FAILED" in status or status == "ROLLBACK_COMPLETE":
                print()
                _err(f"Stack deployment failed: {status}")
                # Try to print the failure reason
                try:
                    events = cf.describe_stack_events(StackName=_CF_STACK_NAME)["StackEvents"]
                    for e in events[:5]:
                        if "FAILED" in e.get("ResourceStatus", ""):
                            _err(f"  {e['LogicalResourceId']}: {e.get('ResourceStatusReason', '')}")
                except Exception:
                    pass
                return {}
            print(".", end="", flush=True)
            dots += 1
        except Exception:
            pass
        time.sleep(5)

    print()
    _err(f"Stack deployment timed out after {timeout}s")
    _print(f"Check the CloudFormation console for stack '{_CF_STACK_NAME}' in us-east-1", 1)
    return {}


def _extract_cf_outputs(cf: Any) -> dict[str, str]:
    """Extract stack outputs and convert to env var dict."""
    try:
        resp = cf.describe_stacks(StackName=_CF_STACK_NAME)
        outputs = {
            o["OutputKey"]: o["OutputValue"]
            for o in resp["Stacks"][0].get("Outputs", [])
        }

        bucket = outputs.get("CURBucketName", "")
        db     = outputs.get("GlueDatabase", "nable_cur")
        table  = outputs.get("AthenaTable", "nable_cur.nable-cur")
        wg     = outputs.get("AthenaWorkgroup", "nable-cur")

        env_vars = {
            "CUR_S3_BUCKET":             bucket,
            "CUR_ATHENA_DATABASE":       db,
            "CUR_ATHENA_TABLE":          table.split(".")[-1],  # just the table name
            "CUR_ATHENA_RESULTS_BUCKET": bucket,
            "CUR_ATHENA_WORKGROUP":      wg,
        }

        _print("")
        _ok("CUR pipeline ready. Add these to your config:")
        for k, v in env_vars.items():
            _print(f"  {k}={v}", 1)

        _warn(
            "First CUR data arrives within 24 hours. "
            "The Glue crawler runs at 6 AM UTC to update the Athena table."
        )

        return env_vars

    except Exception as e:
        _err(f"Failed to read stack outputs: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# AZURE — Service principal + billing export
# ══════════════════════════════════════════════════════════════════════════════

class AzureSetupPackage:
    """
    Generates the exact `az` CLI commands to create a service principal
    with the minimum permissions for nable, then validates credentials.

    What it creates (least privilege):
      - App registration + service principal
      - Billing Reader role (read invoices and billing profiles)
      - Cost Management Reader role (read costs and exports)
      - Reader role on the subscription (read resource metadata)

    No storage export required — nable queries the Cost Management API
    directly. The service principal is the credential; no API key needed.
    """

    ROLES = [
        "Billing Reader",
        "Cost Management Reader",
        "Reader",
    ]

    def print_setup_commands(self, subscription_id: str, sp_name: str = "nable-cost-reader") -> None:
        _header("Azure — Service Principal setup")
        _print("Run these commands in Azure CLI (takes ~30 seconds):", 1)
        _print("")

        scope = f"/subscriptions/{subscription_id}"
        role_flags = " ".join(f'--role "{r}"' for r in self.ROLES)

        _print("  # 1. Create the service principal with required roles", 1)
        _print(
            f"  az ad sp create-for-rbac \\\n"
            f"    --name {sp_name} \\\n"
            f"    --scopes {scope} \\\n"
            f"    --role 'Billing Reader' \\\n"
            f"    --output json",
            1,
        )
        _print("")
        _print("  # 2. Add Cost Management Reader (az assigns one role at creation)", 1)
        _print(
            f"  SP_ID=$(az ad sp list --display-name {sp_name} --query '[0].id' -o tsv)\n"
            f"  az role assignment create \\\n"
            f"    --assignee-object-id $SP_ID \\\n"
            f"    --assignee-principal-type ServicePrincipal \\\n"
            f"    --role 'Cost Management Reader' \\\n"
            f"    --scope {scope}",
            1,
        )
        _print("")
        _print("  The JSON output contains: appId (CLIENT_ID), password (CLIENT_SECRET), tenant (TENANT_ID)", 1)
        _print("  Store these in your finops-mcp config as:", 1)
        _print("    AZURE_CLIENT_ID=<appId>", 2)
        _print("    AZURE_CLIENT_SECRET=<password>", 2)
        _print("    AZURE_TENANT_ID=<tenant>", 2)
        _print(f"    AZURE_SUBSCRIPTION_ID={subscription_id}", 2)

    def validate(self, client_id: str, client_secret: str, tenant_id: str, subscription_id: str) -> bool:
        """Validate that credentials work and have billing access."""
        try:
            import httpx
            token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
            resp = httpx.post(token_url, data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://management.azure.com/.default",
            }, timeout=10)
            if resp.status_code != 200:
                _err(f"Azure auth failed: {resp.text[:200]}")
                return False

            token = resp.json()["access_token"]
            # Test: list cost by service for current month
            test_url = (
                f"https://management.azure.com/subscriptions/{subscription_id}"
                f"/providers/Microsoft.CostManagement/query?api-version=2023-11-01"
            )
            test_body = {
                "type": "ActualCost",
                "timeframe": "MonthToDate",
                "dataset": {"granularity": "None", "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}}},
            }
            check = httpx.post(
                test_url,
                json=test_body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            if check.status_code == 200:
                _ok("Azure credentials valid — Cost Management API accessible")
                return True
            else:
                _err(f"Azure credential check failed ({check.status_code}): {check.text[:200]}")
                return False
        except Exception as e:
            _err(f"Azure validation error: {e}")
            return False


# ══════════════════════════════════════════════════════════════════════════════
# GCP — Service account + BigQuery billing export
# ══════════════════════════════════════════════════════════════════════════════

class GCPSetupPackage:
    """
    Generates `gcloud` commands to set up a service account and enable
    BigQuery billing export (the GCP equivalent of AWS CUR).

    Two paths:
      A. Billing API only (free, instant) — aggregated cost data.
         Good enough for ~80% of use cases.
      B. BigQuery billing export (recommended for Pro) — line-item detail,
         same depth as AWS CUR. Takes 24h for first data.

    nable reads from BigQuery using the service account key file.
    No API key storage — the service account key JSON IS the credential.
    """

    def print_setup_commands(
        self,
        project_id: str,
        billing_account_id: str,
        dataset_id: str = "nable_billing_export",
        sa_name: str = "nable-cost-reader",
    ) -> None:
        _header("GCP — Service Account + BigQuery Billing Export")
        _print("Run these commands in Cloud Shell or gcloud CLI:", 1)
        _print("")

        sa_email = f"{sa_name}@{project_id}.iam.gserviceaccount.com"

        _print("  # 1. Create service account", 1)
        _print(
            f"  gcloud iam service-accounts create {sa_name} \\\n"
            f"    --display-name='nable Cost Reader' \\\n"
            f"    --project={project_id}",
            1,
        )
        _print("")

        _print("  # 2. Grant Billing Account Viewer (to read billing data)", 1)
        _print(
            f"  gcloud billing accounts add-iam-policy-binding {billing_account_id} \\\n"
            f"    --member='serviceAccount:{sa_email}' \\\n"
            f"    --role='roles/billing.viewer'",
            1,
        )
        _print("")

        _print("  # 3. Grant BigQuery Data Viewer on the billing export dataset", 1)
        _print(
            f"  gcloud projects add-iam-policy-binding {project_id} \\\n"
            f"    --member='serviceAccount:{sa_email}' \\\n"
            f"    --role='roles/bigquery.dataViewer'",
            1,
        )
        _print(
            f"  gcloud projects add-iam-policy-binding {project_id} \\\n"
            f"    --member='serviceAccount:{sa_email}' \\\n"
            f"    --role='roles/bigquery.jobUser'",
            1,
        )
        _print("")

        _print("  # 4. Create service account key (download JSON)", 1)
        key_file = f"~/.finops-mcp/gcp-{sa_name}-key.json"
        _print(
            f"  gcloud iam service-accounts keys create {key_file} \\\n"
            f"    --iam-account={sa_email}",
            1,
        )
        _print("")

        _print("  # 5. Enable BigQuery billing export in the Console (one-time):", 1)
        _print(
            f"  https://console.cloud.google.com/billing/{billing_account_id}/export",
            2,
        )
        _print("  → Select 'BigQuery export' → Standard usage cost", 2)
        _print(f"  → Project: {project_id}  Dataset: {dataset_id}", 2)
        _print("")

        _print("  Add to your finops-mcp config:", 1)
        _print(f"    GOOGLE_APPLICATION_CREDENTIALS={key_file}", 2)
        _print(f"    GCP_BILLING_PROJECT={project_id}", 2)
        _print(f"    GCP_BIGQUERY_DATASET={project_id}.{dataset_id}.gcp_billing_export_v1_*", 2)

    def validate(self, credentials_path: str, project_id: str) -> bool:
        """Validate GCP service account credentials."""
        try:
            from google.cloud import bigquery
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_file(
                credentials_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            client = bigquery.Client(project=project_id, credentials=creds)
            # Simple validation: list datasets
            list(client.list_datasets(max_results=1))
            _ok("GCP credentials valid — BigQuery accessible")
            return True
        except Exception as e:
            _err(f"GCP validation error: {e}")
            return False


# ══════════════════════════════════════════════════════════════════════════════
# SNOWFLAKE — Read-only SQL setup
# ══════════════════════════════════════════════════════════════════════════════

class SnowflakeSetupPackage:
    """
    Generates SQL to create a least-privilege Snowflake role for nable.

    Uses SNOWFLAKE.ACCOUNT_USAGE schema (no ACCOUNTADMIN required at
    runtime — only during initial setup) to read:
      - QUERY_HISTORY (compute cost per query/warehouse)
      - WAREHOUSE_METERING_HISTORY (warehouse credit usage)
      - STORAGE_USAGE (storage cost)
      - MARKETPLACE_PAID_USAGE_DAILY (Marketplace spend)

    Credentials: username + password OR RSA key pair (recommended).
    """

    def print_setup_sql(self, account: str, user_name: str = "NABLE_READER") -> None:
        _header("Snowflake — Read-only role setup")
        _print("Run this SQL as ACCOUNTADMIN in a Snowflake worksheet:", 1)
        _print("")

        sql = f"""
-- Run as ACCOUNTADMIN
USE ROLE ACCOUNTADMIN;

-- Create the nable read-only role
CREATE ROLE IF NOT EXISTS NABLE_COST_READER
  COMMENT = 'Read-only role for nable finops-mcp cost visibility';

-- Grant access to account usage views (cost data)
GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE NABLE_COST_READER;

-- Create a dedicated user
CREATE USER IF NOT EXISTS {user_name}
  DEFAULT_ROLE = NABLE_COST_READER
  DEFAULT_WAREHOUSE = COMPUTE_WH
  COMMENT = 'nable finops-mcp service user';

GRANT ROLE NABLE_COST_READER TO USER {user_name};

-- Optionally set a password (or use RSA key pair below)
ALTER USER {user_name} SET PASSWORD = '<generate-a-strong-password>';

-- RSA key pair alternative (more secure):
-- ALTER USER {user_name} SET RSA_PUBLIC_KEY = '<your-public-key>';

-- Verify access
USE ROLE NABLE_COST_READER;
SELECT WAREHOUSE_NAME, SUM(CREDITS_USED) AS CREDITS
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE START_TIME >= DATEADD(DAY, -30, CURRENT_TIMESTAMP)
GROUP BY 1
ORDER BY 2 DESC
LIMIT 5;
"""
        for line in sql.strip().splitlines():
            _print(f"  {line}", 1)

        _print("")
        _print("  Add to your finops-mcp config:", 1)
        _print(f"    SNOWFLAKE_ACCOUNT={account}", 2)
        _print(f"    SNOWFLAKE_USER={user_name}", 2)
        _print(f"    SNOWFLAKE_PASSWORD=<password-you-set>", 2)
        _print(f"    SNOWFLAKE_ROLE=NABLE_COST_READER", 2)


# ══════════════════════════════════════════════════════════════════════════════
# SaaS connector guidance (Datadog, Stripe, etc.)
# ══════════════════════════════════════════════════════════════════════════════

SAAS_SETUP_PACKAGES: dict[str, dict] = {
    "datadog": {
        "name": "Datadog",
        "env_vars": ["DATADOG_API_KEY", "DATADOG_APP_KEY"],
        "key_create_url": "https://app.datadoghq.com/organization-settings/api-keys",
        "min_permissions": ["usage_read", "metrics_read"],
        "notes": (
            "Create an API key and Application key with 'Usage Read' scope. "
            "App keys should be scoped to a service account, not a personal account."
        ),
        "validation": lambda keys: _validate_datadog(**keys),
    },
    "stripe": {
        "name": "Stripe",
        "env_vars": ["STRIPE_API_KEY"],
        "key_create_url": "https://dashboard.stripe.com/apikeys",
        "min_permissions": ["read_only"],
        "notes": (
            "Create a Restricted Key with: Balance (read), Charges (read), "
            "Invoices (read), Usage Records (read). "
            "Never use a secret key — restricted keys are scoped."
        ),
    },
    "datadog_usage": {
        "name": "Datadog (Usage API)",
        "env_vars": ["DATADOG_API_KEY"],
        "key_create_url": "https://app.datadoghq.com/organization-settings/api-keys",
        "min_permissions": ["usage_read"],
        "notes": "API key with usage_read scope. See Usage & Cost page in Datadog.",
    },
    "github": {
        "name": "GitHub",
        "env_vars": ["GITHUB_TOKEN"],
        "key_create_url": "https://github.com/settings/tokens/new",
        "min_permissions": ["repo:read", "billing:read"],
        "notes": (
            "Fine-grained personal access token: Organization permissions → "
            "Administration (read). For billing data, the token must belong to "
            "an organization owner. Prefer GitHub Apps over PATs for production."
        ),
    },
    "jira": {
        "name": "Jira",
        "env_vars": ["JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"],
        "key_create_url": "https://id.atlassian.com/manage-profile/security/api-tokens",
        "min_permissions": ["create_issues", "browse_projects"],
        "notes": (
            "Create an API token at id.atlassian.com. "
            "The token is tied to your email — for production use a dedicated "
            "service account email (e.g. nable-bot@yourcompany.atlassian.net)."
        ),
    },
    "linear": {
        "name": "Linear",
        "env_vars": ["LINEAR_API_KEY"],
        "key_create_url": "https://linear.app/settings/api",
        "min_permissions": ["issues:create", "issues:read"],
        "notes": "Personal API key or OAuth app. Read + Write on Issues.",
    },
    "slack": {
        "name": "Slack",
        "env_vars": ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"],
        "key_create_url": "https://api.slack.com/apps",
        "notes": (
            "Create a Slack App with Bot Token scopes: "
            "channels:read, chat:write, users:read. "
            "Enable Socket Mode for real-time alerts."
        ),
    },
    "pagerduty": {
        "name": "PagerDuty",
        "env_vars": ["PAGERDUTY_API_KEY"],
        "key_create_url": "https://your-subdomain.pagerduty.com/api_keys",
        "min_permissions": ["read_only"],
        "notes": "Read-only API key. Used for incident correlation with cost spikes.",
    },
}


def print_saas_setup(provider: str) -> None:
    """Print guided setup for a SaaS connector."""
    pkg = SAAS_SETUP_PACKAGES.get(provider.lower())
    if not pkg:
        _warn(f"No setup package for '{provider}' — check docs at getnable.com/docs")
        return

    _header(f"{pkg['name']} — API key setup")

    if pkg.get("min_permissions"):
        _print(f"Required permissions: {', '.join(pkg['min_permissions'])}", 1)

    _print(f"Create key at: {pkg['key_create_url']}", 1)
    _print(f"Note: {pkg['notes']}", 1)
    _print("")
    _print("Env vars to set:", 1)
    for var in pkg["env_vars"]:
        _print(f"  {var}=<value>", 1)


# ══════════════════════════════════════════════════════════════════════════════
# Connector registry — "what does this unlock?"
# ══════════════════════════════════════════════════════════════════════════════

CONNECTOR_REGISTRY: list[dict] = [
    # ── Cloud providers (use existing credential chain) ─────────────────────
    {
        "id": "aws",
        "name": "AWS",
        "category": "cloud",
        "setup_method": "credential_chain",
        "setup_time_min": 1,
        "env_vars": [],          # uses boto3 default chain
        "unlocks": [
            "EC2, RDS, S3, Lambda cost by service and tag",
            "Anomaly detection across all AWS services",
            "Rightsizing recommendations (EC2, RDS)",
            "Reserved Instance and Savings Plan coverage",
            "Multi-account org rollup (if Organizations enabled)",
        ],
        "note": "Uses your existing AWS credentials (IAM role, ~/.aws/credentials, env vars). Zero config if already set up.",
    },
    {
        "id": "aws_cur",
        "name": "AWS CUR (line-item detail)",
        "category": "cloud_detail",
        "setup_method": "cloudformation",
        "setup_time_min": 5,
        "env_vars": ["CUR_S3_BUCKET", "CUR_ATHENA_DATABASE", "CUR_ATHENA_TABLE", "CUR_ATHENA_WORKGROUP"],
        "unlocks": [
            "Per-resource line-item cost (not just aggregated totals)",
            "Exact RI/SP amortization per resource",
            "Tag-level breakdown at the resource line level",
            "EKS split cost allocation (pod-level costs from billing)",
            "Sub-service detail (e.g. which S3 bucket, which Lambda function)",
        ],
        "note": "Requires deploying the nable CloudFormation stack. Data arrives within 24 hours of deployment.",
        "team_only": True,
    },
    {
        "id": "azure",
        "name": "Azure",
        "category": "cloud",
        "setup_method": "service_principal",
        "setup_time_min": 3,
        "env_vars": ["AZURE_SUBSCRIPTION_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID"],
        "unlocks": [
            "Azure resource cost by service, resource group, and tag",
            "Reservation utilization and coverage",
            "Budget tracking and alerts",
            "Multi-subscription rollup",
        ],
        "note": "One az CLI command creates the service principal. No console navigation needed.",
    },
    {
        "id": "gcp",
        "name": "GCP",
        "category": "cloud",
        "setup_method": "service_account",
        "setup_time_min": 5,
        "env_vars": ["GOOGLE_APPLICATION_CREDENTIALS", "GCP_BILLING_PROJECT"],
        "unlocks": [
            "GCP cost by project, service, and label",
            "Committed Use Discount coverage",
            "BigQuery, Cloud Run, GKE cost detail",
        ],
        "note": "Service account key JSON. Enable BigQuery billing export for line-item detail.",
    },
    # ── Kubernetes (uses kubeconfig) ─────────────────────────────────────────
    {
        "id": "kubernetes",
        "name": "Kubernetes",
        "category": "compute",
        "setup_method": "kubeconfig",
        "setup_time_min": 1,
        "env_vars": [],          # uses KUBECONFIG or ~/.kube/config
        "unlocks": [
            "Cost by cluster, namespace, workload, and pod label",
            "Cluster efficiency score (0-100) with A-F grade",
            "Label-based chargeback (team, env, app)",
            "Idle node detection and rightsizing",
            "Helm release cost attribution",
            "Cost trend over time from daily snapshots",
        ],
        "note": "Zero config if kubectl already works. Supports EKS, GKE, AKS, and vanilla k8s.",
    },
    # ── SaaS (API keys) ──────────────────────────────────────────────────────
    {
        "id": "datadog",
        "name": "Datadog",
        "category": "observability",
        "setup_method": "api_key",
        "setup_time_min": 2,
        "env_vars": ["DATADOG_API_KEY", "DATADOG_APP_KEY"],
        "unlocks": [
            "Datadog spend by product (APM, Logs, Infrastructure, Synthetics)",
            "Usage trend and anomaly detection",
            "Correlation with infrastructure spend",
        ],
    },
    {
        "id": "snowflake",
        "name": "Snowflake",
        "category": "data",
        "setup_method": "sql_script",
        "setup_time_min": 5,
        "env_vars": ["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"],
        "unlocks": [
            "Warehouse credit usage by warehouse and query tag",
            "Query cost attribution (who ran what and what it cost)",
            "Storage cost trend",
            "Idle warehouse detection",
        ],
    },
    {
        "id": "stripe",
        "name": "Stripe",
        "category": "revenue",
        "setup_method": "api_key",
        "setup_time_min": 2,
        "env_vars": ["STRIPE_API_KEY"],
        "unlocks": [
            "MRR, ARR, and revenue trend",
            "Hosting cost as % of MRR",
            "Cost per paying customer",
        ],
        "note": "Required for business metrics and unit economics (Pro plan).",
    },
    {
        "id": "github",
        "name": "GitHub Actions",
        "category": "devtools",
        "setup_method": "api_key",
        "setup_time_min": 2,
        "env_vars": ["GITHUB_TOKEN"],
        "unlocks": [
            "GitHub Actions minutes cost by repo and workflow",
            "Copilot seat utilization",
            "PR cost comments (Terraform and Helm diffs)",
        ],
    },
    {
        "id": "slack",
        "name": "Slack",
        "category": "alerting",
        "setup_method": "oauth_app",
        "setup_time_min": 5,
        "env_vars": ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"],
        "unlocks": [
            "Anomaly alerts delivered to Slack channels",
            "Weekly digest reports",
            "Interactive cost queries from Slack",
            "Budget breach notifications",
        ],
    },
]


def print_connector_overview() -> None:
    """Print a table of all connectors with setup time and what they unlock."""
    _header("nable connector overview")
    _print("")

    by_category: dict[str, list[dict]] = {}
    for c in CONNECTOR_REGISTRY:
        cat = c["category"]
        by_category.setdefault(cat, []).append(c)

    total_time = sum(c["setup_time_min"] for c in CONNECTOR_REGISTRY)
    _print(f"  {len(CONNECTOR_REGISTRY)} connectors, ~{total_time} minutes total setup time", 1)
    _print("")

    cat_labels = {
        "cloud": "Cloud providers",
        "cloud_detail": "Cloud line-item detail",
        "compute": "Compute",
        "observability": "Observability",
        "data": "Data platform",
        "revenue": "Revenue",
        "devtools": "Developer tools",
        "alerting": "Alerting",
    }

    for cat, connectors in by_category.items():
        label = cat_labels.get(cat, cat.title())
        _print(f"  {label}:", 1)
        for c in connectors:
            team_flag = " [Team]" if c.get("team_only") else ""
            _print(
                f"    {c['name']:<28} ~{c['setup_time_min']} min   "
                f"{c['unlocks'][0]}{team_flag}",
                1,
            )
        _print("")


def get_setup_estimate(connector_ids: list[str]) -> dict:
    """Return total estimated setup time and what will be unlocked."""
    selected = [c for c in CONNECTOR_REGISTRY if c["id"] in connector_ids]
    total_min = sum(c["setup_time_min"] for c in selected)
    unlocks = []
    for c in selected:
        unlocks.extend(c.get("unlocks", []))
    return {
        "connectors": len(selected),
        "estimated_minutes": total_min,
        "unlocks": unlocks,
    }
