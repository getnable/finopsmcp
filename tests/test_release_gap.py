"""The release gap: main carrying a version PyPI does not have.

Claude Code installs the nable plugin from this repository's default branch, so
its `finops-mcp==X` pin reaches users at merge time. Measured on 2026-10-13:
main pinned 0.10.0 (and this branch 0.11.0) while PyPI's newest release was
0.8.216, because no tag was pushed after it. The plugin's exact hook command,
`uvx --python 3.12 --from finops-mcp==0.10.0 finops guard hook --via plugin;
exit 0`, printed nothing and exited 0 on a `terraform destroy`: uv could not
resolve the pin, and the `; exit 0` that keeps a broken hook from blocking the
agent turned that into a silent allow.

tests/test_upgrade_pin.py keeps every carrier in step with pyproject. It cannot
see PyPI and must not try: a merge is ahead of its tag for a few minutes on
every release, and a suite that went red then would be red on main by design.
scripts/check_release.py is the check that can see PyPI, and it runs on a
schedule (.github/workflows/release-gap.yml), never on a push or a pull
request. These tests pin its decisions and its triggers, offline.
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import shutil

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("check_release", ROOT / "scripts/check_release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cr = _load()


def test_it_reads_every_carrier_the_upgrade_pin_test_reads():
    found = cr.carriers(ROOT)
    for rel in ("src/finops/__init__.py", "server.json", "docs/sbom.json",
                "packaging/mcpb/manifest.json", ".claude-plugin/marketplace.json",
                "plugins/nable/.claude-plugin/plugin.json"):
        assert rel in found, rel
    hook_keys = [k for k in found if k.startswith("plugins/nable/hooks/hooks.json")]
    assert len(hook_keys) == 2, "the PreToolUse and PostToolUse hooks each pin a release"
    # and on this tree they agree, which test_upgrade_pin also requires
    assert set(found.values()) == {cr.package_version(ROOT)}


def test_released_is_a_pass():
    assert cr.verdict("1.2.3", {}, True, None, 2.0)[0] == 0


def test_a_fresh_bump_waiting_for_its_tag_is_not_a_failure():
    code, line = cr.verdict("1.2.3", {}, False, 0.2, 2.0)
    assert code == 0 and "Tag v1.2.3" in line


def test_an_old_unreleased_bump_fails_and_says_what_it_breaks():
    code, line = cr.verdict("1.2.3", {}, False, 15.0, 2.0)
    assert code == 1
    assert "not on PyPI" in line and "plugin" in line and "v1.2.3" in line


def test_an_unknown_age_is_not_read_as_fresh():
    assert cr.verdict("1.2.3", {}, False, None, 2.0)[0] == 1


def test_pypi_unreachable_is_not_a_pass():
    code, line = cr.verdict("1.2.3", {}, None, None, 2.0)
    assert code == 2 and "could not" in line


def test_disagreeing_carriers_fail_before_pypi_is_asked(tmp_path):
    tree = tmp_path / "repo"
    for rel in ("pyproject.toml", "src/finops/__init__.py", "server.json", "docs/sbom.json",
                "packaging/mcpb/manifest.json", ".claude-plugin/marketplace.json",
                "plugins/nable/.claude-plugin/plugin.json", "plugins/nable/hooks/hooks.json"):
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tree / rel)
    hooks = tree / "plugins/nable/hooks/hooks.json"
    data = json.loads(hooks.read_text())
    cmd = data["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    data["hooks"]["PostToolUse"][0]["hooks"][0]["command"] = cmd.replace(
        f"finops-mcp=={cr.package_version(ROOT)}", "finops-mcp==0.0.1")
    hooks.write_text(json.dumps(data))

    asked = []
    out = io.StringIO()
    code = cr.run(tree, pypi=lambda v: asked.append(v) or True, out=out)
    assert code == 1 and asked == []
    assert "hooks.json (hook 2)=0.0.1" in out.getvalue()


@pytest.mark.parametrize("released,code", [(True, 0), (None, 2)])
def test_run_reports_without_the_network(released, code):
    out = io.StringIO()
    assert cr.run(ROOT, pypi=lambda v: released, out=out) == code
    assert f"version in this checkout: {cr.package_version(ROOT)}" in out.getvalue()


def test_the_workflow_never_runs_on_a_push_or_a_pull_request():
    """A merge is always ahead of its tag. A check wired to push or
    pull_request would turn main red between merge and tag, every release."""
    wf = yaml.safe_load((ROOT / ".github/workflows/release-gap.yml").read_text(encoding="utf-8"))
    triggers = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` as True
    assert set(triggers) <= {"schedule", "workflow_dispatch"}, set(triggers)
    assert "schedule" in triggers
    assert wf["permissions"] == {"contents": "read"}
    steps = [s for job in wf["jobs"].values() for s in job["steps"]]
    assert any("scripts/check_release.py" in s.get("run", "") for s in steps)
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0, "the bump's age and the tag need full history"
