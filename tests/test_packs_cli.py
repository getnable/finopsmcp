# SPDX-License-Identifier: Apache-2.0
"""`nable pack ...` through the real CLI entry, and code-plugin discovery
(which lists entry points and never loads them)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from finops.setup_wizard import main
from tests import packs_support
from tests.packs_support import (
    EXAMPLE_PACK,
    make_pack,
)

# The shared fixture: an isolated packs root, policy file and registry setting.
packs_env = packs_support.packs_env


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as ei:
        main(["pack", *argv])
    out = capsys.readouterr()
    return ei.value.code, out.out, out.err


def test_validate_new_install_list_audit_remove(packs_env, capsys, tmp_path):
    code, out, _ = _run(capsys, "validate", str(EXAMPLE_PACK))
    assert code == 0 and out.startswith("OK io.github.getnable/startup-credits-runway 1.0.0")

    target = tmp_path / "mine"
    code, out, _ = _run(capsys, "new", "mine", "--dir", str(target), "--namespace", "com.acme")
    assert code == 0 and (target / "nable-pack.toml").is_file()
    code, out, _ = _run(capsys, "validate", str(target), "--json")
    assert code == 0 and json.loads(out)["id"] == "com.acme/mine"

    # not a terminal and no --yes: refused, nothing installed
    code, _, err = _run(capsys, "install", str(target))
    assert code == 1 and "not approved" in err
    code, out, _ = _run(capsys, "install", str(target), "--yes", "--json")
    body = json.loads(out)
    assert code == 0 and body["status"] == "installed" and body["pack"]["approval"] == "--yes"

    code, out, _ = _run(capsys, "list", "--json")
    assert [p["id"] for p in json.loads(out)["packs"]] == ["com.acme/mine"]
    code, out, _ = _run(capsys, "audit")
    assert code == 0 and "[ok] com.acme/mine 0.1.0" in out

    (packs_env.root / "com.acme" / "mine" / "0.1.0" / "guard" / "example.yaml").write_text("x")
    code, out, _ = _run(capsys, "audit")
    assert code == 1 and "[tampered]" in out and "changed since it was approved" in out

    code, _, err = _run(capsys, "remove", "com.acme/mine")
    assert code == 1 and "--yes" in err
    code, out, _ = _run(capsys, "remove", "com.acme/mine", "--yes")
    assert code == 0 and "Removed com.acme/mine" in out


def test_validate_reports_every_problem_and_exits_1(packs_env, capsys, tmp_path):
    src = make_pack(tmp_path / "bad", capabilities='network = ["169.254.169.254"]\n')
    code, out, _ = _run(capsys, "validate", str(src))
    assert code == 1 and "metadata endpoint" in out


def test_install_prompt_shows_capabilities_and_skills(packs_env, capsys, tmp_path, monkeypatch):
    from finops.packs import cli
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    src = make_pack(tmp_path / "src", capabilities='read_data = ["focus.cost"]\n'
                                                    'network = ["api.example.com:443"]\n'
                                                    'pricing = ["override"]\n')
    code, out, _ = _run(capsys, "install", str(src))
    assert code == 0
    assert "focus.cost: cost rows in FOCUS shape" in out
    assert "api.example.com:443: may connect to this host" in out
    assert "shown in full because it steers your agents" in out
    assert "| Do the thing, then say what it cost." in out
    assert "Installed: io.github.example/demo 1.0.0" in out


def test_search_with_an_unreachable_registry(packs_env, capsys, tmp_path):
    code, _, err = _run(capsys, "search", "x", "--registry", str(tmp_path / "none.json"))
    assert code == 1 and "could not be read" in err


def test_list_code_discovers_entry_points_without_loading_them(packs_env, capsys, monkeypatch,
                                                               tmp_path):
    import finops.packs.discovery as disc
    from finops.packs import install as inst

    inst.install(str(make_pack(tmp_path / "src", provides=(
        'policies = ["policies/*.yaml"]\n'
        'connectors = [{id = "kubecost", entry = "nable_k8s.kubecost:main", '
        'output = "focus-1.3"}]\n'))), yes=True)

    class EP(SimpleNamespace):
        def load(self):
            raise AssertionError("a code plugin was loaded")
    eps = {"nable.connectors": [EP(name="kubecost", value="nable_k8s.kubecost:main", dist=None),
                                EP(name="stray", value="other.mod:run", dist=None)]}
    monkeypatch.setattr(disc, "entry_points", lambda group: eps.get(group, []))
    rows = disc.code_plugins()
    assert [(r["name"], r["declared_by"], r["loaded"]) for r in rows] == [
        ("kubecost", "io.github.example/demo", False), ("stray", None, False)]
    code, out, _ = _run(capsys, "list", "--code")
    assert code == 0 and "found, not loaded" in out and "no pack manifest" in out


def test_the_first_party_plugin_seam_is_unchanged():
    from finops import plugins
    assert plugins._PLUGIN_GROUP == "finops.plugins"
    assert callable(plugins.load_plugins) and callable(plugins.loaded_plugins)
