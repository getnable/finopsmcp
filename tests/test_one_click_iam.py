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
    """Every action across the scan statement and the optional ones (each
    optional statement sits behind an Fn::If on its parameter)."""
    res = template["Resources"]["NableReadOnlyPolicy"]["Properties"]
    out: list = []
    for st in res["PolicyDocument"]["Statement"]:
        if "Fn::If" in st:
            st = st["Fn::If"][1]
        out += st["Action"]
    return out


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


def test_key_template_outputs_a_single_combined_paste():
    # tokenmaxxing-style speed fix: one output combining both values means the
    # wizard's fast path is a single copy-paste instead of two, each with its
    # own validation loop.
    tpl = json.loads(I.generate_cloudformation_key())
    combined = tpl["Outputs"]["NableSetupPaste"]["Value"]
    assert combined == {
        "Fn::Join": [
            ":",
            [
                {"Ref": "NableAccessKey"},
                {"Fn::GetAtt": ["NableAccessKey", "SecretAccessKey"]},
            ],
        ]
    }


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
    # cloudformation/readonly-key.json is what users audit and what gets
    # published to S3. It must never drift from the generated template.
    committed = pathlib.Path(__file__).resolve().parent.parent / "cloudformation" / "readonly-key.json"
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
    # sts:AssumeRole is an escalation primitive (it can assume a role whose trust
    # policy allows the account root), not a read. The connect key must not carry it.
    assert "sts:AssumeRole" not in actions
    # Every action must be a read verb (Get/Describe/List) or the one benign STS
    # identity read. No sts:Assume*, no writes.
    for a in actions:
        verb = a.split(":", 1)[1]
        assert verb.startswith(("Get", "Describe", "List")) or a == "sts:GetCallerIdentity", \
            f"{a} is not a read-only action"


# ── commitments: readable, never buyable ─────────────────────────────────────

_PURCHASE_PREFIXES = ("rds:Purchase", "ec2:Purchase", "savingsplans:Create")
_PURCHASE_ACTIONS = ("rds:PurchaseReservedDBInstancesOffering",
                     "ec2:PurchaseReservedInstancesOffering", "savingsplans:CreateSavingsPlan")


def test_buying_a_commitment_is_a_dangerous_prefix():
    """The enterprise derives one read-only probe for each prefix from this
    list, so it passes against this release and the one before."""
    assert set(_PURCHASE_PREFIXES) <= set(I._DANGEROUS_ACTIONS_PREFIXES)
    for action in _PURCHASE_ACTIONS:
        assert any(action.startswith(p) for p in I._DANGEROUS_ACTIONS_PREFIXES), action
    for a in I._REQUIRED_ACTIONS:
        assert not any(a.startswith(p) for p in _PURCHASE_PREFIXES), a


def test_the_commitments_statement_only_describes():
    [group] = [g for g in I._OPTIONAL_GROUPS if g[0] == "OptionalCommitments"]
    _sid, param, what, actions = group
    assert sorted(actions) == ["ec2:DescribeReservedInstances",
                               "elasticache:DescribeReservedCacheNodes",
                               "rds:DescribeReservedDBInstances",
                               "savingsplans:DescribeSavingsPlans"]
    assert all(a.split(":", 1)[1].startswith("Describe") for a in actions)
    assert param == "IncludeCommitments"
    assert "Unlocks" in what and "nable commitments status" in what
    assert set(actions) <= set(I._REQUIRED_ACTIONS)


def _stubbed(service: str):
    import boto3
    return boto3.client(service, region_name="us-east-1", aws_access_key_id="testing",
                        aws_secret_access_key="testing")  # pragma: allowlist secret


