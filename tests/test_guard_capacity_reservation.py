# SPDX-License-Identifier: Apache-2.0
"""EC2 Capacity Reservations in the guard.

`aws ec2 create-capacity-reservation` holds capacity in one Availability Zone
and bills it at the on-demand rate whether or not anything runs in it. With a
commitment duration (a future-dated reservation) it is held, and billed, for
at least that long; the guard let either through without a word, through the
CLI, the hook, MCP and Python alike.

What has to stay true:
  - with a commitment duration it is a commitment purchase: it asks in
    `nable guard check`, the hook, call_aws and use_aws, and through boto3,
    botocore, `_make_api_call`, `python -m awscli`, heredocs and pipes; no
    policy, pack, learned threshold, one-time approval or allow-list makes it
    an allow, and a pack's deny still denies
  - its figure is count x the cited on-demand hourly rate x the duration's
    hours when the guard's table lists the type, and otherwise there is no
    figure and the ask says why
  - without one it is priced the way run-instances is (count x hourly x 730
    a month), or for its own hours when it is limited to end within a month,
    judged on the launch threshold, and the reason says it bills from
    creation whether or not anything runs in it; anything but Linux/UNIX on
    shared tenancy gets no figure, and asks
  - a fleet asks with no figure
  - a dry run excuses it only as an argument of its own
  - every verb and parameter the guard reads is in botocore's ec2 model
  - ec2:CreateCapacityReservation is a dangerous action for a read-only key,
    with its own simulator probe, and `nable setup aws --check-scope` names it
"""
from __future__ import annotations

import io
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import botocore.session
import pytest
from botocore import xform_name

import finops.guard as g
from finops import ai_budget, guard_adapters, guard_approvals, org, policy
from finops.aws_prices import EC2_HOURLY, HOURS_PER_MONTH
from finops.org.cli import _who as human

ROOT = Path(__file__).resolve().parents[1]
BASE = ("aws ec2 create-capacity-reservation --instance-type p5.48xlarge "
        "--instance-platform Linux/UNIX --availability-zone us-east-1a --instance-count 8")
ONE_YEAR = 31_536_000
COMMIT = f"{BASE} --commitment-duration {ONE_YEAR}"
FLEET = ("aws ec2 create-capacity-reservation-fleet --total-target-capacity 8 "
         "--instance-type-specifications InstanceType=p5.48xlarge,InstancePlatform=Linux/UNIX,"
         "Weight=1")
RATE = EC2_HOURLY["p5.48xlarge"]                 # the cited row, $55.04 in us-east-1
TERM_TOTAL = 8 * RATE * 8760
MONTHLY = 8 * RATE * HOURS_PER_MONTH
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
PARAMS = {"InstanceType": "p5.48xlarge", "InstancePlatform": "Linux/UNIX",
          "AvailabilityZone": "us-east-1a", "InstanceCount": 8}


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_VELOCITY_CAP_USD",
                "FINOPS_POLICY_LOOP_COUNT", "FINOPS_GUARD_TEAM", "FINOPS_GUARD_ACCOUNT",
                "FINOPS_POLICY_ON_BUDGET_BREACH", "FINOPS_POLICY_FILE",
                *guard_adapters._COPILOT_CLOUD_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "no-accounts.yaml"))
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})
    monkeypatch.setattr(g, "_utcnow", lambda: NOW, raising=False)


def _guard_check(capsys, cmd: str) -> dict:
    from finops import setup_wizard
    try:
        setup_wizard.main(["guard", "check", "--json", "--command", cmd])
    except SystemExit as e:
        assert e.code in (0, 1, None)
    return json.loads(capsys.readouterr().out)


def _hook(cmd: str) -> dict:
    out = io.StringIO()
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert g.run_hook(stdin=io.StringIO(json.dumps(payload)), stdout=out) == 0
    return json.loads(out.getvalue())["hookSpecificOutput"]


