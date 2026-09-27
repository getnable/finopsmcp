# SPDX-License-Identifier: Apache-2.0
"""`nable pack report`: pack report templates rendered over nable's own data
(finops.packs.reports), the evidence the ledger scope builds
(finops.change_evidence) and the AI spend the `ai` source reads.

What has to stay true:
  - a template reads a data scope only when its pack declares it; one that
    reads an undeclared scope is refused and the source never runs
  - with no report named, a pack's only report; a pack with several asks
  - --days N is --since Nd, and the two are not given together
  - `--set` fills plain placeholders and can never stand in for a scope
  - nothing in a template is evaluated
  - the evidence says what it is, and an empty ledger is evidence of nothing
"""
from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta

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


def test_out_refuses_the_guards_files_however_they_are_reached(packs_env, tmp_path, capsys,
                                                               monkeypatch):
    # A hard link to the ledger outside the data dir, a symlink to it, a
    # budget file and another repo's org model are the guard's files too.
    pid = _install(tmp_path)
    gl.append({"decision": "allow", "command": "ls"})
    before = gl.ledger_path().read_bytes()
    hard = tmp_path / "hard-link.md"
    os.link(gl.ledger_path(), hard)
    soft = tmp_path / "soft-link.md"
    soft.symlink_to(gl.ledger_path())
    (tmp_path / "other" / "nable.org").mkdir(parents=True)
    targets = (hard, soft, tmp_path / "budget.yml",
               tmp_path / "other" / "nable.org" / "freezes.yaml")
    for target in targets:
        with pytest.raises(SystemExit) as ei:
            main(["pack", "report", pid, "brief", "--out", str(target)])
        assert ei.value.code == 1, target
        assert "never writes" in capsys.readouterr().err, target
    assert gl.ledger_path().read_bytes() == before
    assert not (tmp_path / "budget.yml").exists()
    assert not (tmp_path / "other" / "nable.org" / "freezes.yaml").exists()


def test_out_refuses_the_guards_files_on_a_case_insensitive_disk(packs_env, tmp_path, capsys,
                                                                  monkeypatch):
    # guard_paths folds case on macOS and Windows; --out must compare the same
    # way, or /Users/Alice/... never equals the folded /users/alice/...
    from finops import guard_paths
    org_dir = tmp_path / "Org"
    org_dir.mkdir()
    monkeypatch.setenv("FINOPS_ORG_DIR", str(org_dir))
    monkeypatch.setattr(guard_paths, "_FOLD", True)
    pid = _install(tmp_path)
    with pytest.raises(SystemExit) as ei:
        main(["pack", "report", pid, "brief", "--out", str(org_dir / "freezes.yaml")])
    assert ei.value.code == 1 and "never writes" in capsys.readouterr().err
    assert not (org_dir / "freezes.yaml").exists()


def test_out_writes_nothing_that_can_drive_a_terminal(packs_env, tmp_path, capsys):
    # A proposal a pack adapter made can carry an escape sequence; the file a
    # person later cats shows it as text, as the terminal output does.
    from finops import org
    org.propose(org.make_fact("approval", "team:platform",
                              {"action_classes": ["*"], "approvers": ["team:x\x1b[2Jwiped"],
                               "min": 1}, source="pack:io.github.example/evil:a"))
    pid = _install(tmp_path, text="${ledger.guard.tables.approval_chains}\n")
    target = tmp_path / "chains.md"
    with pytest.raises(SystemExit) as ei:
        main(["pack", "report", pid, "brief", "--out", str(target)])
    assert ei.value.code == 0
    text = target.read_text()
    assert "wiped" in text and "\x1b" not in text and "\\x1b[2J" in text


def test_set_cannot_stand_in_for_what_nable_fills(packs_env, tmp_path):
    # The period, the time it was made and the report's own name are nable's:
    # an evidence report that claims another period than it read is forged.
    pid = _install(tmp_path, text="${since} ${until} ${generated_at} ${pack} ${report}")
    for key in ("since", "until", "generated_at", "pack", "report", "item"):
        with pytest.raises(PackError, match="nable fills"):
            reports.render(pid, "brief", sets={key: "2020-01-01"})