def test_check_credential_scope_names_commitment_purchases_it_could_make(monkeypatch):
    import boto3
    from botocore.stub import ANY, Stubber

    sts, iam = _stubbed("sts"), _stubbed("iam")
    sent: dict = {}
    iam.meta.events.register("provide-client-params.iam.SimulatePrincipalPolicy",
                             lambda params, **_: sent.update(params))
    arn = "arn:aws:iam::123456789012:user/ci"
    allowed = set(I._REQUIRED_ACTIONS) | set(_PURCHASE_ACTIONS)

    def answer(params):
        return {"EvaluationResults": [
            {"EvalActionName": a, "EvalDecision": "allowed" if a in allowed else "implicitDeny"}
            for a in params]}

    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: {"sts": sts, "iam": iam}[name])
    with Stubber(sts) as s_sts, Stubber(iam) as s_iam:
        s_sts.add_response("get_caller_identity",
                           {"Account": "123456789012", "Arn": arn, "UserId": "AIDA0"})
        # The reply allows the scan's reads and the three purchases; the
        # hook above records which actions were asked about.
        names = list(I._REQUIRED_ACTIONS) + ["ec2:TerminateInstances", "s3:PutObject",
                                             "iam:CreateUser", "lambda:InvokeFunction",
                                             *_PURCHASE_ACTIONS]
        s_iam.add_response("simulate_principal_policy", answer(names),
                           {"PolicySourceArn": arn, "ActionNames": ANY, "ResourceArns": ["*"]})
        out = I.check_credential_scope()
    assert set(_PURCHASE_ACTIONS) <= set(sent["ActionNames"])
    assert sorted(out["dangerous_allowed"]) == sorted(_PURCHASE_ACTIONS)
    assert out["scoped_correctly"] is False


# Every commitment purchase the guard asks about, as its IAM action.
_ALL_PURCHASE_ACTIONS = (
    *_PURCHASE_ACTIONS,
    "elasticache:PurchaseReservedCacheNodesOffering", "memorydb:PurchaseReservedNodesOffering",
    "redshift:PurchaseReservedNodeOffering", "redshift:AcceptReservedNodeExchange",
    "es:PurchaseReservedElasticsearchInstanceOffering", "es:PurchaseReservedInstanceOffering",
    "ec2:AcceptReservedInstancesExchangeQuote", "ec2:PurchaseCapacityBlock",
    "ec2:PurchaseHostReservation", "redshift-serverless:CreateReservation",
    "bedrock:CreateProvisionedModelThroughput", "sagemaker:CreateTrainingPlan",
    "sagemaker:ExtendTrainingPlan", "medialive:PurchaseOffering",
    "mediaconnect:PurchaseOffering", "devicefarm:PurchaseOffering", "outposts:CreateOrder",
    "glacier:PurchaseProvisionedCapacity",
)


def test_every_commitment_purchase_is_dangerous_and_probed():
    for action in _ALL_PURCHASE_ACTIONS:
        assert any(action.startswith(p) for p in I._DANGEROUS_ACTIONS_PREFIXES), action
    purchase_prefixes = I._DANGEROUS_ACTIONS_PREFIXES[
        I._DANGEROUS_ACTIONS_PREFIXES.index("rds:Purchase"):]
    for prefix in purchase_prefixes:
        assert any(p.startswith(prefix) for p in I._COMMITMENT_PURCHASE_PROBES), prefix
    import botocore.session
    session = botocore.session.get_session()
    for probe in I._COMMITMENT_PURCHASE_PROBES:
        service, op = probe.split(":", 1)
        assert op in session.get_service_model(service).operation_names, probe