def _use_aws(operation: str, params: dict) -> dict | None:
    return g.gate_mcp_call("mcp__q__use_aws", {"service_name": "ec2", "operation_name": operation,
                                               "parameters": params, "region": "us-east-1"},
                           record=False)


def _usd(x: float) -> str:
    return f"${x:,.0f}"


# ── pinned to botocore's ec2 model ────────────────────────────────────────────

def _ec2():
    return botocore.session.get_session().get_service_model("ec2")


@pytest.mark.parametrize("operation,members", [
    ("CreateCapacityReservation", ("InstanceType", "InstancePlatform", "AvailabilityZone",
                                   "AvailabilityZoneId", "InstanceCount", "Tenancy",
                                   "EndDate", "EndDateType", "StartDate",
                                   "CommitmentDuration", "DryRun")),
    ("CreateCapacityReservationFleet", ("TotalTargetCapacity", "InstanceTypeSpecifications",
                                        "DryRun")),
    ("ModifyCapacityReservation", ("CapacityReservationId",)),
])
def test_each_verb_and_parameter_is_in_botocore(operation, members):
    model = _ec2()
    assert operation in model.operation_names
    shape = model.operation_model(operation).input_shape
    for name in members:
        assert name in shape.members, name
    verb = xform_name(operation, "-")
    assert re.search(rf"\baws ec2 {verb}\b", {"CreateCapacityReservation": BASE,
                                              "CreateCapacityReservationFleet": FLEET,
                                              "ModifyCapacityReservation":
                                              "aws ec2 modify-capacity-reservation"}[operation])


def test_the_flags_and_values_the_pricer_reads_are_botocore_s():
    shape = _ec2().operation_model("CreateCapacityReservation").input_shape.members
    for flag, member in (("instance-type", "InstanceType"),
                         ("instance-platform", "InstancePlatform"),
                         ("availability-zone", "AvailabilityZone"),
                         ("instance-count", "InstanceCount"), ("tenancy", "Tenancy"),
                         ("end-date-type", "EndDateType"), ("end-date", "EndDate"),
                         ("commitment-duration", "CommitmentDuration")):
        assert xform_name(member, "-") == flag and member in shape
    assert "Linux/UNIX" in shape["InstancePlatform"].enum
    assert {"Windows", "Red Hat Enterprise Linux"} <= set(shape["InstancePlatform"].enum)
    assert set(shape["Tenancy"].enum) == {"default", "dedicated"}
    assert set(shape["EndDateType"].enum) == {"unlimited", "limited"}
    assert shape["CommitmentDuration"].type_name == "long"           # seconds
    assert xform_name("CreateCapacityReservation") == "create_capacity_reservation"
    assert xform_name("CreateCapacityReservationFleet") == "create_capacity_reservation_fleet"


def test_the_commitment_wording_is_aws_s_own():
    """The ask quotes the API reference's words for a commitment duration and
    claims nothing more: that it cannot be cancelled is the EC2 User Guide's
    to say, and that page has not been checked."""
    doc = _ec2().operation_model("CreateCapacityReservation").input_shape.members[
        "CommitmentDuration"].documentation
    text = " ".join(re.sub(r"<[^>]+>", " ", doc).split())
    quoted = re.search(r'"([^"]+)"', g.CAPACITY_COMMITMENT_TERMS).group(1)
    assert quoted in text
    assert "cancel" not in g.CAPACITY_COMMITMENT_TERMS.lower()


def test_the_sdk_method_names_the_guard_reads_are_botocore_s():
    for p in g.COMMITMENT_PURCHASES:
        assert g._snake(p.operation) == xform_name(p.operation), p


def test_it_is_in_the_commitment_table_with_its_flag():
    [p] = [p for p in g.COMMITMENT_PURCHASES if p.operation == "CreateCapacityReservation"]
    assert (p.service, p.verb, p.when) == ("ec2", "create-capacity-reservation",
                                           "--commitment-duration")
    assert xform_name(p.operation, "-") == p.verb
    assert xform_name("CommitmentDuration", "-") == p.when.lstrip("-")


