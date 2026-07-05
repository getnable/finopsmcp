"""Guard hardening: classifier coverage, shell/MCP vocabulary consistency, and
a hook command that exists for uvx users.

The consistency invariant matters most: the shell guard and the MCP gate are two
doors into one policy, and they once disagreed (the guard allowed `terraform
apply` while check_action_policy blocked "terraform_apply" as unknown). Every
action type either half can emit must be classified by policy.door_of.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from finops import guard
from finops.policy import DEFAULT_POLICY, door_of


# ── classifier coverage: the one-way doors the audit found missing ─────────────

@pytest.mark.parametrize("cmd", [
    "terraform apply -destroy",
    "tofu apply -destroy -auto-approve",
    "pulumi destroy --yes",
    "eksctl delete cluster --name prod",
    "aws s3 rb s3://prod-bucket --force",
    "aws s3 rm s3://prod-bucket --recursive",
    "gsutil rm -r gs://prod-bucket",
    "gsutil -m rm -r gs://prod-bucket",
])
def test_one_way_doors_are_caught(cmd):
    hit = guard.classify_command(cmd)
    assert hit is not None and hit[0] == "one_way", cmd


@pytest.mark.parametrize("cmd", [
    "terraform apply",                          # plain apply stays reversible
    "terraform apply && echo done -destroy",    # -destroy in a CHAINED command must not leak back
])
def test_plain_apply_stays_two_way(cmd):
    hit = guard.classify_command(cmd)
    assert hit is not None and hit[0] == "two_way", cmd


@pytest.mark.parametrize("cmd", [
    "aws s3 ls", "aws s3 cp file s3://b/", "git rm -r old/", "terraform plan",
])
def test_reads_and_local_ops_are_ignored(cmd):
    assert guard.classify_command(cmd) is None, cmd


# ── shell/MCP consistency invariant ─────────────────────────────────────────────

def test_every_classifier_action_type_is_known_to_policy():
    """No classifier may emit an action type the policy cannot door-classify:
    'unknown' falls to the block branch and the two halves contradict."""
    emitted = {a for _, a in guard._ONE_WAY_CLASSIFIERS} | {a for _, a in guard._TWO_WAY_CLASSIFIERS}
    unknown = sorted(a for a in emitted if door_of(a) == "unknown")
    assert not unknown, f"policy.py doors missing for: {unknown}"


def test_reversible_apply_is_allowed_by_default():
    # What the shell guard waves through silently, the MCP gate must also allow.
    from finops.policy import evaluate_action_gate
    for action in ("infra_apply", "terraform_apply", "helm_upgrade"):
        assert action in DEFAULT_POLICY["allowed_action_types"]
        assert evaluate_action_gate(action)["gate"] == "allow"
        # ...but a big cost delta still escalates.
        assert evaluate_action_gate(action, monthly_delta_usd=9999)["gate"] == "escalate"


# ── durable hook command ────────────────────────────────────────────────────────

def test_hook_command_prefers_persistent_binary():
    with patch("shutil.which", return_value="/usr/local/bin/finops"):
        assert guard._hook_command() == "/usr/local/bin/finops guard hook"


def test_hook_command_quotes_paths_with_spaces():
    with patch("shutil.which", return_value="/Users/x/My Tools/finops"):
        assert guard._hook_command() == '"/Users/x/My Tools/finops" guard hook'


def test_hook_command_falls_back_to_uvx():
    # A uvx user has no finops on PATH; the bare command would fail on every
    # Bash call. uv is guaranteed present for anyone who installed via uvx.
    with patch("shutil.which", return_value=None):
        assert guard._hook_command() == "uvx --from finops-mcp finops guard hook"


def test_install_marker_matches_all_command_forms(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for resolved, timeout in [
        ("/usr/local/bin/finops guard hook", 10),
        ('"/Users/x/My Tools/finops" guard hook', 10),
        ("uvx --from finops-mcp finops guard hook", 30),
    ]:
        with patch.object(guard, "_hook_command", return_value=resolved):
            path = guard.install(global_scope=False)
            written = json.loads(path.read_text())
            hook = written["hooks"]["PreToolUse"][0]["hooks"][0]
            assert hook["command"] == resolved
            assert hook["timeout"] == timeout
            assert guard.is_installed(path)      # marker finds every form
            assert guard.uninstall(global_scope=False)
            assert not guard.is_installed(path)  # and removes every form
