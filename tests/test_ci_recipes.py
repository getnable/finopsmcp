"""The CI recipes in examples/github-actions run against the CLI as it is.

Each is copied into other people's repos, so a recipe whose `nable` command no
longer parses, or whose exit-code contract moved, fails there, quietly, on
someone else's runner. This parses every workflow, holds every action to a
commit SHA, and runs the exact `nable` command each recipe runs, headless and
with no cloud credentials, for the exit code its header documents.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "github-actions"
SRC = str(ROOT / "src")
RECIPES = ("budget-gate.yml", "scheduled-scan.yml", "guard-policy-check.yml",
           "commitments-status.yml")


def _workflow(name: str) -> dict:
    return yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(p.name for p in EXAMPLES.glob("*.yml")))
def test_every_example_parses_and_pins_its_actions(name):
    wf = _workflow(name)
    assert wf.get(True, wf.get("on")), "no trigger"       # YAML 1.1 reads `on` as True
    for job in wf["jobs"].values():
        for step in job["steps"]:
            assert ("uses" in step) != ("run" in step), step
            if "uses" in step:
                assert re.search(r"@[0-9a-f]{40}$", step["uses"]), step["uses"]


def _runs(name: str) -> str:
    return "\n".join(s["run"] for j in _workflow(name)["jobs"].values()
                     for s in j["steps"] if "run" in s)


def _nable(tmp_path: Path, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    full = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "FINOPS_", "NABLE_"))}
    full.update(HOME=str(home), PYTHONPATH=SRC, FINOPS_NO_KEYRING="1", NABLE_NO_TELEMETRY="1",
                AWS_CONFIG_FILE=str(home / "none"), AWS_SHARED_CREDENTIALS_FILE=str(home / "none"),
                AWS_EC2_METADATA_DISABLED="true", FINOPS_DATA_DIR=str(tmp_path / "data"))
    full.update(env or {})
    return subprocess.run([sys.executable, "-m", "finops.entry", *argv], cwd=str(tmp_path),
                          env=full, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=120, check=False)


def test_the_budget_gate_fails_closed_without_budgets_or_history(tmp_path):
    cmd = "nable budget ci-gate --fail-on-breach --json --budget-file budget.yml"
    assert cmd in _runs("budget-gate.yml")
    argv = cmd.split()[1:]
    r = _nable(tmp_path, *argv)
    assert r.returncode == 2 and json.loads(r.stdout)["error"].startswith("File not found")
    (tmp_path / "budget.yml").write_text(
        "budgets:\n  - name: AWS monthly\n    scope_type: provider\n    scope_value: aws\n"
        "    period: monthly\n    limit_usd: 50000\n")
    r = _nable(tmp_path, *argv)
    doc = json.loads(r.stdout)
    assert r.returncode == 2 and doc["cannot_check"] == ["AWS monthly"]


def test_the_scheduled_scan_without_credentials_exits_6_with_a_document(tmp_path):
    assert 'nable scan --json > "$SCAN_JSON"' in _runs("scheduled-scan.yml")
    r = _nable(tmp_path, "scan", "--json")
    assert r.returncode == 6
    assert json.loads(r.stdout)["error"]["exit_code"] == 6


def test_the_guard_policy_check_reads_the_verdict_and_the_policy(tmp_path):
    assert '["nable", "guard", "check", "--json", "--command", cmd]' in _runs(
        "guard-policy-check.yml")
    policy = tmp_path / "nable.policy.yaml"
    policy.write_text("max_auto_monthly_usd: lots\n")
    r = _nable(tmp_path, "guard", "check", "--json", "--command", "terraform destroy",
               env={"FINOPS_POLICY_FILE": str(policy)})
    doc = json.loads(r.stdout)
    assert r.returncode == 1 and doc["decision"] == "ask"
    assert doc["policy"]["broken"] and doc["policy"]["problems"] and doc["policy"]["fix"]


def test_every_recipe_installs_the_release_its_commands_were_checked_against():
    """A recipe pinned to an older release runs commands that release lacks
    (`guard check --json`, the budget gate's exit 2): it would fail on someone
    else's runner for a reason the tests above never see."""
    version = re.search(r'^version = "([^"]+)"$',
                        (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    pins = {p.name: re.findall(r"finops-mcp==([0-9][0-9A-Za-z.]*)", p.read_text(encoding="utf-8"))
            for p in [*EXAMPLES.glob("*.yml"), EXAMPLES / "README.md"]}
    assert all(pins[name] for name in RECIPES), pins
    assert {name: v for name, vs in pins.items() for v in vs if v != version} == {}