def test_no_record_at_or_after_a_break_is_shown_as_intact():
    # A change ticket says "chain intact at this line" for its record. An
    # edited record still chains to the one before it (only the next one
    # shows the edit), and one after the break chains to a line nobody can
    # vouch for: neither is intact.
    for cmd in ("terraform apply", "terraform destroy", "helm upgrade web ./chart"):
        gl.append({"decision": "ask", "action_type": "infra_apply", "command": cmd,
                   "harness": "claude-code"})
    p = gl.ledger_path()
    lines = p.read_text().splitlines()
    lines[0] = lines[0].replace("terraform apply", "ls")
    p.write_text("\n".join(lines) + "\n")
    ev = change_evidence.build(model=None, policies=[])
    assert not ev["ledger"]["chain_ok"] and ev["ledger"]["broken_at"] == 2
    assert [c["chain_ok"] for c in ev["changes"]] == [False, False, False]


def test_a_clean_ledger_shows_each_record_intact():
    for cmd in ("terraform apply", "terraform destroy"):
        gl.append({"decision": "ask", "action_type": "infra_apply", "command": cmd,
                   "harness": "claude-code"})
    ev = change_evidence.build(model=None, policies=[])
    assert ev["ledger"]["chain_ok"] and all(c["chain_ok"] for c in ev["changes"])


