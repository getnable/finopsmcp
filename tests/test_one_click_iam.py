"""Tests for the one-click read-only connect path.

The connect link mints a read-only IAM user + access key via CloudFormation. The
whole pitch is "read-only, auditable," so these tests are the guarantee: the
template must create exactly a user/policy/key, expose the two key outputs, carry
only the canonical read action set, and contain no write/delete permission. The
quick-create URL must point the AWS console at the published template.
"""
import json
import pathlib

from finops.security import iam_setup as I


def _policy_actions(template: dict) -> list:
    res = template["Resources"]["NableReadOnlyPolicy"]["Properties"]
    return res["PolicyDocument"]["Statement"][0]["Action"]


def test_key_template_creates_user_policy_and_access_key():
    tpl = json.loads(I.generate_cloudformation_key())
    types = sorted(r["Type"] for r in tpl["Resources"].values())
    assert types == [
        "AWS::IAM::AccessKey",
        "AWS::IAM::ManagedPolicy",
        "AWS::IAM::User",
    ]


def test_key_template_outputs_the_pasteable_credentials():
    tpl = json.loads(I.generate_cloudformation_key())
    assert set(tpl["Outputs"]) >= {"AccessKeyId", "SecretAccessKey"}
    # The secret must come from the AccessKey resource attribute, not a literal.
    secret = tpl["Outputs"]["SecretAccessKey"]["Value"]
    assert secret == {"Fn::GetAtt": ["NableAccessKey", "SecretAccessKey"]}


def test_key_template_uses_canonical_read_actions():
    actions = _policy_actions(json.loads(I.generate_cloudformation_key()))
    assert actions == I._REQUIRED_ACTIONS


def test_key_template_grants_no_write_or_delete():
    # The read-only promise: no action may match a known mutating prefix.
    actions = _policy_actions(json.loads(I.generate_cloudformation_key()))
    for a in actions:
        for bad in I._DANGEROUS_ACTIONS_PREFIXES:
            assert not a.startswith(bad), f"{a} matches dangerous prefix {bad}"


def test_quick_create_url_points_console_at_published_template(monkeypatch):
    monkeypatch.setattr(I, "CFN_KEY_TEMPLATE_S3_URL", "https://b.s3.amazonaws.com/t.json")
    url = I.quick_create_url(region="us-west-2", stack_name="nable-test")
    assert url.startswith("https://console.aws.amazon.com/cloudformation/home?region=us-west-2")
    assert "#/stacks/create/review?" in url
    # The S3 URL is passed url-encoded so the console parses it as one param.
    assert "templateURL=https%3A%2F%2Fb.s3.amazonaws.com%2Ft.json" in url
    assert "stackName=nable-test" in url


def test_committed_template_matches_source_of_truth():
    # web/cloudformation/readonly-key.json is what users audit and what gets
    # published to S3. It must never drift from the generated template.
    committed = pathlib.Path(__file__).resolve().parent.parent / "web" / "cloudformation" / "readonly-key.json"
    assert committed.exists(), "run scripts/publish_cfn.py --dry-run to regenerate"
    assert json.loads(committed.read_text()) == json.loads(I.generate_cloudformation_key())


def test_one_click_is_published_and_live_by_default():
    """After publishing the template to S3, the one-click path must be ON by
    default (no env var needed), since end users never set NABLE_CFN_TEMPLATE_URL.
    Guards against a silent regression back to the unpublished placeholder."""
    assert I.CFN_KEY_TEMPLATE_S3_URL == I._CFN_TEMPLATE_PUBLISHED
    assert I.CFN_KEY_TEMPLATE_S3_URL != I._CFN_TEMPLATE_PLACEHOLDER
    assert I.quick_create_available() is True
    url = I.quick_create_url()
    assert "getnable-public.s3.us-east-2.amazonaws.com" in url
    assert url.startswith("https://console.aws.amazon.com/cloudformation/home")


def test_connect_key_is_strictly_read_only_no_writes():
    """The one-click connect credential must be 100% read. Specifically guards
    against logs:PutRetentionPolicy (a write) creeping back in, since the template
    advertises 'no create, modify, or delete permissions of any kind' and a
    security-minded user reads this policy on the connect screen."""
    actions = _policy_actions(json.loads(I.generate_cloudformation_key()))
    assert "logs:PutRetentionPolicy" not in actions
    # Every action must be a read verb (Get/Describe/List) or benign STS auth.
    for a in actions:
        verb = a.split(":", 1)[1]
        assert verb.startswith(("Get", "Describe", "List")) or a in (
            "sts:GetCallerIdentity", "sts:AssumeRole",
        ), f"{a} is not a read-only action"
