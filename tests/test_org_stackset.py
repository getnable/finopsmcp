"""The org StackSet role: one read-only role deployed to every member account, so
onboarding a 50-account org is one CloudFormation deploy, not fifty grants. These
lock in that the role is strictly read-only and trusts the management account (so
nable can assume into every child), matching what discover_org_accounts assumes.
"""
from __future__ import annotations

from finops.security.iam_setup import org_stackset_template, _REQUIRED_ACTIONS


def test_template_role_trusts_management_account():
    tpl = org_stackset_template()
    role = tpl["Resources"]["NableOrgReadOnlyRole"]["Properties"]
    principal = role["AssumeRolePolicyDocument"]["Statement"][0]["Principal"]
    # Trust the management account root via a parameter, so it works in every account.
    assert principal["AWS"] == {"Fn::Sub": "arn:aws:iam::${ManagementAccountId}:root"}
    assert "ManagementAccountId" in tpl["Parameters"]


def test_role_name_defaults_to_what_nable_assumes():
    # discover_org_accounts / FINOPS_ORG_ROLE_NAME default is FinOpsReadOnly; the
    # deployed role must use the same name or nable would assume a role that isn't there.
    tpl = org_stackset_template()
    assert tpl["Parameters"]["RoleName"]["Default"] == "FinOpsReadOnly"


def test_template_is_strictly_read_only():
    tpl = org_stackset_template()
    actions = tpl["Resources"]["NableOrgReadOnlyPolicy"]["Properties"]["PolicyDocument"]["Statement"][0]["Action"]
    assert actions == _REQUIRED_ACTIONS
    banned = (":Create", ":Delete", ":Modify", ":Put", ":Run", ":Terminate", ":Update", "sts:Assume")
    for a in actions:
        assert not any(b.lower() in a.lower() for b in banned), f"write/escalation action leaked in: {a}"
    # It can read costs and describe resources, the two things a member-account scan needs.
    assert "ce:GetCostAndUsage" in actions
    assert "ec2:DescribeInstances" in actions


def test_template_is_valid_cloudformation_shape():
    tpl = org_stackset_template()
    assert tpl["AWSTemplateFormatVersion"] == "2010-09-09"
    assert set(tpl["Resources"]) == {"NableOrgReadOnlyPolicy", "NableOrgReadOnlyRole"}
    assert "RoleArn" in tpl["Outputs"]
