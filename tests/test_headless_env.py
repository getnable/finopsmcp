"""Configured by environment only: what headless use sets instead of a wizard.

A pipeline has no prompt to answer, so every decision is an environment
variable, and a variable that half works is worse than one that is missing.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
ROOT = Path(SRC) / "finops"
_OURS = re.compile(r"^(FINOPS|NABLE)_[A-Z0-9_]*[A-Z0-9]$")
# String constants with nable's prefixes that are not environment variables.
_NOT_ENV = {"NABLE_READER"}  # setup/cloud_infra.py: the Snowflake user the setup SQL creates


def _docstrings(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def _our_names_in_source() -> dict[str, set[str]]:
    """Every FINOPS_*/NABLE_* string constant in src/finops (not comments or
    docstrings), the way the code names a variable it reads: os.getenv("X"),
    get_env("X"), a tuple it loops over, or a module constant such as
    DELIVER_ENV = "NABLE_BRIEF_DELIVER"."""
    found: dict[str, set[str]] = {}
    for f in ROOT.rglob("*.py"):
        if f.name == "env_vars.py":
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in skip and _OURS.match(node.value)
                    and node.value not in _NOT_ENV):
                found.setdefault(node.value, set()).add(str(f.relative_to(ROOT)))
    return found


def test_the_known_list_is_exactly_what_the_source_reads():
    """env_vars.KNOWN is what `nable doctor` checks the environment against,
    so it must not miss a name (the doctor would call a real setting a typo)
    or keep a dead one (it would call a typo fine)."""
    from finops import env_vars
    in_source = set(_our_names_in_source())
    assert in_source - env_vars.KNOWN == set(), "read but missing from env_vars.KNOWN"
    assert env_vars.KNOWN - in_source == set(), "in env_vars.KNOWN but read nowhere"


def test_names_said_not_to_be_read_are_not_read():
    from finops import env_vars
    assert set(env_vars.NOT_READ_HERE) & set(_our_names_in_source()) == set()

_READERS = """
import json
from finops import config, license as lic, update_check
from finops.security import vault
print(json.dumps({
    "config.is_airgap": config.is_airgap(),
    "update_check._disabled": update_check._disabled(),
    "vault._keyring_disabled": vault._keyring_disabled(),
    "license._keyring_disabled": lic._keyring_disabled(),
}))
"""


@pytest.mark.parametrize("value,on", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("OFF", False),
])
def test_every_reader_of_finops_airgap_reads_it_the_same_way(tmp_path, value, on):
    """FINOPS_AIRGAP had three rules. "true" turned air-gap on but left OS
    keychain access on (vault and license compared to "1"); "off" turned it
    ON for config and telemetry (which only knew 0/false/no) and off for the
    update check. One rule now: set to anything but 0/false/no/off."""
    import json
    env = {k: v for k, v in os.environ.items()
           if k not in ("FINOPS_NO_KEYRING", "NABLE_NO_UPDATE_CHECK", "FINOPS_NO_UPDATE_CHECK",
                        "NABLE_NO_TELEMETRY", "DO_NOT_TRACK")}
    env.update(FINOPS_AIRGAP=value, HOME=str(tmp_path), PYTHONPATH=SRC)
    r = subprocess.run([sys.executable, "-c", _READERS], env=env, capture_output=True,
                       text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    seen = json.loads(r.stdout)
    if sys.platform == "darwin" and not on:
        # No login keychain on a CI mac also disables the keychain; not this test's subject.
        seen.pop("vault._keyring_disabled")
        seen.pop("license._keyring_disabled")
    assert seen == {k: on for k in seen}, seen


def test_every_known_name_is_described():
    from finops import env_vars
    assert set(env_vars.DOC) == set(env_vars.KNOWN)
    sections = {key for key, _title in env_vars.SECTIONS}
    for name, (section, text) in env_vars.DOC.items():
        assert section in sections and text.strip(), name


def test_the_headless_doc_lists_exactly_the_known_names():
    """docs/HEADLESS.md's environment reference is generated from env_vars by
    scripts/headless_env_table.py: every name it lists is one the package
    reads, every name it reads is listed, and the table is the generator's
    current output."""
    import importlib.util

    from finops import env_vars
    root = Path(__file__).resolve().parents[1]
    doc = (root / "docs" / "HEADLESS.md").read_text(encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        "headless_env_table", root / "scripts" / "headless_env_table.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    block = doc.split(gen.START, 1)[1].split(gen.END, 1)[0]
    listed = re.findall(r"^\| `((?:FINOPS|NABLE)_[A-Z0-9_]+)` \|", block, re.MULTILINE)
    assert len(listed) == len(set(listed)), "a name is listed twice"
    assert set(listed) == env_vars.KNOWN
    assert gen.splice(doc, gen.render()) == doc, (
        "docs/HEADLESS.md is stale: python scripts/headless_env_table.py --write")
