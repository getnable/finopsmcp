"""
Azure Cloud Shell one-paste connect.

Azure has no CloudFormation equivalent for this: an App Registration lives at
the Entra ID (tenant) level, not a resource group, so there is no ARM template
that can create one the way iam_setup.py's CloudFormation template creates an
AWS IAM user. The real analog is Azure Cloud Shell: browser-based, already
signed in as the user, no local az CLI install needed. This module generates
the one script that replaces the whole manual sequence (create the service
principal, then run three role-assignment commands per subscription) with a
single paste in, single line back.
"""
from __future__ import annotations

import re

CLOUDSHELL_URL = "https://shell.azure.com/bash"

# The exact read-only roles setup_azure() already documents for a manually
# created service principal. Cost Management Reader for cost/budget/forecast,
# Reader for Advisor recs + VM list, Monitoring Reader for VM CPU (rightsizing).
_READONLY_ROLES = ("Cost Management Reader", "Reader", "Monitoring Reader")


def generate_cloudshell_script(sp_name: str = "nable-finops-readonly") -> str:
    """Bash script for Azure Cloud Shell: creates a read-only service principal,
    assigns the three roles across every subscription the signed-in user can
    see, and prints one combined line (tenant:client:secret:sub1,sub2,...) for
    the setup wizard's single-paste prompt.

    Read-only by construction: the roles above are Microsoft's own built-in
    read roles, and the script assigns nothing else. Delete the app
    registration in Entra ID any time to revoke access.
    """
    roles_array = " ".join(f'"{r}"' for r in _READONLY_ROLES)
    return f"""\
# nable read-only connect: creates a service principal scoped to
# {", ".join(_READONLY_ROLES)} on every subscription you can see.
# Paste this whole block into Cloud Shell, then paste back the ONE line
# it prints at the end into the nable setup wizard.
set -e
SP_NAME="{sp_name}"
TENANT_ID=$(az account show --query tenantId -o tsv)
SP_JSON=$(az ad sp create-for-rbac --name "$SP_NAME" --skip-assignment 2>/dev/null || \\
          az ad sp create-for-rbac --name "$SP_NAME")
CLIENT_ID=$(echo "$SP_JSON" | grep -o '"appId": *"[^"]*"' | cut -d'"' -f4)
CLIENT_SECRET=$(echo "$SP_JSON" | grep -o '"password": *"[^"]*"' | cut -d'"' -f4)
ROLES=({roles_array})
SUBS=$(az account list --query "[].id" -o tsv)
for SUB in $SUBS; do
  for ROLE in "${{ROLES[@]}}"; do
    az role assignment create --assignee "$CLIENT_ID" --role "$ROLE" \\
      --scope "/subscriptions/$SUB" >/dev/null
  done
done
SUB_CSV=$(echo "$SUBS" | tr '\\n' ',' | sed 's/,$//')
echo ""
echo "Paste this ONE line into the nable setup wizard:"
echo "$TENANT_ID:$CLIENT_ID:$CLIENT_SECRET:$SUB_CSV"
"""


def parse_combined_azure_paste(combined: str) -> tuple[str, str, str, list[str]] | None:
    """Parse the Cloud Shell script's output line
    ("tenant_id:client_id:client_secret:sub1,sub2,...") into its parts, or None
    if it does not look like a valid quadruple. Azure GUIDs never contain a
    colon, and a client secret containing one is vanishingly rare and would
    just fail the field-count check below, falling through to manual entry."""
    if not combined or combined.count(":") != 3:
        return None
    tenant_id, client_id, client_secret, sub_csv = combined.split(":")
    _guid = re.compile(r"^[0-9a-fA-F-]{36}$")
    if not (_guid.match(tenant_id) and _guid.match(client_id)):
        return None
    if len(client_secret) < 8:
        return None
    subs = [s.strip() for s in sub_csv.split(",") if s.strip()]
    if not subs:
        return None
    return tenant_id, client_id, client_secret, subs
