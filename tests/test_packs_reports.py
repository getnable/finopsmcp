# SPDX-License-Identifier: Apache-2.0
"""Pack report templates rendered over nable's own data (finops.packs.reports)
and the evidence the ledger scope builds (finops.change_evidence).

What has to stay true:
  - a template reads a data scope only when its pack declares it
  - `--set` fills plain placeholders and can never stand in for a scope
  - nothing in a template is evaluated
  - the evidence says what it is, and an empty ledger is evidence of nothing
"""
from __future__ import annotations

import json

import pytest

import finops.guard_ledger as gl
from finops import change_evidence
from finops.packs import install as inst
from finops.packs import reports
from finops.packs.errors import PackError, PolicyRefusal
from finops.setup_wizard import main
from tests import packs_support
from tests.packs_support import make_pack

packs_env = packs_support.packs_env

TEMPLATE = """\
# ${title}
Asked: ${ledger.guard.counts.asked}. Note: ${ledger.guard.note}
Untouched: ${missing.value} {{ ''.__class__ }}
"""


def _install(tmp_path, caps='read_data = ["ledger.guard"]\n', text=TEMPLATE, name="rep"):
    src = make_pack(tmp_path / name, name=name, provides='reports = ["reports/*.md"]\n',
                    capabilities=caps)
    (src / "reports").mkdir()
    (src / "reports" / "brief.md").write_text(text)
    inst.install(str(src), yes=True)
    return f"io.github.example/{name}"


def test_scopes_are_found_from_placeholders():
    assert reports.scopes_in(TEMPLATE) == ["ledger.guard"]
    assert reports.scopes_in("${ledger.guardian} ${year}") == []


def test_a_declared_scope_renders_and_nothing_is_evaluated(packs_env, tmp_path):
    pid = _install(tmp_path)
    r = reports.render(pid, "brief", sets={"title": "Weekly"})
    assert r["scopes"] == ["ledger.guard"] and r["report"] == "reports/brief.md"
    assert r["text"].startswith("# Weekly\nAsked: 0. Note: This is change-management evidence")
    assert "${missing.value} {{ ''.__class__ }}" in r["text"]
    assert r["values"]["ledger"]["guard"]["ledger"]["records"] == 0


def test_an_undeclared_scope_is_refused(packs_env, tmp_path):
    pid = _install(tmp_path, caps="")
    with pytest.raises(PolicyRefusal, match="does not declare"):
        reports.render(pid, "brief.md")


def test_set_cannot_stand_in_for_a_scope_or_carry_a_line_break(packs_env, tmp_path):
    pid = _install(tmp_path)
    with pytest.raises(PackError, match="data scope"):
        reports.render(pid, "brief", sets={"ledger": "forged"})
    with pytest.raises(PackError, match="one line"):
        reports.render(pid, "brief", sets={"title": "a\nb"})
    with pytest.raises(PackError, match="lowercase"):
        reports.render(pid, "brief", sets={"Title": "x"})


def test_each_needs_a_list_and_a_report_must_exist(packs_env, tmp_path):
    pid = _install(tmp_path)
    with pytest.raises(PackError, match="not a list of records"):
        reports.render(pid, "brief", each="ledger.guard.counts")
    with pytest.raises(PackError, match="no report 'nope'"):
        reports.render(pid, "nope")
    with pytest.raises(PackError, match="not installed"):
        reports.render("io.github.example/absent", "brief")


def test_the_cli_renders_and_refuses(packs_env, tmp_path, capsys):
    pid = _install(tmp_path)
    with pytest.raises(SystemExit) as ei:
        main(["pack", "report", pid, "brief", "--set", "title=CLI", "--json"])
    assert ei.value.code == 0
    assert json.loads(capsys.readouterr().out)["text"].startswith("# CLI")
    with pytest.raises(SystemExit) as ei:
        main(["pack", "report", pid, "brief", "--set", "no-equals-sign"])
    assert ei.value.code == 1 and "key=value" in capsys.readouterr().err
    with pytest.raises(SystemExit) as ei:
        main(["pack", "report", pid, "brief", "--since", "last tuesday"])
    assert ei.value.code == 1 and "--since" in capsys.readouterr().err


def test_out_never_writes_over_the_guards_own_files(packs_env, tmp_path, capsys):
    pid = _install(tmp_path)
    gl.append({"decision": "allow", "command": "ls"})
    before = gl.ledger_path().read_bytes()
    for target in (gl.ledger_path(), packs_env.root / "index.json"):
        with pytest.raises(SystemExit) as ei:
            main(["pack", "report", pid, "brief", "--out", str(target)])
        assert ei.value.code == 1 and "never writes over" in capsys.readouterr().err
    assert gl.ledger_path().read_bytes() == before
    with pytest.raises(SystemExit) as ei:
        main(["pack", "report", pid, "brief", "--out", str(tmp_path / "brief.md")])
    assert ei.value.code == 0 and (tmp_path / "brief.md").read_text().startswith("# ")


def test_evidence_from_an_empty_ledger_says_so():
    ev = change_evidence.build(model=None, policies=[])
    assert ev["counts"]["changes"] == 0 and ev["ledger"]["chain_ok"]
    assert "not a SOC 2 report" in ev["note"]
    assert "No change was asked about" in ev["tables"]["changes"]


def test_evidence_keeps_allows_out_of_the_change_list_but_counts_them():
    gl.append({"decision": "allow", "action_type": "infra_apply", "command": "terraform apply",
               "harness": "claude-code"})
    gl.append({"decision": "fail_open", "error": "boom", "harness": "claude-code"})
    ev = change_evidence.build(model=None, policies=[])
    assert ev["counts"]["allowed_by_policy"] == 1
    [c] = ev["changes"]
    assert c["outcome"] == "not_examined" and c["error"] == "boom"
    assert c["show"]["approved_by"] == "-"
