"""
IAM least-privilege setup for nable (finops-mcp).

Two outputs:
  1. CloudFormation template  — paste into AWS Console or deploy via CLI
  2. Terraform snippet        — drop into your infra repo

Minimum permissions nable ever needs (read-only, no mutations):
  Cost Explorer  → ce:Get*, ce:Describe*, ce:List*
  Compute Opt.   → compute-optimizer:Get*, compute-optimizer:Describe*
  CloudWatch     → cloudwatch:GetMetricStatistics (fallback rightsizing)
  EC2 read       → ec2:DescribeInstances, ec2:DescribeRegions
  Organizations  → organizations:List*, organizations:Describe* (org rollup)
  STS            → sts:GetCallerIdentity (account ID discovery)

Nothing in this list can create, modify, or delete any resource.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

log = logging.getLogger(__name__)

# ── Exact IAM actions required ────────────────────────────────────────────────

_REQUIRED_ACTIONS: list[str] = [
    # Cost Explorer
    "ce:GetCostAndUsage",
    "ce:GetCostForecast",
    "ce:GetReservationUtilization",
    "ce:GetReservationCoverage",
    "ce:GetSavingsPlansPurchaseRecommendation",
    "ce:GetSavingsPlansUtilization",
    "ce:GetSavingsPlansUtilizationDetails",
    "ce:GetRightsizingRecommendation",
    "ce:ListCostAllocationTags",
    "ce:DescribeCostCategoryDefinition",
    # Compute Optimizer
    "compute-optimizer:GetEC2InstanceRecommendations",
    "compute-optimizer:GetLambdaFunctionRecommendations",
    "compute-optimizer:GetECSServiceRecommendations",
    "compute-optimizer:GetEnrollmentStatus",
    "compute-optimizer:GetRecommendationSummaries",
    # CloudWatch (fallback rightsizing)
    "cloudwatch:GetMetricStatistics",
    "cloudwatch:ListMetrics",
    # EC2 (region + instance discovery)
    "ec2:DescribeInstances",
    "ec2:DescribeRegions",
    # Organizations (org rollup — optional but harmless to include)
    "organizations:ListAccounts",
    "organizations:ListRoots",
    "organizations:ListOrganizationalUnitsForParent",
    "organizations:DescribeOrganization",
    "organizations:DescribeAccount",
    # STS (account ID)
    "sts:GetCallerIdentity",
]

# Actions that WOULD indicate over-provisioned credentials
_DANGEROUS_ACTIONS_PREFIXES = [
    "ec2:Create", "ec2:Delete", "ec2:Modify", "ec2:Run", "ec2:Stop", "ec2:Terminate",
    "s3:Put", "s3:Delete", "s3:Create",
    "iam:Create", "iam:Delete", "iam:Attach", "iam:Detach", "iam:Put", "iam:Update",
    "rds:Create", "rds:Delete", "rds:Modify",
    "lambda:Create", "lambda:Delete", "lambda:Update", "lambda:Invoke",
]

CLOUDFORMATION_TEMPLATE: dict[str, Any] = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Description": (
        "Least-privilege read-only IAM role for nable (finops-mcp). "
        "Grants access to Cost Explorer, Compute Optimizer, CloudWatch metrics, "
        "EC2 describe, and Organizations read. No write permissions of any kind."
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
                "AWS account ID allowed to assume this role (leave blank "
                "for same-account key-based access)"
            ),
        },
    },
    "Resources": {
        "NableReadOnlyPolicy": {
            "Type": "AWS::IAM::ManagedPolicy",
            "Properties": {
                "ManagedPolicyName": "NableFinopsReadOnlyPolicy",
                "Description": "Exact permissions nable needs — nothing more.",
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "NableReadOnly",
                            "Effect": "Allow",
                            "Action": _REQUIRED_ACTIONS,
                            "Resource": "*",
                        }
                    ],
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
                            "Effect": "Allow",
                            "Principal": {"Service": "ec2.amazonaws.com"},
                            "Action": "sts:AssumeRole",
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

_TERRAFORM_TEMPLATE = '''\
# ── nable (finops-mcp) least-privilege IAM role ───────────────────────────────
# Grants read-only access to Cost Explorer, Compute Optimizer, CloudWatch
# metrics, EC2 describe, and Organizations. No write permissions of any kind.

locals {{
  nable_actions = {actions}
}}

resource "aws_iam_policy" "nable_readonly" {{
  name        = "NableFinopsReadOnlyPolicy"
  description = "Exact permissions nable needs — nothing more."

  policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [{{
      Sid      = "NableReadOnly"
      Effect   = "Allow"
      Action   = local.nable_actions
      Resource = "*"
    }}]
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
    """Return CloudFormation template JSON string."""
    return json.dumps(CLOUDFORMATION_TEMPLATE, indent=2)


def generate_terraform() -> str:
    """Return Terraform HCL snippet string."""
    actions_json = json.dumps(_REQUIRED_ACTIONS, indent=4)
    # indent the list to look nice inside the locals block
    indented = "\n".join(
        "  " + line if i > 0 else line
        for i, line in enumerate(actions_json.splitlines())
    )
    return _TERRAFORM_TEMPLATE.format(actions=indented)


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
    """Print IAM template to stdout in requested format."""
    print()
    if fmt == "terraform":
        print("# ── Terraform — copy into your infra repo ──────────────────")
        print(generate_terraform())
    else:
        print("# ── CloudFormation — deploy with: ──────────────────────────")
        print("#   aws cloudformation deploy \\")
        print("#     --template-file nable-iam.json \\")
        print("#     --stack-name nable-readonly \\")
        print("#     --capabilities CAPABILITY_NAMED_IAM")
        print()
        print(generate_cloudformation())

    print()
    print("─" * 60)
    print("  These permissions are read-only. nable cannot create,")
    print("  modify, or delete any AWS resource with this policy.")
    print("─" * 60)
    print()