# ── with a commitment duration: a commitment purchase ────────────────────────

def test_classified_as_a_commitment_purchase():
    assert g.classify_command(COMMIT) == ("one_way", "purchase_commitment")
    assert g.classify_command(COMMIT.replace("aws ", "aws --profile prod --region us-east-1 ", 1)
                              ) == ("one_way", "purchase_commitment")
    assert g.classify_command(BASE) == ("two_way", "infra_apply")
    assert g.classify_command(FLEET) == ("two_way", "infra_apply")


def test_guard_check_asks_with_the_term_figure(capsys):
    doc = _guard_check(capsys, COMMIT)
    assert doc["decision"] == "ask" and doc["action_type"] == "purchase_commitment"
    assert "This would buy a commitment" in doc["reason"]
    assert _usd(TERM_TOTAL) in doc["reason"]
    assert "8,760-hour commitment" in doc["reason"]
    est = doc["estimate"]
    assert est["total_usd"] == round(8 * RATE * 8760, 2) and est["monthly_usd"] is None
    assert est["hourly_usd"] == RATE and est["count"] == 8 and est["commitment_hours"] == 8760


def test_the_hook_asks():
    d = _hook(COMMIT)
    assert d["permissionDecision"] == "ask"
    assert "buy a commitment" in d["permissionDecisionReason"]
    assert _usd(TERM_TOTAL) in d["permissionDecisionReason"]


def test_mcp_call_aws_and_use_aws_ask():
    v = g.gate_mcp_call("mcp__aws-api__call_aws", {"cli_command": COMMIT}, record=False)
    assert v["decision"] == "ask" and v["action_type"] == "purchase_commitment"
    v = _use_aws("CreateCapacityReservation", {**PARAMS, "CommitmentDuration": ONE_YEAR})
    assert v["decision"] == "ask" and v["action_type"] == "purchase_commitment"
    assert _usd(TERM_TOTAL) in v["reason"]
    v = _use_aws("create-capacity-reservation", {**PARAMS, "CommitmentDuration": ONE_YEAR})
    assert v["decision"] == "ask" and v["action_type"] == "purchase_commitment"


def test_the_ask_quotes_aws_and_never_says_it_cannot_be_cancelled():
    for v in (g.gate_command(COMMIT, record=False),
              _use_aws("CreateCapacityReservation", {**PARAMS, "CommitmentDuration": ONE_YEAR})):
        assert g.CAPACITY_COMMITMENT_TERMS in v["reason"]
        assert "cancel" not in v["reason"].lower()
        assert "while it is active, whether or not anything runs in it" in v["reason"]


def test_without_the_rate_in_the_table_there_is_no_figure_and_it_says_why():
    # p5e.48xlarge has no us-east-1 on-demand row (aws_prices leaves it out).
    assert "p5e.48xlarge" not in EC2_HOURLY
    v = g.gate_command(COMMIT.replace("p5.48xlarge", "p5e.48xlarge"), record=False)
    assert v["decision"] == "ask" and v["action_type"] == "purchase_commitment"
    assert "$" not in v["reason"] and "estimate" not in v
    assert "No figure: the guard's table has no on-demand rate for p5e.48xlarge" in v["reason"]
    # A size the table can only scale from its family prices a launch, never
    # a commitment.
    assert "c6i.32xlarge" not in EC2_HOURLY
    scaled = COMMIT.replace("p5.48xlarge", "c6i.32xlarge")
    v = g.gate_command(scaled, record=False)
    assert "$" not in v["reason"] and "no on-demand rate for c6i.32xlarge" in v["reason"]
    v = g.gate_command(scaled.replace(f" --commitment-duration {ONE_YEAR}", ""), record=False)
    assert v["decision"] == "ask" and "scaled from the c6i sizes" in v["reason"]