def test_an_out_of_band_approval_answers_only_the_ask_it_could_have_answered():
    # Approval ids are short and live 15 minutes, so one comes round again.
    # An approval answers an ask with its id from the ask until the id
    # expires, wherever the period ends; not an older ask that happened to
    # carry the same id.
    t0 = datetime(2026, 9, 1, 12, tzinfo=UTC)

    def at(minutes):
        return (t0 + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    cmd = "terraform destroy -target=aws_instance.old"
    gl.append({"ts": at(0), "decision": "ask", "action_type": "infra_destroy",
               "command": cmd, "harness": "codex", "approval_id": "0badcafe"})
    gl.append({"ts": at(3 * 24 * 60), "decision": "ask", "action_type": "infra_destroy",
               "command": cmd, "harness": "codex", "approval_id": "0badcafe"})
    gl.append({"ts": at(3 * 24 * 60 + 5), "decision": "allow", "action_type": "infra_destroy",
               "command": cmd, "harness": "codex",
               "approved_out_of_band": {"id": "0badcafe", "by": "dana", "at": at(3 * 24 * 60 + 4)}})
    now = t0 + timedelta(days=4)
    ev = change_evidence.build(model=None, policies=[], now=now)
    first, second, _let_through = ev["changes"]
    assert first["outcome"] == "not_run" and first["approved_by"] is None
    assert second["outcome"] == "approved_later_out_of_band"
    assert second["approved_by"] == "dana" and second["answered_by_line"] == 3
    # A period that ends between the ask and the approval still shows it.
    ev = change_evidence.build(model=None, policies=[], now=now,
                               until=t0 + timedelta(days=3, minutes=1))
    first, second = ev["changes"]
    assert first["outcome"] == "not_run"
    assert second["outcome"] == "approved_later_out_of_band"
    assert second["approved_by"] == "dana" and second["answered_by_line"] == 3


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


# ── the `ai` source (focus.cost), and picking a report ────────────────────────

AI_TEMPLATE = "Total ${ai.total_usd} for ${ai.period}; ${who.knows} stays; ${period}\n"


@pytest.fixture
def filled(monkeypatch):
    calls = []

    def fill(*, days, pack_id, **_):
        calls.append((days, pack_id))
        return {"total_usd": "12.50", "period": "last week", "table": ["not", "a", "scalar"]}

    monkeypatch.setitem(reports.SOURCES, "ai", reports.Source("focus.cost", fill, "test"))
    return calls


def _pack(tmp_path, *, caps: str, reports_: dict[str, str]):
    src = make_pack(tmp_path / "src", capabilities=caps,
                    provides='reports = ["reports/*.md"]\n')
    for name, text in reports_.items():
        (src / "reports").mkdir(exist_ok=True)
        (src / "reports" / name).write_text(text)
    inst.install(str(src), yes=True)
    return "io.github.example/demo"


def test_a_declared_source_fills_its_placeholders(packs_env, tmp_path, filled, capsys):
    pid = _pack(tmp_path, caps='read_data = ["focus.cost"]\n', reports_={"r.md": AI_TEMPLATE})
    assert reports.scopes_in(AI_TEMPLATE) == ["focus.cost"]
    r = reports.render(pid, days=7)
    assert r["text"] == "Total 12.50 for last week; ${who.knows} stays; ${period}\n"
    assert r["sources"] == ["ai"] and r["scopes"] == ["focus.cost"] and filled == [(7, pid)]
    with pytest.raises(SystemExit) as ei:
        main(["pack", "report", pid, "r"])
    assert ei.value.code == 0 and "Total 12.50" in capsys.readouterr().out


def test_an_undeclared_source_is_not_read_and_is_refused(packs_env, tmp_path, filled, capsys):
    # The two report designs differed here: one left the placeholders as
    # written with a note, the other refused. A report that silently lacks
    # its numbers is easy to mistake for one that has none, so it refuses.
    pid = _pack(tmp_path, caps='read_data = ["org.owners"]\n', reports_={"r.md": AI_TEMPLATE})
    with pytest.raises(PolicyRefusal, match="focus.cost, which the pack does not declare"):
        reports.render(pid)
    assert filled == []
    with pytest.raises(SystemExit) as ei:
        main(["pack", "report", pid])
    assert ei.value.code == 1 and "does not declare" in capsys.readouterr().err
    assert filled == []


def test_picking_a_report(packs_env, tmp_path, filled, capsys):
    pid = _pack(tmp_path, caps='read_data = ["focus.cost"]\n',
                reports_={"a.md": "A ${ai.total_usd}", "b.md": "B"})
    with pytest.raises(PackError, match="has 2 reports; name one"):
        reports.render(pid)
    assert reports.render(pid, "reports/a.md")["text"] == "A 12.50"
    assert reports.render(pid, "b.md")["text"] == "B"
    with pytest.raises(PackError, match="has no report 'c'"):
        reports.render(pid, "c")
    with pytest.raises(PackError, match="not installed"):
        reports.render("io.github.example/absent")
    with pytest.raises(PackError, match="at least 1"):
        reports.render(pid, "a", days=0)
    with pytest.raises(SystemExit) as ei:
        main(["pack", "report", pid])
    assert ei.value.code == 1 and "name one" in capsys.readouterr().err


def test_days_is_since_in_days_and_not_both(packs_env, tmp_path, filled, capsys):
    pid = _pack(tmp_path, caps='read_data = ["focus.cost"]\n', reports_={"r.md": AI_TEMPLATE})
    with pytest.raises(PackError, match="give one of them"):
        reports.render(pid, days=7, since=datetime(2026, 9, 1, tzinfo=UTC))
    now = datetime(2026, 9, 27, 12, tzinfo=UTC)
    r = reports.render(pid, days=7, now=now)
    assert r["values"]["since"] == "2026-09-20T12:00:00+00:00"
    with pytest.raises(SystemExit) as ei:
        main(["pack", "report", pid, "--days", "3", "--since", "3d"])
    assert ei.value.code == 1 and "give one of them" in capsys.readouterr().err
    with pytest.raises(SystemExit) as ei:
        main(["pack", "report", pid, "--days", "3", "--json"])
    assert ei.value.code == 0 and json.loads(capsys.readouterr().out)["sources"] == ["ai"]
    assert filled[-1] == (3, pid)


def test_the_ai_window_counts_local_days():
    today = date(2026, 9, 27)
    assert reports._ai_window(None, None, 7, today) == (date(2026, 9, 20), today, 7)
    assert reports._ai_window(None, None, None, today) == (date(2026, 8, 28), today, 30)
    since = datetime(2026, 9, 13, 12, tzinfo=UTC)
    _start, end, days = reports._ai_window(since, None, None, today)
    assert end == today and days == (today - since.astimezone().date()).days


def test_a_pack_with_commitment_bounds_beside_its_rules_still_reports(packs_env, tmp_path):
    # A policy file may hold commitment_bounds as well as rules; the report's
    # policy check reads the rules and passes over the bounds.
    src = make_pack(tmp_path / "src", capabilities='read_data = ["focus.cost"]\n',
                    provides='policies = ["policies/*.yaml"]\nreports = ["reports/*.md"]\n')
    (src / "policies" / "rules.yaml").write_text(
        (src / "policies" / "rules.yaml").read_text() +
        "commitment_bounds:\n  - id: cap\n    description: At most 12 months.\n"
        "    max_term_months: 12\n")
    (src / "reports").mkdir()
    (src / "reports" / "r.md").write_text("${ai.policy_findings}")
    inst.install(str(src), yes=True)
    lines = reports._policy_lines("io.github.example/demo", {"monthly_delta_usd": 5000})
    assert lines == ["- medium (flag, rule big-increase): adds 5000/mo"]


def test_set_cannot_stand_in_for_the_ai_source(packs_env, tmp_path, filled):
    pid = _pack(tmp_path, caps='read_data = ["focus.cost"]\n', reports_={"r.md": AI_TEMPLATE})
    with pytest.raises(PackError, match="data scope"):
        reports.render(pid, sets={"ai": "forged"})
