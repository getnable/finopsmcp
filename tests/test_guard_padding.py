"""A padded command must not switch the guard off.

The hook has a timeout (10 s in Claude Code) and a timed-out hook fails open.
`terraform destroy -auto-approve # AAAA...` with a 100 KB comment used to take
the hook 18 s: redact() and several classifier patterns were quadratic (one
cubic) in the command, so padding was a way to run the destroy unguarded.

Invariants under test:
  - classification is linear: a 1 MB command of any shape classifies fast
  - redact() reads a bounded prefix, never the whole input
  - a command too long to judge in time is asked about, not waved through
  - the hook writes its verdict before it writes the ledger
"""
from __future__ import annotations

import io
import json
import time

import pytest

import finops.guard as g
import finops.guard_adapters as ga
import finops.guard_ledger as gl
from finops import ai_budget

MB = 1 << 20

# Fillers that each defeated a pattern: a program name repeated gives a regex
# one start per repetition, and each start rescanned the rest of the command.
FILLS = ["A", "terraform ", "tofu ", "terraform apply ", "apply ", "gcloud ", "az ",
         "kubectl ", "pulumi ", "gsutil -x ", "-gsutil ", "TF_CLI_ARGS=", "aws ",
         "aws --profile p ", "--limit-price ", "http://", "password", "----", "Aa1"]
HEADS = ["aws ec2 terminate-instances --instance-ids i-1 ; echo ",
         "aws ec2 run-instances --instance-type t3.micro ; echo "]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})


def _timed(fn, *a, **k):
    t = time.perf_counter()
    out = fn(*a, **k)
    return out, time.perf_counter() - t


@pytest.mark.parametrize("fill", FILLS)
def test_classification_is_linear_in_a_padded_command(fill):
    for head in HEADS:
        cmd = head + fill * (MB // len(fill))
        hit, took = _timed(g.classify_command, cmd)
        assert took < 1.0, f"{fill!r}: {took:.2f}s for 1 MB"
        assert hit is not None, "the real command in front of the padding is still seen"


def test_classification_time_grows_linearly():
    cmd = "aws ec2 run-instances ; echo " + "terraform apply " * (MB // 16)
    _, small = _timed(g.classify_command, cmd[: MB // 4])
    _, big = _timed(g.classify_command, cmd)
    assert big < 8 * max(small, 0.01), "4x the input may not cost 16x (quadratic)"


@pytest.mark.parametrize("cmd,want", [
    ("terraform apply -auto-approve -destroy", ("one_way", "delete_resource")),
    ("cd x && tofu -chdir=y apply -destroy -auto-approve", ("one_way", "delete_resource")),
    ("terraform plan -destroy -out p && terraform apply p", ("two_way", "infra_apply")),
    ("TF_CLI_ARGS_apply=-destroy terraform apply", ("one_way", "delete_resource")),
    ("TF_CLI_ARGS=-lock=false,-destroy terraform apply", ("one_way", "delete_resource")),
    ("TF_CLI_ARGS_plan=-lock=false terraform apply", ("two_way", "infra_apply")),
    ("gsutil -m rm -r gs://b", ("one_way", "delete_resource")),
    ("/usr/bin/gsutil rb gs://b", ("one_way", "delete_resource")),
    ("gsutil ls gs://b", None),
    ("terraform -chdir=a destroy", ("one_way", "delete_resource")),
    ("gcloud --project p compute instances create vm-1", ("two_way", "infra_apply")),
])
def test_the_linear_rules_judge_what_the_old_patterns_did(cmd, want):
    assert g.classify_command(cmd) == want


def test_redact_reads_a_bounded_prefix():
    out, took = _timed(gl.redact, "terraform destroy # " + "http://a" * (10 * MB // 8))
    assert took < 0.2 and len(out) <= 400
    secret_after_cut = "x " * 3000 + "PASSWORD=hunter2"
    assert "hunter2" not in gl.redact(secret_after_cut, limit=10_000)


def test_a_token_cut_at_the_boundary_is_dropped_not_half_kept():
    cmd = " " * (gl._REDACT_INPUT_MAX - 20) + "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    out = gl.redact(cmd)
    assert "wJalrXUtnFEMI" not in out and out.endswith("...")


def test_a_padded_destroy_is_asked_about_in_time():
    cmd = "terraform destroy -auto-approve # " + "A" * (100 * 1024)
    v, took = _timed(g.gate_command, cmd)
    assert took < 1.0
    assert v["decision"] == "ask" and v["action_type"] == "delete_resource"


def test_a_command_too_long_to_judge_is_asked_about():
    cmd = "echo ok # " + "A" * (2 * MB)
    v, took = _timed(g.gate_command, cmd)
    assert took < 1.0
    assert v["decision"] == "ask" and v["action_type"] == "oversize_command"
    assert "2,048 KB" in v["reason"] and "256 KB" in v["reason"]
    [r] = [json.loads(line) for line in gl.ledger_path().read_text().splitlines()]
    assert r["action_type"] == "oversize_command" and len(r["command"]) <= 400


def test_an_oversize_mcp_call_is_asked_about():
    call = {"cli_command": "aws ec2 terminate-instances --instance-ids " + "i-1 " * MB}
    v = g.gate_mcp_call("mcp__aws-api__call_aws", call)
    assert v["decision"] == "ask" and v["action_type"] == "oversize_command"


def test_the_hook_through_the_cli_is_fast_on_a_padded_payload():
    payload = json.dumps({"tool_name": "Bash", "session_id": "s", "tool_input": {
        "command": "terraform destroy -auto-approve # " + "A" * (10 * MB)}})
    out = io.StringIO()
    _, took = _timed(ga.run_hook, None, io.StringIO(payload), out, io.StringIO())
    assert took < 2.0
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "ask"


# ── the answer before the receipt ─────────────────────────────────────────────

class _Stdout(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushed_at_append: list[str] = []


def _spy_append(monkeypatch, out: _Stdout) -> None:
    real = gl.append

    def append(entry):
        out.flushed_at_append.append(out.getvalue())
        return real(entry)
    monkeypatch.setattr(gl, "append", append)


def test_the_claude_hook_writes_its_verdict_before_the_ledger(monkeypatch):
    out = _Stdout()
    _spy_append(monkeypatch, out)
    payload = {"tool_name": "Bash", "tool_input": {"command": "terraform destroy"}}
    g.run_hook(io.StringIO(json.dumps(payload)), out)
    assert out.flushed_at_append and '"permissionDecision": "ask"' in out.flushed_at_append[0]
    assert json.loads(gl.ledger_path().read_text())["decision"] == "ask"


@pytest.mark.parametrize("harness,payload,needle", [
    ("cursor", {"hook_event_name": "beforeShellExecution", "command": "terraform destroy"},
     '"permission": "ask"'),
    ("codex", {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": "terraform destroy"}}, '"permissionDecision": "deny"'),
])
def test_the_other_harnesses_answer_before_the_ledger_too(monkeypatch, harness, payload, needle):
    out = _Stdout()
    _spy_append(monkeypatch, out)
    ga.run_hook(harness, io.StringIO(json.dumps(payload)), out, io.StringIO())
    assert out.flushed_at_append and needle in out.flushed_at_append[0]


def test_outside_a_hook_the_ledger_is_written_at_once():
    g.gate_command("terraform destroy")
    assert gl.ledger_path().exists(), "gate_command's own callers see the record immediately"