@pytest.mark.parametrize("change,why", [
    (("Linux/UNIX", "Windows"), "its --instance-platform is not Linux/UNIX"),
    (("--instance-platform Linux/UNIX", "--instance-platform 'Red Hat Enterprise Linux'"),
     "its --instance-platform is not Linux/UNIX"),
    (("--instance-count 8", "--instance-count 8 --tenancy dedicated"),
     "it reserves dedicated tenancy"),
])
def test_a_commitment_off_linux_or_shared_tenancy_has_no_figure(change, why):
    v = g.gate_command(COMMIT.replace(*change), record=False)
    assert v["decision"] == "ask" and v["action_type"] == "purchase_commitment"
    assert "$" not in v["reason"] and why in v["reason"]


@pytest.fixture
def loose_policy(monkeypatch):
    monkeypatch.setitem(policy.DEFAULT_POLICY, "escalate_one_way_doors", False)
    monkeypatch.setenv("FINOPS_POLICY_MAX_AUTO_USD", "1000000000000")
    monkeypatch.setenv("FINOPS_POLICY_VELOCITY_CAP_USD", "0")
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS",
                       ",".join([*policy.TWO_WAY_DOORS, "purchase_commitment"]))


def test_no_policy_turns_it_into_an_allow(loose_policy):
    for v in (g.gate_command(COMMIT, record=False),
              g.gate_mcp_call("mcp__aws-api__call_aws", {"cli_command": COMMIT}, record=False),
              _use_aws("CreateCapacityReservation", {**PARAMS, "CommitmentDuration": ONE_YEAR})):
        assert v is not None and v["decision"] == "ask", v
        assert "cannot be cancelled" not in v["reason"]


def test_a_learned_threshold_does_not_allow_it(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "infra").mkdir()
    org.set_fact(org.make_fact("owner", "repo_path:repo//infra", {"team": "payments"},
                               source="human"), human("maria"))
    org.set_fact(org.make_fact("threshold", "team:payments",
                               {"max_auto_monthly_usd": 1e12, "velocity_cap_usd": 1e12},
                               source="human"), human("maria"))
    where = str(repo / "infra")
    # The threshold is in force: the same reservation without a term passes.
    assert g.gate_command(BASE, cwd=where, record=False) is None
    assert g.gate_command(COMMIT, cwd=where, record=False)["decision"] == "ask"


def _pack(monkeypatch, verdict: str):
    from finops import guard_packs
    from finops.packs.content import parse_guard_rules
    problems: list = []
    rules = parse_guard_rules({"rules": [{"id": "reserving", "pattern": "capacity-reservation",
                                          "verdict": verdict, "reason": "not this quarter"}]},
                              "g.yaml", problems)
    assert not problems
    monkeypatch.setattr(guard_packs, "state", lambda: {"rules": rules, "guard_problems": []})


def test_a_pack_deny_still_denies_and_a_pack_ask_stays_an_ask(monkeypatch, loose_policy):
    _pack(monkeypatch, "deny")
    v = g.gate_command(COMMIT, record=False)
    assert v["decision"] == "deny" and "not this quarter" in v["reason"]
    _pack(monkeypatch, "ask")
    assert g.gate_command(COMMIT, record=False)["decision"] == "ask"


def _codex(command: str, cwd) -> dict:
    out, err = io.StringIO(), io.StringIO()
    payload = {"hook_event_name": "PreToolUse", "turn_id": "t1", "session_id": "s1",
               "tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    assert guard_adapters.run_hook(stdin=io.StringIO(json.dumps(payload)), stdout=out,
                                   stderr=err) == 0
    return json.loads(out.getvalue())["hookSpecificOutput"]