def _scope(monkeypatch, *, simulate=None, probes=None):
    """check_credential_scope against stubbed STS and IAM: `simulate` is the
    set of actions the simulator allows, or None when the key may not call
    it; `probes` is what _probe_permissions found, for that case."""
    import boto3
    from botocore.stub import ANY, Stubber

    sts, iam = _stubbed("sts"), _stubbed("iam")
    sent: dict = {}
    iam.meta.events.register("provide-client-params.iam.SimulatePrincipalPolicy",
                             lambda params, **_: sent.update(params))
    arn = "arn:aws:iam::123456789012:user/ci"
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: {"sts": sts, "iam": iam}[name])
    if probes is not None:
        monkeypatch.setattr(I, "_probe_permissions", lambda: dict(probes))
    with Stubber(sts) as s_sts, Stubber(iam) as s_iam:
        s_sts.add_response("get_caller_identity",
                           {"Account": "123456789012", "Arn": arn, "UserId": "AIDA0"})
        if simulate is None:
            s_iam.add_client_error("simulate_principal_policy", "AccessDenied",
                                   http_status_code=403)
        else:
            names = list(dict.fromkeys(
                [*I._REQUIRED_ACTIONS, "ec2:TerminateInstances", "s3:PutObject",
                 "iam:CreateUser", "lambda:InvokeFunction", *I._COMMITMENT_PURCHASE_PROBES]))
            s_iam.add_response("simulate_principal_policy", {"EvaluationResults": [
                {"EvalActionName": a, "EvalDecision": "allowed" if a in simulate
                 else "implicitDeny"} for a in names]},
                {"PolicySourceArn": arn, "ActionNames": ANY, "ResourceArns": ["*"]})
        out = I.check_credential_scope()
    return out, sent


def test_a_key_that_can_buy_reserved_nodes_or_exchange_is_not_scoped(monkeypatch):
    buys = {"elasticache:PurchaseReservedCacheNodesOffering",
            "redshift:PurchaseReservedNodeOffering", "ec2:AcceptReservedInstancesExchangeQuote"}
    out, sent = _scope(monkeypatch, simulate=set(I._REQUIRED_ACTIONS) | buys)
    assert buys <= set(sent["ActionNames"])
    assert set(out["dangerous_allowed"]) == buys
    assert out["scoped_correctly"] is False


def test_a_key_that_cannot_simulate_is_not_reported_clean(monkeypatch):
    """The fallback probes only a few actions. What it could not reach is not
    read, never denied and never allowed, and the key is not proven clean."""
    probed = {a: "allowed" for a in I._REQUIRED_ACTIONS}
    probed.update({"ec2:TerminateInstances": "implicitDeny", "s3:PutObject": "implicitDeny",
                   "ec2:PurchaseReservedInstancesOffering": "implicitDeny"})
    out, _ = _scope(monkeypatch, probes=probed)
    assert out["scoped_correctly"] is None
    assert "rds:PurchaseReservedDBInstancesOffering" in out["not_read"]
    assert "ec2:PurchaseReservedInstancesOffering" not in out["not_read"]
    assert out["dangerous_allowed"] == [] and out["required_denied"] == []


def test_the_fallback_asks_ec2_about_a_purchase_as_a_dry_run(monkeypatch):
    import boto3
    from botocore.stub import Stubber

    ce, ec2, s3 = _stubbed("ce"), _stubbed("ec2"), _stubbed("s3")
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: {"ce": ce, "ec2": ec2,
                                                                 "s3": s3}[name])
    with Stubber(ce) as s_ce, Stubber(ec2) as s_ec2, Stubber(s3) as s_s3:
        s_ce.add_response("get_cost_and_usage", {"ResultsByTime": []})
        s_ec2.add_client_error("describe_regions", "DryRunOperation")
        s_ec2.add_client_error("terminate_instances", "UnauthorizedOperation")
        s_ec2.add_client_error("purchase_reserved_instances_offering", "DryRunOperation",
                               expected_params={
                                   "InstanceCount": 1, "DryRun": True,
                                   "ReservedInstancesOfferingId":
                                       "00000000-0000-0000-0000-000000000000"})
        s_s3.add_client_error("put_object", "AccessDenied")
        got = I._probe_permissions()
    assert got["ec2:PurchaseReservedInstancesOffering"] == "allowed"
    assert got["ec2:TerminateInstances"] == "implicitDeny"
