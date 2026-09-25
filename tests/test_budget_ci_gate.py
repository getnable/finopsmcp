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
from datetime import datetime, timedelta

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


# ── no cost data is not $0 spent ──────────────────────────────────────────────

def _checks():
    return {b["name"]: b for b in enforcer.check_all_budgets()}


def test_no_cost_data_in_the_period_is_no_data_not_ok(isolated_db):
    enforcer.create_budget("Total", "total", 100.0)
    (b,) = enforcer.check_all_budgets()
    assert b["status"] == "no_data" and b["cost_rows"] == 0


def test_a_fresh_runner_cannot_pass_the_gate(isolated_db, capsys):
    enforcer.create_budget("Total", "total", 100.0)
    capsys.readouterr()
    code, out = _run(capsys, fail_on_breach=True)
    assert code == 2
    assert "[no data] Total" in out and "cannot check" in out
    assert "Failing the step (--fail-on-breach)." in out
    code, out = _run(capsys, fail_on_breach=True, as_json=True)
    doc = json.loads(out)
    assert code == 2 and doc["cannot_check"] == ["Total"] and not doc["ok"]
    assert doc["spend_through"] is None
    code, out = _run(capsys)
    assert code == 0 and "pass --fail-on-breach" in out


def test_the_cli_on_a_fresh_runner_exits_2(isolated_db, capsys, tmp_path):
    from finops.setup_wizard import main
    yml = tmp_path / "budget.yml"
    yml.write_text("budgets:\n  - name: Total\n    scope_type: total\n    limit_usd: 100\n")
    with pytest.raises(SystemExit) as e:
        main(["budget", "ci-gate", "--fail-on-breach", "--budget-file", str(yml)])
    assert e.value.code == 2 and "cannot check" in capsys.readouterr().out


def test_a_breach_beside_a_budget_with_no_data_still_exits_1(isolated_db, capsys):
    _spend(1_200.0)
    enforcer.create_budget("Total", "total", 1_000.0)
    enforcer.create_budget("Azure", "provider", 1_000.0, scope_value="azure")
    code, out = _run(capsys, fail_on_breach=True, as_json=True)
    doc = json.loads(out)
    assert code == 1 and doc["breached"] == ["Total"] and doc["cannot_check"] == ["Azure"]


def test_last_months_rows_are_not_this_months_data(isolated_db):
    from finops.budget.summary import read_summary
    from finops.storage.snapshots import store_snapshot
    first = datetime.now().astimezone().date().replace(day=1)
    store_snapshot("aws", "EC2", "111", "us-east-1", first - timedelta(days=3), 50.0)
    enforcer.create_budget("Total", "total", 100.0)
    (b,) = enforcer.check_all_budgets()
    assert b["status"] == "no_data"
    assert read_summary()["spend_through"] is None


def test_a_service_nobody_used_this_period_spent_nothing(isolated_db):
    _spend(10.0)                                     # EC2 rows exist this month
    enforcer.create_budget("Unused", "service", 100.0, scope_value="Amazon Unused Service")
    assert _checks()["Unused"]["status"] == "ok"


def test_the_guard_does_not_read_no_data_as_a_fresh_zero(isolated_db):
    import finops.guard as g
    from finops.budget import summary as bs
    enforcer.create_budget("Total", "total", 100.0)
    doc = bs.read_summary()
    assert bs.freshness(doc)["state"] == "no_data"
    assert bs.current_budgets(doc) == []
    lens = g.budget_lens("aws ec2 run-instances --instance-type m5.2xlarge",
                         {"monthly_usd": 1_000.0})
    assert lens is not None and lens["state"] == "no_data"


def test_a_summary_whose_cost_data_predates_the_period_is_no_data(isolated_db):
    from finops.budget import summary as bs
    first = datetime.now().astimezone().date().replace(day=1)
    end = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    row = {"name": "T", "scope_type": "total", "scope_value": "*", "period": "monthly",
           "period_start": first.isoformat(), "period_end": end.isoformat(),
           "spent": 0.0, "limit": 100.0, "pct_used": 0.0, "status": "ok"}
    bs.write_summary([row], spend_through=(first - timedelta(days=1)).isoformat())
    assert bs.freshness(bs.read_summary())["state"] == "no_data"
    bs.write_summary([row], spend_through=first.isoformat())
    assert bs.freshness(bs.read_summary())["state"] == "fresh"


