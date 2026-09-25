"""
IAM least-privilege setup for nable (finops-mcp).

Two outputs:
  1. CloudFormation template  — paste into AWS Console or deploy via CLI
  2. Terraform snippet        — drop into your infra repo

The policy has two parts:
  NableReadOnlyScan   exactly what `nable scan` calls, generated from the same
                      manifest as `nable scan --dry-run --json`
  Optional*           one statement per other nable feature (billed Cost
                      Explorer, metric discovery, deeper inventory, CUR
                      discovery, Organizations), each naming what it unlocks and
                      each switchable off with a template parameter

Nothing in either part can create, modify, delete, or terminate any resource.
It is strictly read-only.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

log = logging.getLogger(__name__)

# ── IAM actions ───────────────────────────────────────────────────────────────
#
# Two parts, kept apart on purpose.
#
# The scan statement is generated from scan_manifest, the same source as
# `nable scan --dry-run --json`, and a test derives that manifest from the calls
# the checks make. It is never listed by hand here: this file used to carry its
# own list of 58 actions, described as "exact permissions nable needs, nothing
# more", while the dry run printed 23 and the two disagreed in both directions.
#
# The optional statements are for the rest of nable (the MCP server's cost,
# reservation, rightsizing and cleanup tools). Each is a separate statement
# named for what it unlocks, each can be switched off with a template
# parameter, and the billed one says so. None is needed for `nable scan`.

from ..scan_manifest import iam_actions as _scan_manifest_actions

_SCAN_ACTIONS: list[str] = _scan_manifest_actions(include_spend=False,
                                                  include_get_metric_data=False)

# (Sid, template parameter, what it unlocks, actions)
_OPTIONAL_GROUPS: list[tuple[str, str, str, list[str]]] = [
    ("OptionalCostExplorerBilled", "IncludeCostExplorer",
     ("BILLED: every Cost Explorer request costs $0.01 on your AWS bill. Unlocks "
      "`nable scan --spend`, Bedrock spend in the AI view, cost anomalies, and the "
      "MCP server's cost, reservation and Savings Plans tools."),
     [
         "ce:GetCostAndUsage",
         "ce:GetCostForecast",
         "ce:GetAnomalies",
         "ce:GetReservationUtilization",
         "ce:GetReservationCoverage",
         "ce:GetSavingsPlansPurchaseRecommendation",
         "ce:GetSavingsPlansUtilization",
         "ce:GetSavingsPlansUtilizationDetails",
         "ce:GetSavingsPlansCoverage",
         "ce:GetRightsizingRecommendation",
         "ce:ListCostAllocationTags",
         "ce:DescribeCostCategoryDefinition",
     ]),
    ("OptionalMetricDiscovery", "IncludeMetricDiscovery",
     ("cloudwatch:ListMetrics is free. cloudwatch:GetMetricData is BILLED at $0.01 "
      "per 1,000 metrics and is used only when FINOPS_CLOUDWATCH_GETMETRICDATA=1. "
      "Unlocks metric discovery for the AI routing and CloudWatch cardinality "
      "tools, and batched metric reads."),
     [
         "cloudwatch:GetMetricData",
         "cloudwatch:ListMetrics",
     ]),
    ("OptionalDeeperInventory", "IncludeDeeperInventory",
     ("Free reads. Unlocks the MCP server's rightsizing, cleanup and "
      "recommendation tools: load balancer target health, RDS snapshots, Lambda "
      "concurrency, S3 Intelligent-Tiering configuration, and Compute Optimizer "
      "enrollment and ECS recommendations."),
     [
         "elasticloadbalancing:DescribeTargetGroups",
         "elasticloadbalancing:DescribeTargetHealth",
         "rds:DescribeDBSnapshots",
         "lambda:GetFunctionConfiguration",
         "logs:DescribeLogStreams",
         "s3:GetIntelligentTieringConfiguration",
         "compute-optimizer:GetEnrollmentStatus",
         "compute-optimizer:GetECSServiceRecommendations",
         "compute-optimizer:GetRecommendationSummaries",
     ]),
    # Account-level describe (Resource:"*"): it returns WHERE the CUR is
    # delivered (bucket, prefix, report name) so the export can be read from
    # S3 instead of paying for Cost Explorer. Reading the CUR OBJECTS needs
    # s3:GetObject, which is NOT here: it is granted separately, scoped to the
    # CUR bucket, so the role never gains "read every object in the account".
    ("OptionalCurDiscovery", "IncludeCurDiscovery",
     ("Free. Finds where your Cost and Usage Report is delivered, so it can be "
      "read in place of Cost Explorer. Reading the report itself is a separate "
      "grant, scoped to its bucket."),
     ["cur:DescribeReportDefinitions"]),
    ("OptionalOrganizations", "IncludeOrganizations",
     ("Free. Unlocks the org rollup: listing member accounts and organizational "
      "units from the management account."),
     [
         "organizations:ListAccounts",
         "organizations:ListRoots",
         "organizations:ListOrganizationalUnitsForParent",
         "organizations:ListParents",
         "organizations:DescribeOrganizationalUnit",
         "organizations:DescribeOrganization",
         "organizations:DescribeAccount",
     ]),
]

# Everything any template can grant: the scan, then every optional group.
# sts:AssumeRole is deliberately NOT here: the single-account connect key never
# assumes a role, and granting AssumeRole on "*" turns a "read-only" key into a
# privilege-escalation primitive (it can assume any role whose trust policy
# allows the account root, which is common). Cross-account setups grant
# AssumeRole separately, scoped to the specific role.
_REQUIRED_ACTIONS: list[str] = list(dict.fromkeys(
    _SCAN_ACTIONS + [a for _, _, _, actions in _OPTIONAL_GROUPS for a in actions]))


def _policy_statements() -> list[dict[str, Any]]:
    """The scan statement, then each optional group behind its parameter."""
    statements: list[dict[str, Any]] = [{
        "Sid": "NableReadOnlyScan",
        "Effect": "Allow",
        "Action": list(_SCAN_ACTIONS),
        "Resource": "*",
    }]
    for sid, param, _what, actions in _OPTIONAL_GROUPS:
        statements.append({"Fn::If": [
            param,
            {"Sid": sid, "Effect": "Allow", "Action": list(actions), "Resource": "*"},
            {"Ref": "AWS::NoValue"},
        ]})
    return statements


def _optional_parameters() -> dict[str, Any]:
    return {
        param: {
            "Type": "String",
            "AllowedValues": ["true", "false"],
            "Default": "true",
            "Description": f"{what} Set to false to leave statement {sid} out.",
        }
        for sid, param, what, _ in _OPTIONAL_GROUPS
    }


def _optional_conditions() -> dict[str, Any]:
    return {param: {"Fn::Equals": [{"Ref": param}, "true"]}
            for _, param, _, _ in _OPTIONAL_GROUPS}


_POLICY_DESCRIPTION = (
    "Read-only. Statement NableReadOnlyScan is exactly what `nable scan` calls; "
    "each Optional statement adds another nable feature and can be switched off "
    "with its parameter.")

# Actions that WOULD indicate over-provisioned credentials
_DANGEROUS_ACTIONS_PREFIXES = [
    "ec2:Create", "ec2:Delete", "ec2:Modify", "ec2:Run", "ec2:Stop", "ec2:Terminate",
    "s3:Put", "s3:Delete", "s3:Create",
    "iam:Create", "iam:Delete", "iam:Attach", "iam:Detach", "iam:Put", "iam:Update",
    "rds:Create", "rds:Delete", "rds:Modify",
    "lambda:Create", "lambda:Delete", "lambda:Update", "lambda:Invoke",
    "logs:Put", "logs:Create", "logs:Delete",  # logs writes (e.g. PutRetentionPolicy)
    "sts:Assume",  # AssumeRole et al — escalation primitive, not a read
]

CLOUDFORMATION_TEMPLATE: dict[str, Any] = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Description": (
        "Least-privilege IAM role for nable (finops-mcp). "
        "The NableReadOnlyScan statement is exactly what `nable scan` calls; the "
        "Optional statements add other nable features and can each be switched off. "
        "Strictly read-only: no create, modify, or delete permissions of any kind."
    ),
    "Parameters": {
        "RoleName": {
            "Type": "String",
            "Default": "NableFinopsReadOnly",
            "Description": "Name for the IAM role",
        },
        "TrustedAccountId": {
            "Type": "String",
            "Default": "",
            "Description": (
                "AWS account ID allowed to assume this role cross-account (nable's "
                "hosting account for the managed offering). Leave blank for "
                "same-account instance-profile access."
            ),
        },
        "ExternalId": {
            "Type": "String",
            "Default": "",
            "Description": (
                "Optional external ID required on the cross-account assume-role "
                "(confused-deputy protection). Recommended for the managed offering."
            ),
        },
        **_optional_parameters(),
    },
    "Conditions": {
        # Cross-account trust when a TrustedAccountId is supplied; otherwise the role
        # trusts the EC2 service (same-account instance profile) as before.
        "HasTrustedAccount": {"Fn::Not": [{"Fn::Equals": [{"Ref": "TrustedAccountId"}, ""]}]},
        "HasExternalId": {"Fn::Not": [{"Fn::Equals": [{"Ref": "ExternalId"}, ""]}]},
        **_optional_conditions(),
    },
    "Resources": {
        "NableReadOnlyPolicy": {
            "Type": "AWS::IAM::ManagedPolicy",
            "Properties": {
                "ManagedPolicyName": "NableFinopsReadOnlyPolicy",
                "Description": _POLICY_DESCRIPTION,
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": _policy_statements(),
                },
            },
        },
        "NableReadOnlyRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": {"Ref": "RoleName"},
                "Description": "Read-only role for nable cost intelligence",
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Fn::If": [
                                "HasTrustedAccount",
                                # Managed offering: only nable's hosting account may
                                # assume this role, optionally gated by an external ID.
                                {
                                    "Effect": "Allow",
                                    "Principal": {
                                        "AWS": {"Fn::Sub": "arn:aws:iam::${TrustedAccountId}:root"}
                                    },
                                    "Action": "sts:AssumeRole",
                                    "Condition": {
                                        "Fn::If": [
                                            "HasExternalId",
                                            {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}},
                                            {"Ref": "AWS::NoValue"},
                                        ]
                                    },
                                },
                                # Same-account fallback: instance-profile access.
                                {
                                    "Effect": "Allow",
                                    "Principal": {"Service": "ec2.amazonaws.com"},
                                    "Action": "sts:AssumeRole",
                                },
                            ]
                        }
                    ],
                },
                "ManagedPolicyArns": [{"Ref": "NableReadOnlyPolicy"}],
                "Tags": [
                    {"Key": "ManagedBy", "Value": "nable-finops"},
                    {"Key": "Purpose", "Value": "cost-intelligence-read-only"},
                ],
            },
        },
    },
    "Outputs": {
        "RoleArn": {
            "Description": "ARN of the nable read-only role",
            "Value": {"Fn::GetAtt": ["NableReadOnlyRole", "Arn"]},
        },
        "PolicyArn": {
            "Description": "ARN of the nable read-only managed policy",
            "Value": {"Ref": "NableReadOnlyPolicy"},
        },
    },
}


def org_stackset_template() -> "dict[str, Any]":
    """CloudFormation template for the org-wide read-only role, deployed to every
    member account at once via a service-managed StackSet.

    Same least-privilege read set as the single-account role, but the trust policy
    lets the Organizations management (payer) account assume it, so nable (running
    with management-account credentials) can read every child account. Deploy this
    once at the org root and AWS provisions it into all current and future member
    accounts. Cost data does not need this role at all, the payer's Cost Explorer
    already sees every linked account; this is only for per-account resource scans
    (idle, rightsizing, tagging). See setup_aws_org() for the deploy commands.

    RoleName defaults to FinOpsReadOnly to match discover_org_accounts and the
    FINOPS_ORG_ROLE_NAME env var, so nable assumes the same name it deployed.
    """
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": (
            "Org-wide least-privilege read-only role for nable (finops-mcp), one "
            "per member account via a StackSet. Strictly read-only: no create, "
            "modify, or delete of any kind. The management account assumes it to "
            "read each account's resources for waste and rightsizing scans."
        ),
        "Parameters": {
            "ManagementAccountId": {
                "Type": "String",
                "AllowedPattern": "^[0-9]{12}$",
                "Description": "The Organizations management (payer) account ID that nable runs from and that assumes this role.",
            },
            "RoleName": {
                "Type": "String",
                "Default": "FinOpsReadOnly",
                "Description": "Role name created in each member account. Must match what nable assumes (FINOPS_ORG_ROLE_NAME).",
            },
            **_optional_parameters(),
        },
        "Conditions": _optional_conditions(),
        "Resources": {
            "NableOrgReadOnlyPolicy": {
                "Type": "AWS::IAM::ManagedPolicy",
                "Properties": {
                    "ManagedPolicyName": "NableFinopsOrgReadOnlyPolicy",
                    "Description": _POLICY_DESCRIPTION,
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": _policy_statements(),
                    },
                },
            },
            "NableOrgReadOnlyRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": {"Ref": "RoleName"},
                    "Description": "Read-only role nable's management account assumes to read this account.",
                    "AssumeRolePolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [{
                            "Effect": "Allow",
                            "Principal": {"AWS": {"Fn::Sub": "arn:aws:iam::${ManagementAccountId}:root"}},
                            "Action": "sts:AssumeRole",
                        }],
                    },
                    "ManagedPolicyArns": [{"Ref": "NableOrgReadOnlyPolicy"}],
                    "Tags": [
                        {"Key": "ManagedBy", "Value": "nable-finops"},
                        {"Key": "Purpose", "Value": "cost-intelligence-read-only"},
                    ],
                },
            },
        },
        "Outputs": {
            "RoleArn": {
                "Description": "ARN of the nable read-only role in this account",
                "Value": {"Fn::GetAtt": ["NableOrgReadOnlyRole", "Arn"]},
            },
        },
    }


# The role template above needs you to already have AWS credentials on the box to
# assume the role. Most people who get stuck in setup do NOT, so the one-click
# activation path uses this template instead: it mints a read-only IAM user and an
# access key in their own account. They paste the two outputs into the wizard and
# they are connected, with no pre-existing credentials required.
CLOUDFORMATION_TEMPLATE_KEY: dict[str, Any] = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Description": (
        "Read-only IAM user and access key for nable (finops-mcp). "
        "The NableReadOnlyScan statement is exactly what `nable scan` calls; the "
        "Optional statements add other nable features (billed Cost Explorer "
        "among them) and can each be switched off with a parameter. No create, "
        "modify, or delete permissions of any kind. The access "
        "key id and secret appear in the Outputs tab once: copy them into the "
        "nable setup wizard. You can delete this stack any time to revoke access."
    ),
    "Parameters": {
        "UserName": {
            "Type": "String",
            "Default": "nable-finops-readonly",
            "Description": "Name for the read-only IAM user",
        },
        **_optional_parameters(),
    },
    "Conditions": _optional_conditions(),
    "Resources": {
        "NableReadOnlyPolicy": {
            "Type": "AWS::IAM::ManagedPolicy",
            "Properties": {
                "ManagedPolicyName": "NableFinopsReadOnlyPolicy",
                "Description": _POLICY_DESCRIPTION,
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": _policy_statements(),
                },
            },
        },
        "NableReadOnlyUser": {
            "Type": "AWS::IAM::User",
            "Properties": {
                "UserName": {"Ref": "UserName"},
                "ManagedPolicyArns": [{"Ref": "NableReadOnlyPolicy"}],
                "Tags": [
                    {"Key": "ManagedBy", "Value": "nable-finops"},
                    {"Key": "Purpose", "Value": "cost-intelligence-read-only"},
                ],
            },
        },
        "NableAccessKey": {
            "Type": "AWS::IAM::AccessKey",
            "Properties": {"UserName": {"Ref": "NableReadOnlyUser"}},
        },
    },
    "Outputs": {
        "NableSetupPaste": {
            "Description": (
                "Copy this ONE value and paste it into the nable setup wizard "
                "(it asks for a single paste first). Treat it as a secret (it "
                "stays visible in this stack's Outputs); delete this stack any "
                "time to revoke the key."
            ),
            "Value": {
                "Fn::Join": [
                    ":",
                    [
                        {"Ref": "NableAccessKey"},
                        {"Fn::GetAtt": ["NableAccessKey", "SecretAccessKey"]},
                    ],
                ]
            },
        },
        "AccessKeyId": {
            "Description": "Only needed if the wizard's single-paste prompt does not accept NableSetupPaste above: paste this as the AWS Access Key ID",
            "Value": {"Ref": "NableAccessKey"},
        },
        "SecretAccessKey": {
            "Description": "Only needed if the wizard's single-paste prompt does not accept NableSetupPaste above: paste this as the AWS Secret Access Key. Treat it as a secret (it stays visible in this stack's Outputs); delete this stack any time to revoke the key.",
            "Value": {"Fn::GetAtt": ["NableAccessKey", "SecretAccessKey"]},
        },
        "PolicyArn": {
            "Description": "ARN of the read-only managed policy attached to this user",
            "Value": {"Ref": "NableReadOnlyPolicy"},
        },
    },
}

# The AWS console's quick-create flow only loads templates from an S3 URL, so the
# key template above is published to a public S3 object (see scripts/publish_cfn.py)
# and the live URL is the default below. Overridable via env for testing or a
# custom bucket. _CFN_TEMPLATE_PLACEHOLDER is kept as the "unpublished" sentinel:
# if the default is ever reset to it, quick_create_available() returns False so the
# wizard never advertises a dead one-click link.
_CFN_TEMPLATE_PLACEHOLDER = "https://nable-public.s3.amazonaws.com/cloudformation/readonly-key.json"
_CFN_TEMPLATE_PUBLISHED = "https://getnable-public.s3.us-east-2.amazonaws.com/cloudformation/readonly-key.json"
CFN_KEY_TEMPLATE_S3_URL = os.environ.get("NABLE_CFN_TEMPLATE_URL", _CFN_TEMPLATE_PUBLISHED)


def quick_create_available() -> bool:
    """True only when a real published template URL is configured (not the
    placeholder), so callers never advertise a one-click link that 404s."""
    return bool(CFN_KEY_TEMPLATE_S3_URL) and CFN_KEY_TEMPLATE_S3_URL != _CFN_TEMPLATE_PLACEHOLDER

_TERRAFORM_TEMPLATE = '''\
# ── nable (finops-mcp) read-only IAM role ─────────────────────────────────────
# NableReadOnlyScan is exactly what `nable scan` calls. Each Optional statement
# adds another nable feature; delete the ones you do not want. No write
# permissions of any kind.

resource "aws_iam_policy" "nable_readonly" {{
  name        = "NableFinopsReadOnlyPolicy"
  description = "Read-only. NableReadOnlyScan is exactly what nable scan calls; Optional statements add other features."

  policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [
{statements}
    ]
  }})

  tags = {{
    ManagedBy = "nable-finops"
    Purpose   = "cost-intelligence-read-only"
  }}
}}

resource "aws_iam_role" "nable_readonly" {{
  name        = "NableFinopsReadOnly"
  description = "Read-only role for nable cost intelligence"

  assume_role_policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [{{
      Effect    = "Allow"
      Principal = {{ Service = "ec2.amazonaws.com" }}
      Action    = "sts:AssumeRole"
    }}]
  }})

  tags = {{
    ManagedBy = "nable-finops"
    Purpose   = "cost-intelligence-read-only"
  }}
}}

resource "aws_iam_role_policy_attachment" "nable_readonly" {{
  role       = aws_iam_role.nable_readonly.name
  policy_arn = aws_iam_policy.nable_readonly.arn
}}

output "nable_role_arn" {{
  description = "ARN of the nable read-only role"
  value       = aws_iam_role.nable_readonly.arn
}}
'''


def generate_cloudformation() -> str:
    """Return CloudFormation template JSON string (read-only role)."""
    return json.dumps(CLOUDFORMATION_TEMPLATE, indent=2)


def generate_cloudformation_key() -> str:
    """Return CloudFormation template JSON string (read-only user + access key).

    This is the template behind the one-click connect link. It mints a scoped
    read-only IAM user and an access key the user pastes into the setup wizard.
    """
    return json.dumps(CLOUDFORMATION_TEMPLATE_KEY, indent=2)


def quick_create_url(region: str = "us-east-1", stack_name: str = "nable-readonly") -> str:
    """One-click AWS console URL that opens the read-only-key stack pre-loaded.

    The user reviews the template (read-only, auditable), clicks Create, then
    copies AccessKeyId and SecretAccessKey from the Outputs tab into the wizard.
    Collapses the IAM step from a dozen console clicks to two copy-pastes, and
    works even when the user has no AWS credentials configured locally.
    """
    from urllib.parse import quote

    template_url = quote(CFN_KEY_TEMPLATE_S3_URL, safe="")
    return (
        f"https://console.aws.amazon.com/cloudformation/home?region={region}"
        f"#/stacks/create/review?templateURL={template_url}"
        f"&stackName={stack_name}"
    )


def generate_terraform() -> str:
    """Return Terraform HCL snippet string."""
    def _stmt(sid: str, actions: list[str], comment: str = "") -> str:
        lines = []
        if comment:
            words, line = comment.split(), "      #"
            for w in words:
                if len(line) + len(w) > 78:
                    lines.append(line)
                    line = "      #"
                line += " " + w
            lines.append(line)
        lines.append("      {")
        lines.append(f'        Sid      = "{sid}"')
        lines.append('        Effect   = "Allow"')
        lines.append("        Action   = [")
        lines += [f'          "{a}",' for a in actions]
        lines.append("        ]")
        lines.append('        Resource = "*"')
        lines.append("      },")
        return "\n".join(lines)

    parts = [_stmt("NableReadOnlyScan", _SCAN_ACTIONS,
                   "Exactly what `nable scan` calls (nable scan --dry-run --json).")]
    parts += [_stmt(sid, actions, f"Optional. {what}")
              for sid, _param, what, actions in _OPTIONAL_GROUPS]
    return _TERRAFORM_TEMPLATE.format(statements="\n".join(parts))


# ── Credential scope validator ────────────────────────────────────────────────

def check_credential_scope() -> dict[str, Any]:
    """
    Simulate-call a set of required and dangerous actions via IAM dry-run
    (simulate_principal_policy) to determine whether configured credentials
    are over-provisioned.

    Returns:
        {
            "account_id": str,
            "identity_arn": str,
            "required_allowed": list[str],   # actions that work
            "required_denied": list[str],    # actions that don't work
            "dangerous_allowed": list[str],  # write actions that shouldn't work
            "scoped_correctly": bool,
        }
    """
    try:
        import boto3
    except ImportError:
        return {"error": "boto3 not installed"}

    try:
        sts = boto3.client("sts")
        identity = sts.get_caller_identity()
        account_id = identity["Account"]
        identity_arn = identity["Arn"]
    except Exception as e:
        return {"error": f"Could not get caller identity: {e}"}

    try:
        iam = boto3.client("iam")
        all_actions = _REQUIRED_ACTIONS + [
            "ec2:TerminateInstances",
            "s3:PutObject",
            "iam:CreateUser",
            "lambda:InvokeFunction",
        ]
        resp = iam.simulate_principal_policy(
            PolicySourceArn=identity_arn,
            ActionNames=all_actions,
            ResourceArns=["*"],
        )
        results = {
            r["EvalActionName"]: r["EvalDecision"]
            for r in resp.get("EvaluationResults", [])
        }
    except Exception:
        # simulate_principal_policy requires iam:SimulatePrincipalPolicy
        # which many keys won't have — fall back to a simple allow/deny test
        results = _probe_permissions()

    required_allowed = [a for a in _REQUIRED_ACTIONS if results.get(a) == "allowed"]
    required_denied  = [a for a in _REQUIRED_ACTIONS if results.get(a) != "allowed"]
    dangerous_allowed = [
        a for a in results
        if a not in _REQUIRED_ACTIONS and results[a] == "allowed"
    ]

    return {
        "account_id":       account_id,
        "identity_arn":     identity_arn,
        "required_allowed": required_allowed,
        "required_denied":  required_denied,
        "dangerous_allowed": dangerous_allowed,
        "scoped_correctly": (
            len(required_denied) == 0 and len(dangerous_allowed) == 0
        ),
    }


def _probe_permissions() -> dict[str, str]:
    """
    Fallback: attempt dry-run calls and infer allow/deny from exceptions.
    Only checks a representative subset of actions.
    """
    import boto3
    from botocore.exceptions import ClientError

    results: dict[str, str] = {}

    def _try(fn, action: str) -> None:
        try:
            fn()
            results[action] = "allowed"
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("AccessDenied", "UnauthorizedOperation", "AuthFailure"):
                results[action] = "implicitDeny"
            else:
                # Got a real error back (not auth) — so we're allowed to call it
                results[action] = "allowed"
        except Exception:
            results[action] = "allowed"

    ce = boto3.client("ce")
    _try(
        lambda: ce.get_cost_and_usage(
            TimePeriod={"Start": "2024-01-01", "End": "2024-01-02"},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
        ),
        "ce:GetCostAndUsage",
    )

    ec2 = boto3.client("ec2", region_name="us-east-1")
    _try(
        lambda: ec2.describe_regions(DryRun=True),
        "ec2:DescribeRegions",
    )
    _try(
        lambda: ec2.terminate_instances(InstanceIds=["i-00000000000000000"], DryRun=True),
        "ec2:TerminateInstances",
    )

    s3 = boto3.client("s3")
    try:
        import io
        s3.put_object(Bucket="nable-probe-bucket-does-not-exist", Key="probe", Body=b"")
        results["s3:PutObject"] = "allowed"
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NoSuchBucket", "AccessDenied"):
            results["s3:PutObject"] = "implicitDeny" if code == "AccessDenied" else "allowed"
        else:
            results["s3:PutObject"] = "allowed"
    except Exception:
        results["s3:PutObject"] = "implicitDeny"

    return results


# ── CLI output ────────────────────────────────────────────────────────────────

def print_iam_template(fmt: str = "cloudformation") -> None:
    """Print the template on stdout and everything else on stderr.

    `nable iam-template > nable-iam.json` is the obvious thing to type, and it
    wrote comment lines and a footer around the JSON, so the file did not
    parse and the deploy command below failed on it. Only the template goes
    to stdout now.
    """
    err = sys.stderr
    if fmt == "terraform":
        print("# Terraform: copy into your infra repo", file=err)
        print(generate_terraform())
    else:
        print("CloudFormation template on stdout. Deploy with:", file=err)
        print("  nable iam-template > nable-iam.json", file=err)
        print("  aws cloudformation deploy --template-file nable-iam.json \\", file=err)
        print("    --stack-name nable-readonly --capabilities CAPABILITY_NAMED_IAM", file=err)
        print(generate_cloudformation())

    print(file=err)
    print("NableReadOnlyScan is exactly what `nable scan` calls. Each Optional", file=err)
    print("statement adds another feature and names it; Cost Explorer is billed", file=err)
    print("at $0.01 per request. Every statement is read-only: nable cannot create,", file=err)
    print("modify, terminate, or delete any resource with this policy.", file=err)
