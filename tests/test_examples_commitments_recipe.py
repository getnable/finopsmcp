# SPDX-License-Identifier: Apache-2.0
"""examples/github-actions/commitments-status.yml, the weekly commitments check.

It is copied into other people's repos, so what has to stay true:
  - it parses as a workflow, every action is pinned to a commit
  - it assumes a role through GitHub OIDC and stores no key: no static AWS
    credentials and no secrets anywhere in it
  - it runs `nable commitments status --json --fail-within 30` through uvx,
    pinned to the release this checkout is, and keeps the JSON as an artifact
    whatever the exit code
  - its header's exit codes are the command's own
  - scripts/bump_version.py moves its pin and scripts/check_release.py reads
    it as a carrier
  - the exact command it runs, headless with no credentials, is exit 2 (it
    cannot check) with a JSON document
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from finops import cli_commitments as cli

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "examples" / "github-actions" / "commitments-status.yml"
COMMAND = "nable commitments status --json --fail-within 30"


def _workflow() -> dict:
    return yaml.safe_load(RECIPE.read_text(encoding="utf-8"))


def _steps() -> list[dict]:
    return [s for job in _workflow()["jobs"].values() for s in job["steps"]]


def _version() -> str:
    return re.search(r'^version = "([^"]+)"$', (ROOT / "pyproject.toml").read_text(
        encoding="utf-8"), re.MULTILINE).group(1)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_it_parses_and_pins_every_action():
    wf = _workflow()
    assert wf.get(True, wf.get("on")), "no trigger"       # YAML 1.1 reads `on` as True
    for step in _steps():
        assert ("uses" in step) != ("run" in step), step
        if "uses" in step:
            assert re.search(r"@[0-9a-f]{40}$", step["uses"]), step["uses"]


def test_it_assumes_a_role_through_oidc_and_stores_no_key():
    wf = _workflow()
    assert wf["permissions"]["id-token"] == "write"
    assert wf["permissions"]["contents"] == "read"
    [role] = [s for s in _steps() if "configure-aws-credentials" in s.get("uses", "")]
    assert role["with"]["role-to-assume"] == "${{ vars.NABLE_AWS_ROLE_ARN }}"
    assert set(role["with"]) == {"role-to-assume", "aws-region"}
    text = RECIPE.read_text(encoding="utf-8")
    for static in ("aws-access-key-id", "aws-secret-access-key", "AWS_ACCESS_KEY_ID",
                   "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "secrets."):
        assert static not in text, static


def test_it_runs_the_command_through_uvx_pinned_to_this_release():
    runs = "\n".join(s["run"] for s in _steps() if "run" in s)
    assert f'uvx --from "finops-mcp=={_version()}" {COMMAND} > "$STATUS_JSON"' in runs
    # uv itself is installed at a pinned version, the way the other recipes
    # install what they run: from PyPI, with pip.
    assert re.search(r'python -m pip install --quiet "uv==\d+\.\d+\.\d+"', runs)


def test_it_keeps_the_json_as_an_artifact_whatever_the_exit_code():
    steps = _steps()
    [upload] = [s for s in steps if "upload-artifact" in s.get("uses", "")]
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "${{ env.STATUS_JSON }}"
    assert upload["with"]["if-no-files-found"] == "error"
    [fail] = [s for s in steps if s.get("name", "").startswith("Fail when")]
    assert "exit_code != '0'" in fail["if"] and fail["if"].startswith("always()")
    [job] = _workflow()["jobs"].values()
    assert job["env"]["STATUS_JSON"].endswith("nable-commitments.json")


def test_its_header_says_what_the_exit_codes_mean():
    header = RECIPE.read_text(encoding="utf-8").split("\non:", 1)[0]
    flat = " ".join(line.lstrip("# ") for line in header.splitlines())
    assert "1  a commitment that was read ends within 30 days (this wins)" in flat
    assert cli.EXIT_CODES[cli.EXIT_ENDING] == "a commitment that was read ends within DAYS"
    assert "2  a commitment read failed, so it cannot check" in flat
    assert cli.EXIT_CODES[cli.EXIT_CANNOT_CHECK] == "a commitment read failed, so it cannot check"
    # The statement the role needs is the template's, action for action.
    from finops.security import iam_setup
    [group] = [g for g in iam_setup._OPTIONAL_GROUPS if g[0] == "OptionalCommitments"]
    for action in group[3]:
        assert action in flat, action


@pytest.fixture
def tree(tmp_path):
    rels = ["pyproject.toml", "src/finops/__init__.py", "server.json",
            "plugins/nable/.claude-plugin/plugin.json", "plugins/nable/hooks/hooks.json",
            ".claude-plugin/marketplace.json", "packaging/mcpb/manifest.json", "docs/sbom.json",
            "examples/github-actions/commitments-status.yml"]
    for rel in rels:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dst)
    return tmp_path


def test_the_bump_moves_it_and_check_release_reads_it(tree):
    check_release, bump_version = _load("check_release"), _load("bump_version")
    key = "examples/github-actions/commitments-status.yml"
    assert check_release.carriers(ROOT)[key] == _version()
    assert check_release.carriers(tree)[key] == _version()
    bump_version.bump(_version(), "9.9.9", root=tree)
    assert check_release.carriers(tree)[key] == "9.9.9"
    text = (tree / key).read_text(encoding="utf-8")
    assert '"finops-mcp==9.9.9"' in text and f"finops-mcp=={_version()}" not in text


def test_without_credentials_the_command_it_runs_cannot_check(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "FINOPS_", "NABLE_"))}
    env.update(HOME=str(home), PYTHONPATH=str(ROOT / "src"), FINOPS_NO_KEYRING="1",
               NABLE_NO_TELEMETRY="1", NABLE_NO_UPDATE_CHECK="1",
               AWS_CONFIG_FILE=str(home / "none"), AWS_SHARED_CREDENTIALS_FILE=str(home / "none"),
               AWS_EC2_METADATA_DISABLED="true", FINOPS_DATA_DIR=str(tmp_path / "data"))
    r = subprocess.run([sys.executable, "-m", "finops.entry", *COMMAND.split()[1:]],
                       cwd=str(tmp_path), env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == cli.EXIT_CANNOT_CHECK == 2, r.stderr[-800:]
    doc = json.loads(r.stdout)
    assert doc["error"]["class"] == "no-credentials" and doc["gate"]["cannot_check"] is True