def test_a_one_time_approval_never_lets_it_through(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    d = _codex(COMMIT, work)
    assert d["permissionDecision"] == "deny"
    assert "never approved out of band" in d["permissionDecisionReason"]
    assert "nable guard approve " not in d["permissionDecisionReason"]
    aid = guard_approvals.pending("shell", COMMIT, cwd=str(work), harness="codex",
                                  session="s1", tool="Bash", verdict={"reason": "x"})
    guard_approvals.approve(aid, human("maria"))
    assert _codex(COMMIT, work)["permissionDecision"] == "deny"
    from finops import guard_ledger
    records = [json.loads(line) for line in guard_ledger.ledger_path().read_text().splitlines()]
    assert records and all(not r.get("approved_out_of_band") for r in records)


def test_a_verdict_layer_that_allows_is_overruled(monkeypatch):
    monkeypatch.setattr(g, "_policy_verdict", lambda command, hit, **kw: {
        "decision": "allow", "action_type": hit[1], "door": hit[0]})
    v = g.gate_command(COMMIT, record=False)
    assert v["decision"] == "ask" and v["action_type"] == "purchase_commitment"
    assert "whatever the policy" in v["reason"] and g.CAPACITY_COMMITMENT_TERMS in v["reason"]


def test_input_the_guard_does_not_read_may_hold_a_term():
    """`--cli-input-json file://...` may carry a CommitmentDuration: the floor
    holds, and the ask says what it does not know. Inline input that names one
    is a commitment, and inline input without one is not."""
    v = g.gate_command("aws ec2 create-capacity-reservation --cli-input-json file://cr.json",
                       record=False)
    assert v["decision"] == "ask" and v["action_type"] == "purchase_commitment"
    assert "may buy a commitment" in v["reason"] and "does not read" in v["reason"]
    assert "--cli-input-json" in v["reason"] and "$" not in v["reason"]
    inline = ("aws ec2 create-capacity-reservation --cli-input-json "
              "'{\"InstanceType\": \"p5.48xlarge\", \"CommitmentDuration\": 31536000}'")
    assert g.classify_command(inline) == ("one_way", "purchase_commitment")
    plain = ("aws ec2 create-capacity-reservation --cli-input-json "
             "'{\"InstanceType\": \"p5.48xlarge\", \"InstanceCount\": 8}'")
    assert g.classify_command(plain) == ("two_way", "infra_apply")
    assert g.gate_command(plain, record=False)["decision"] == "ask"


# ── without one: priced like a launch ────────────────────────────────────────

def test_without_a_term_it_is_priced_like_run_instances_and_judged_on_the_threshold():
    v = g.gate_command(BASE, record=False)
    assert v["decision"] == "ask" and v["action_type"] == "infra_apply"
    assert v["monthly_delta_usd"] == round(MONTHLY, 2)
    assert v["estimate"]["hourly_usd"] == RATE and v["estimate"]["count"] == 8
    assert f"{_usd(MONTHLY)}/mo" in v["reason"]
    assert "over your $500 auto threshold" in v["reason"]
    assert g.CAPACITY_BILLING in v["reason"]
    assert "bills from when it is created, whether or not anything runs in it" in v["reason"]
    # The same figure the launch of the same instances gets.
    launch = g.gate_command("aws ec2 run-instances --instance-type p5.48xlarge --count 8",
                            record=False)
    assert launch["monthly_delta_usd"] == v["monthly_delta_usd"]


def test_a_small_one_under_the_threshold_passes_like_a_small_launch(monkeypatch):
    small = BASE.replace("p5.48xlarge", "t3.micro").replace("--instance-count 8",
                                                            "--instance-count 1")
    assert g.gate_command(small, record=False) is None
    monkeypatch.setenv("FINOPS_POLICY_MAX_AUTO_USD", "5")
    v = g.gate_command(small, record=False)
    assert v["decision"] == "ask" and g.CAPACITY_BILLING in v["reason"]


def test_a_limited_one_ending_within_a_month_is_priced_for_its_own_hours():
    end = (NOW + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    v = g.gate_command(f"{BASE} --end-date-type limited --end-date {end}", record=False)
    assert v["decision"] == "ask"
    assert v["monthly_delta_usd"] == round(8 * RATE * 48, 2)
    assert v["estimate"]["hours"] == 48
    assert f"{_usd(8 * RATE * 48)} for the 48 hours until its end date" in v["reason"]
    # A month or more away is a month's bill; an end that has passed has none.
    far = (NOW + timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    v = g.gate_command(f"{BASE} --end-date-type limited --end-date {far}", record=False)
    assert v["monthly_delta_usd"] == round(MONTHLY, 2)
    gone = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    v = g.gate_command(f"{BASE} --end-date-type limited --end-date {gone}", record=False)
    assert v["decision"] == "ask" and "$" not in v["reason"]
    assert "its --end-date has passed" in v["reason"]
    v = g.gate_command(f"{BASE} --end-date-type limited --end-date soon", record=False)
    assert "could not read its --end-date" in v["reason"]


@pytest.mark.parametrize("change,why", [
    (("Linux/UNIX", "Windows"), "its --instance-platform is not Linux/UNIX"),
    (("--instance-platform Linux/UNIX", "--instance-platform 'Red Hat Enterprise Linux'"),
     "the guard prices Linux/UNIX on shared tenancy only"),
    (("--instance-count 8", "--instance-count 8 --tenancy dedicated"),
     "it reserves dedicated tenancy"),
    (("us-east-1a", "us-east-1-bos-1a"), "not a standard Availability Zone"),
    (("p5.48xlarge", "zz9.huge"), "no on-demand rate for zz9.huge"),
])
def test_anything_the_table_does_not_price_asks_with_no_figure(change, why):
    v = g.gate_command(BASE.replace(*change), record=False)
    assert v["decision"] == "ask" and v["action_type"] == "infra_apply"
    assert "$" not in v["reason"] and "monthly_delta_usd" not in v
    assert "No figure" in v["reason"] and why in v["reason"]
    assert g.CAPACITY_BILLING in v["reason"]


def test_the_zone_names_the_region_it_is_priced_in():
    v = g.gate_command(BASE.replace("us-east-1a", "sa-east-1a"), record=False)
    est = v["estimate"]
    assert est["region"] == "sa-east-1" and est["hourly_usd"] > RATE
    assert "sa-east-1 floor" in est["basis"]


def test_a_fleet_asks_with_no_figure(capsys):
    for v in (g.gate_command(FLEET, record=False), _guard_check(capsys, FLEET),
              _use_aws("CreateCapacityReservationFleet", {
                  "TotalTargetCapacity": 8, "InstanceTypeSpecifications": [
                      {"InstanceType": "p5.48xlarge", "InstancePlatform": "Linux/UNIX"}]})):
        assert v["decision"] == "ask" and "$" not in v["reason"]
        assert "Capacity Reservation Fleet" in v["reason"] and "No figure" in v["reason"]
    d = _hook(FLEET)
    assert d["permissionDecision"] == "ask"


def test_modifying_one_is_not_covered():
    """Named as not covered in the CHANGELOG: a modify can raise the count,
    and the guard does not read the reservation it changes."""
    cmd = "aws ec2 modify-capacity-reservation --capacity-reservation-id cr-1 --instance-count 64"
    assert g.gate_command(cmd, record=False) is None


# ── Python, and the AWS CLI run as a module ───────────────────────────────────

_TERM = f"CommitmentDuration={ONE_YEAR}"
_ARGS = "InstanceType='p5.48xlarge', InstancePlatform='Linux/UNIX', InstanceCount=8"
PY_COMMIT = [
    f"python3 -c \"import boto3; boto3.client('ec2').create_capacity_reservation({_ARGS}, {_TERM})\"",
    f"python3 -Ic \"import boto3; boto3.client('ec2').create_capacity_reservation({_ARGS}, {_TERM})\"",
    ("python3 - <<'EOF'\nimport boto3\nec2 = boto3.client('ec2')\n"
     f"ec2.create_capacity_reservation({_ARGS}, {_TERM})\nEOF"),
    (f"echo \"import boto3; boto3.client('ec2').create_capacity_reservation({_ARGS}, {_TERM})\""
     " | python3"),
    ("python3 -c \"import botocore.session; c = botocore.session.get_session().create_client("
     f"'ec2'); c._make_api_call('CreateCapacityReservation', {{'InstanceCount': 8, "
     f"'CommitmentDuration': {ONE_YEAR}}})\""),
    ("python3 -c \"import boto3; kw = dict(CommitmentDuration=31536000); "
     "boto3.client('ec2').create_capacity_reservation(**kw)\""),
]
PY_PLAIN = [cmd.replace(f", {_TERM}", "").replace(f", 'CommitmentDuration': {ONE_YEAR}", "")
            for cmd in PY_COMMIT[:5]] + [
    ("python3 -c \"import boto3; boto3.client('ec2').create_capacity_reservation_fleet("
     "TotalTargetCapacity=8)\"")]


@pytest.mark.parametrize("cmd", PY_COMMIT)
def test_python_that_reserves_with_a_term_is_under_the_floor(cmd, loose_policy):
    assert g.classify_command(cmd) == ("one_way", "purchase_commitment")
    v = g.gate_command(cmd, record=False)
    assert v["decision"] == "ask" and v["action_type"] == "purchase_commitment"
    assert "No figure: it is Python code" in v["reason"]


@pytest.mark.parametrize("cmd", PY_PLAIN)
def test_python_that_reserves_without_one_asks_with_no_figure(cmd):
    assert g.classify_command(cmd) == ("two_way", "infra_apply")
    v = g.gate_command(cmd, record=False)
    assert v["decision"] == "ask" and v["action_type"] == "infra_apply"
    assert "$" not in v["reason"] and "does not price Python code" in v["reason"]


def test_reading_or_naming_one_does_not_ask():
    for cmd in (("python3 -c \"import boto3; print(boto3.client('ec2')"
                 ".describe_capacity_reservations())\""),
                "python3 -c \"print('create_capacity_reservation')\"",
                "aws ec2 describe-capacity-reservations",
                "grep -rn create-capacity-reservation docs/",
                "git commit -m 'guard: price aws ec2 create-capacity-reservation'"):
        assert g.gate_command(cmd, record=False) is None, cmd


def test_python_m_awscli_is_read_as_aws():
    cmd = COMMIT.replace("aws ec2", "python3 -m awscli ec2", 1)
    v = g.gate_command(cmd, record=False)
    assert v["decision"] == "ask" and v["action_type"] == "purchase_commitment"
    assert _usd(TERM_TOTAL) in v["reason"]


# ── a dry run, only as an argument of its own ─────────────────────────────────

def test_a_real_dry_run_buys_nothing():
    for cmd in (f"{COMMIT} --dry-run", f"{BASE} --dry-run", f"{FLEET} --dry-run"):
        assert g.gate_command(cmd, record=False) is None, cmd
    assert _use_aws("CreateCapacityReservation",
                    {**PARAMS, "CommitmentDuration": ONE_YEAR, "DryRun": True}) is None


@pytest.mark.parametrize("tail", [
    "  # --dry-run", " --client-token 'x --dry-run'",
    " --tag-specifications 'ResourceType=capacity-reservation,Tags=[{Key=n,Value= --dry-run}]'"])
def test_a_dry_run_in_a_comment_or_a_value_does_not_excuse_it(tail):
    v = g.gate_command(COMMIT + tail, record=False)
    assert v["decision"] == "ask" and v["action_type"] == "purchase_commitment"
    v = g.gate_command(BASE + tail, record=False)
    assert v["decision"] == "ask" and v["action_type"] == "infra_apply"
    # Priced, not skipped as if it were a dry run.
    assert v["monthly_delta_usd"] == round(MONTHLY, 2), v["reason"]
    v = _use_aws("CreateCapacityReservation", {**PARAMS, "CommitmentDuration": ONE_YEAR,
                                               "ClientToken": "x --dry-run"})
    assert v["decision"] == "ask"


def test_a_launch_with_a_dry_run_in_a_comment_is_priced_too():
    """The same hole in the pricer: `# --dry-run` made the launch read as a
    dry run, so 8x p5.48xlarge passed with no figure and no ask."""
    v = g.gate_command("aws ec2 run-instances --instance-type p5.48xlarge --count 8 "
                       "# --dry-run", record=False)
    assert v["decision"] == "ask" and v["monthly_delta_usd"] == round(MONTHLY, 2)
    v = g.gate_command("aws ec2 run-instances --instance-type p5.48xlarge --count 8 "
                       "--client-token 'x --dry-run'", record=False)
    assert v["decision"] == "ask"
    # A real one beside a real launch: the dry run is still left out.
    v = g.gate_command("aws ec2 run-instances --instance-type p5.48xlarge --count 8 --dry-run"
                       " && aws ec2 run-instances --instance-type m5.large --count 40",
                       record=False)
    assert v["estimate"]["instance_type"] == "m5.large"


# ── the read-only key ─────────────────────────────────────────────────────────

def test_creating_one_is_a_dangerous_action_with_its_own_probe():
    from finops.security import iam_setup as iam
    assert "ec2:CreateCapacityReservation" in iam._DANGEROUS_ACTIONS_PREFIXES
    assert "ec2:CreateCapacityReservation" in iam._COMMITMENT_PURCHASE_PROBES
    assert "CreateCapacityReservation" in _ec2().operation_names
    assert not any(a.startswith("ec2:CreateCapacityReservation")
                   for a in iam._REQUIRED_ACTIONS)


def _client(service: str):
    import boto3
    return boto3.client(service, region_name="us-east-1", aws_access_key_id="testing",
                        aws_secret_access_key="testing")  # pragma: allowlist secret


def test_check_scope_reports_a_key_that_can_create_one(monkeypatch, capsys):
    import boto3
    from botocore.stub import ANY, Stubber

    from finops import setup_wizard
    from finops.security import iam_setup as iam
    sts, iam_client = _client("sts"), _client("iam")
    asked: dict = {}
    iam_client.meta.events.register("provide-client-params.iam.SimulatePrincipalPolicy",
                                    lambda params, **_: asked.update(params))
    arn = "arn:aws:iam::123456789012:user/ci"
    allowed = {*iam._REQUIRED_ACTIONS, "ec2:CreateCapacityReservation"}
    names = list(dict.fromkeys([*iam._REQUIRED_ACTIONS, "ec2:TerminateInstances",
                                "s3:PutObject", "iam:CreateUser", "lambda:InvokeFunction",
                                *iam._COMMITMENT_PURCHASE_PROBES]))
    monkeypatch.setattr(boto3, "client", lambda name, *a, **k: {"sts": sts,
                                                                 "iam": iam_client}[name])
    with Stubber(sts) as s_sts, Stubber(iam_client) as s_iam:
        s_sts.add_response("get_caller_identity",
                           {"Account": "123456789012", "Arn": arn, "UserId": "AIDA0"})
        s_iam.add_response("simulate_principal_policy", {"EvaluationResults": [
            {"EvalActionName": a, "EvalDecision": "allowed" if a in allowed else "implicitDeny"}
            for a in names]}, {"PolicySourceArn": arn, "ActionNames": ANY, "ResourceArns": ["*"]})
        with pytest.raises(SystemExit) as e:
            setup_wizard.main(["setup", "aws", "--check-scope"])
    assert e.value.code == 1
    assert "ec2:CreateCapacityReservation" in asked["ActionNames"]
    out = capsys.readouterr().out
    assert re.search(r"Over-provisioned.*ec2:CreateCapacityReservation", out, re.DOTALL), out