def test_a_budget_that_cannot_be_checked_is_reported_not_dropped(isolated_db, capsys):
    from sqlalchemy import update

    from finops.storage.db import budgets, get_engine
    _spend(10.0)
    enforcer.create_budget("Odd", "total", 100.0)
    with get_engine().begin() as conn:               # a row an older version wrote
        conn.execute(update(budgets).values(scope_type="region"))
    (b,) = enforcer.check_all_budgets()
    assert b["status"] == "error" and "region" in b["error"]
    code, out = _run(capsys, fail_on_breach=True, as_json=True)
    assert code == 2 and json.loads(out)["cannot_check"] == ["Odd"]


# ── a budget.yml that cannot be read ──────────────────────────────────────────

@pytest.mark.parametrize("text", ["budgets: [\n  - name: x\n", "", "- just\n- a list\n",
                                  "budgets: nope\n"])
def test_a_malformed_budget_file_fails_closed_only_when_asked(isolated_db, capsys, tmp_path,
                                                              text):
    yml = tmp_path / "budget.yml"
    yml.write_text(text)
    code, out = _run(capsys, budget_yaml=str(yml), fail_on_breach=True, as_json=True)
    doc = json.loads(out)
    assert code == 2 and "budget.yml" in doc["error"] and doc["sync"]["error"]
    code, out = _run(capsys, budget_yaml=str(yml))
    assert code == 0 and "Budget check failed" in out
    assert enforcer.list_budgets() == []


@pytest.mark.parametrize("entry,said", [
    ("scope_type: region\n    limit_usd: 100", "scope_type"),
    ("scope_type: total\n    limit_usd: 0", "more than 0"),
    ("scope_type: total", "limit_usd is missing"),
])
def test_an_invalid_budget_in_the_file_is_refused_whole(isolated_db, tmp_path, entry, said):
    yml = tmp_path / "budget.yml"
    yml.write_text("budgets:\n  - name: Good\n    scope_type: total\n    limit_usd: 5\n"
                   f"  - name: Bad\n    {entry}\n")
    got = enforcer.sync_from_yaml(str(yml))
    assert said in got["error"] and "Bad" in got["error"]
    assert enforcer.list_budgets() == []             # nothing half-synced


@pytest.mark.parametrize("scope_type,limit", [("region", 100.0), ("total", 0.0),
                                              ("total", -5.0)])
def test_create_refuses_a_budget_that_would_always_read_ok(isolated_db, scope_type, limit):
    with pytest.raises(ValueError):
        enforcer.create_budget("X", scope_type, limit)
    assert enforcer.list_budgets() == []


def test_a_service_short_name_resolves_to_the_cost_explorer_name(isolated_db):
    from finops.storage.snapshots import store_snapshot
    b = enforcer.create_budget("Before", "service", 100.0, scope_value="ec2")
    assert b["scope_value"] == "Amazon Elastic Compute Cloud"
    store_snapshot("aws", "Amazon Elastic Compute Cloud - Compute", "111", "us-east-1",
                   datetime.now().astimezone().date(), 30.0)
    b = enforcer.create_budget("After", "service", 100.0, scope_value="EC2")
    assert b["scope_value"] == "Amazon Elastic Compute Cloud - Compute"
    got = _checks()
    assert got["Before"]["spent"] == 30.0 and got["After"]["spent"] == 30.0


# ── the summary file ──────────────────────────────────────────────────────────

def test_concurrent_summary_writers_each_get_their_own_temp_file(isolated_db):
    import threading

    from finops.budget import summary as bs
    results: list = []

    def write():
        for _ in range(25):
            results.append(bs.write_summary([]))

    threads = [threading.Thread(target=write) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 150 and all(r is not None for r in results)
    assert bs.read_summary() is not None
    assert not [p for p in bs.summary_path().parent.iterdir() if p.name.endswith(".tmp")]
