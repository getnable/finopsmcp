"""`nable budget ci-gate`: a pipeline step that can fail on a breached budget.

Invariants under test:
  - by default it reports and exits 0, as it always has (back-compat)
  - with --fail-on-breach a breached budget (spend at or past its critical
    percentage) exits 1; a warning alone does not
  - --json prints one parseable document on stdout, whatever the exit code
  - a budget check that cannot run exits 2 under --fail-on-breach (a gate that
    cannot see must not pass), 0 otherwise
  - --budget-file syncs a budget.yml first
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from finops.budget import enforcer


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    from finops.storage import db
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FINOPS_DB_PATH", raising=False)
    if db._ENGINE is not None:
        db._ENGINE.dispose()
    monkeypatch.setattr(db, "_ENGINE", None)
    monkeypatch.setattr(db, "_DATA_DIR", None)
    yield tmp_path
    if db._ENGINE is not None:
        db._ENGINE.dispose()


def _spend(amount: float) -> None:
    from finops.storage.snapshots import store_snapshot
    store_snapshot("aws", "EC2", "111", "us-east-1",
                   datetime.now().astimezone().date(), amount)


def _run(capsys, **kw):
    code = enforcer.ci_gate(**kw)
    return code, capsys.readouterr().out


def test_no_budgets_passes(isolated_db, capsys):
    code, out = _run(capsys, as_json=True)
    doc = json.loads(out)
    assert code == 0 and doc["ok"] and doc["budgets"] == [] and doc["breached"] == []


def test_a_breach_still_exits_0_by_default(isolated_db, capsys):
    _spend(1_200.0)
    enforcer.create_budget("Total", "total", 1_000.0)
    code, out = _run(capsys)
    assert code == 0
    assert "Total" in out and "exceeded" in out.lower()
    assert "--fail-on-breach" in out


def test_fail_on_breach_fails_the_pipeline(isolated_db, capsys):
    _spend(1_200.0)
    enforcer.create_budget("Total", "total", 1_000.0)
    code, out = _run(capsys, fail_on_breach=True, as_json=True)
    doc = json.loads(out)
    assert code == 1 and doc["exit_code"] == 1 and not doc["ok"]
    assert doc["breached"] == ["Total"]
    (b,) = doc["budgets"]
    assert b["spent"] == 1_200.0 and b["status"] == "exceeded"
    assert doc["fail_on_breach"] is True


def test_the_legacy_fail_on_exceeded_argument_now_works(isolated_db, capsys):
    _spend(1_200.0)
    enforcer.create_budget("Total", "total", 1_000.0)
    assert enforcer.ci_gate(fail_on_exceeded=True) == 1


def test_a_warning_is_not_a_breach(isolated_db, capsys):
    _spend(900.0)
    enforcer.create_budget("Total", "total", 1_000.0)
    code, out = _run(capsys, fail_on_breach=True, as_json=True)
    doc = json.loads(out)
    assert code == 0 and doc["warnings"] == ["Total"] and doc["breached"] == []


def test_the_critical_percentage_is_the_line(isolated_db, capsys):
    _spend(900.0)
    enforcer.create_budget("Total", "total", 1_000.0, critical_at_pct=85.0)
    code, _ = _run(capsys, fail_on_breach=True)
    assert code == 1


def test_an_unreadable_budget_check_fails_closed_only_when_asked(isolated_db, capsys,
                                                                  monkeypatch):
    def boom():
        raise RuntimeError("database is locked")
    monkeypatch.setattr(enforcer, "check_all_budgets", boom)
    code, out = _run(capsys, fail_on_breach=True, as_json=True)
    doc = json.loads(out)
    assert code == 2 and "database is locked" in doc["error"]
    code, _ = _run(capsys)
    assert code == 0


def test_a_budget_file_is_synced_first(isolated_db, capsys, tmp_path):
    _spend(600.0)
    yml = tmp_path / "budget.yml"
    yml.write_text("budgets:\n  - name: From YAML\n    scope_type: total\n"
                   "    limit_usd: 500\n")
    code, out = _run(capsys, budget_yaml=str(yml), fail_on_breach=True, as_json=True)
    doc = json.loads(out)
    assert code == 1 and doc["breached"] == ["From YAML"]
    assert doc["sync"]["created"] == ["From YAML"]


def test_a_missing_budget_file_fails_closed_only_when_asked(isolated_db, capsys, tmp_path):
    code, out = _run(capsys, budget_yaml=str(tmp_path / "nope.yml"), fail_on_breach=True,
                     as_json=True)
    assert code == 2 and "nope.yml" in json.loads(out)["error"]
    code, _ = _run(capsys, budget_yaml=str(tmp_path / "nope.yml"))
    assert code == 0


def test_the_cli(isolated_db, capsys):
    from finops.setup_wizard import main
    _spend(1_200.0)
    enforcer.create_budget("Total", "total", 1_000.0)
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        main(["budget", "ci-gate", "--fail-on-breach", "--json"])
    assert e.value.code == 1
    doc = json.loads(capsys.readouterr().out)     # stdout is only the document
    assert doc["breached"] == ["Total"]
    with pytest.raises(SystemExit) as e:
        main(["budget", "ci-gate"])
    assert e.value.code == 0
