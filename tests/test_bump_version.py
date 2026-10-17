"""scripts/bump_version.py moves the version carriers and nothing else.

The 0.11.0 bump was a find-and-replace that also raised the `pdfplumber>=0.10.0`
dependency floor to `>=0.11.0`. These tests run the script's bump on a copy of
the carriers where a dependency floor equals the old version, and require the
floor to survive.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import shutil

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_CARRIER_FILES = [
    "pyproject.toml",
    "src/finops/__init__.py",
    "server.json",
    "plugins/nable/.claude-plugin/plugin.json",
    "plugins/nable/hooks/hooks.json",
    ".claude-plugin/marketplace.json",
    "packaging/mcpb/manifest.json",
    "docs/sbom.json",
    "examples/github-actions/README.md",
    "examples/github-actions/budget-gate.yml",
]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tree(tmp_path):
    for rel in _CARRIER_FILES:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dst)
    return tmp_path


def _current() -> str:
    found = set(_load("check_release").carriers(ROOT).values())
    assert len(found) == 1, found
    return found.pop()


def test_every_carrier_moves_but_the_sbom_waits_for_its_generator(tree):
    bump_version, check_release = _load("bump_version"), _load("check_release")
    bump_version.bump(_current(), "9.9.9", root=tree)
    after = check_release.carriers(tree)
    assert {k: v for k, v in after.items() if v != "9.9.9"} == {
        "docs/sbom.json": _current()}


def test_a_dependency_floor_equal_to_the_old_version_is_left_alone(tree):
    old = _current()
    pyproject = tree / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    text, n = re.subn(r'"pdfplumber>=[^"]+"', f'"pdfplumber>={old}"', text)
    assert n >= 1
    pyproject.write_text(text, encoding="utf-8")

    _load("bump_version").bump(old, "9.9.9", root=tree)

    bumped = pyproject.read_text(encoding="utf-8")
    assert bumped.count(f'"pdfplumber>={old}"') == n
    assert re.search(r'^version = "9\.9\.9"$', bumped, re.MULTILINE)


def test_a_carrier_that_does_not_say_the_old_version_stops_the_bump(tree):
    hooks = tree / "plugins/nable/hooks/hooks.json"
    hooks.write_text(hooks.read_text(encoding="utf-8").replace(
        f"finops-mcp=={_current()}", "finops-mcp==0.0.1", 1), encoding="utf-8")
    with pytest.raises(SystemExit, match="hooks.json"):
        _load("bump_version").bump(_current(), "9.9.9", root=tree)


def test_the_repo_dependency_floors_are_not_the_release_version():
    """pdfplumber's floor is 0.10.0 by intent (95e5c61); 0.11.0 was the accident."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"pdfplumber>=0.10.0"' in pyproject
    assert f'"pdfplumber>={_current()}"' not in pyproject


def test_a_refused_bump_writes_nothing(tree):
    before = {rel: (tree / rel).read_text(encoding="utf-8") for rel in _CARRIER_FILES}
    hooks = tree / "plugins/nable/hooks/hooks.json"
    hooks.write_text(before["plugins/nable/hooks/hooks.json"].replace(
        f"finops-mcp=={_current()}", "finops-mcp==0.0.1", 1), encoding="utf-8")
    with pytest.raises(SystemExit):
        _load("bump_version").bump(_current(), "9.9.9", root=tree)
    for rel in _CARRIER_FILES:
        if rel != "plugins/nable/hooks/hooks.json":
            assert (tree / rel).read_text(encoding="utf-8") == before[rel], rel


def test_the_ci_recipes_move_with_the_release(tree):
    _load("bump_version").bump(_current(), "9.9.9", root=tree)
    gate = (tree / "examples/github-actions/budget-gate.yml").read_text(encoding="utf-8")
    readme = (tree / "examples/github-actions/README.md").read_text(encoding="utf-8")
    assert '"finops-mcp==9.9.9"' in gate and _current() not in gate
    assert "finops-mcp==9.9.9" in readme and "(finops-mcp 9.9.9)" in readme
