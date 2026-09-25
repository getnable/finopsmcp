"""`nable guard doctor` says which cloud budgets the guard enforces, and how
fresh its spend figure is.

A budget stop people believe is on, running on a spend figure from last week
or on no figure at all, is worse than none. So the doctor names each budget
the guard checks priced changes against, the budgets it cannot place a change
in (a team or account budget with nothing saying which team or account), what
a breach gets (ask or deny, and which setting says so), and the age of the
figure, with the command that refreshes it.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from datetime import UTC, datetime, timedelta

import pytest

import finops.guard as g
from finops.budget import summary as bs


@pytest.fixture
def machine(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    for var in ("FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_ON_BUDGET_BREACH",
                "FINOPS_GUARD_TEAM", "FINOPS_GUARD_ACCOUNT",
                "FINOPS_GUARD_BUDGET_MAX_AGE_HOURS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_POLICY_FILE", str(tmp_path / "nable.policy.yaml"))
    files = {False: tmp_path / "project.json", True: home / ".claude" / "settings.json"}
    monkeypatch.setattr(g, "_settings_path", lambda global_scope: files[global_scope])
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: "/usr/bin/uvx" if n == "uvx" else None)
    return tmp_path


def _budget(name, *, scope_type="total", scope_value="*", spent=100.0, limit=1_000.0):
    today = datetime.now().astimezone().date()
    start = today.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return {"name": name, "scope_type": scope_type, "scope_value": scope_value,
            "period": "monthly", "period_start": start.isoformat(),
            "period_end": end.isoformat(), "spent": spent, "limit": limit,
            "pct_used": round(spent / limit * 100, 1), "status": "ok"}


def _summary(*budgets, age_hours=3.0):
    bs.write_summary(list(budgets), spend_through="2026-09-24",
                     now=datetime.now(UTC) - timedelta(hours=age_hours))


def _cli(**kw):
    from finops import setup_wizard
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        setup_wizard._run_guard(argparse.Namespace(guard_action="doctor", guard_global=False,
                                                   **kw))
    return out.getvalue()


def test_the_doctor_names_the_budgets_it_enforces(machine):
    _summary(_budget("Cloud total"), _budget("AWS", scope_type="provider", scope_value="aws"))
    b = g.doctor()["budgets"]
    assert b["state"] == "fresh" and b["age_hours"] == pytest.approx(3.0, abs=0.1)
    assert [e["name"] for e in b["enforced"]] == ["Cloud total", "AWS"]
    assert b["on_breach"] == "ask" and b["on_breach_source"] == "default"
    assert b["spend_through"] == "2026-09-24"


def test_team_and_account_budgets_need_the_guard_told_which(machine, monkeypatch):
    _summary(_budget("Platform", scope_type="team", scope_value="platform"),
             _budget("Prod", scope_type="account", scope_value="123456789012"))
    d = g.doctor()
    assert d["budgets"]["enforced"] == []
    assert [x["name"] for x in d["budgets"]["not_enforced"]] == ["Platform", "Prod"]
    assert any("FINOPS_GUARD_TEAM" in n for n in d["not_covered"])
    monkeypatch.setenv("FINOPS_GUARD_TEAM", "platform")
    assert [e["name"] for e in g.doctor()["budgets"]["enforced"]] == ["Platform"]


def test_a_stale_figure_is_a_gap_with_its_fix(machine):
    _summary(_budget("Cloud total"), age_hours=72)
    d = g.doctor()
    assert d["budgets"]["state"] == "stale"
    assert any("3 days old" in n for n in d["not_covered"])
    assert any(r.startswith("nable budget refresh") for r in d["recommendations"])


def test_no_figure_is_a_gap_with_its_fix(machine):
    d = g.doctor()
    assert d["budgets"]["state"] == "absent"
    assert any("no spend figure" in n for n in d["not_covered"])
    assert any(r.startswith("nable budget refresh") for r in d["recommendations"])


def test_no_budgets_set_says_how_to_set_one(machine):
    _summary()
    d = g.doctor()
    assert d["budgets"]["state"] == "fresh" and d["budgets"]["enforced"] == []
    assert any("no cloud budget" in n for n in d["not_covered"])


def test_the_doctor_says_what_a_breach_gets_and_why(machine, monkeypatch):
    (machine / "nable.policy.yaml").write_text("on_budget_breach: deny\n")
    _summary(_budget("Cloud total"))
    b = g.doctor()["budgets"]
    assert (b["on_breach"], b["on_breach_source"]) == ("deny", "policy file")
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "0")
    b = g.doctor()["budgets"]
    assert (b["on_breach"], b["on_breach_source"]) == ("ask", "FINOPS_GUARD_STOP_ON_BUDGET")


def test_the_cli_prints_the_budget_section(machine):
    _summary(_budget("Cloud total", spent=640.0, limit=1_000.0))
    out = _cli(guard_json=False)
    flat = " ".join(out.split())
    assert "Cloud budgets" in out
    assert "Cloud total (total): $640 of $1,000" in flat
    assert "spend figure from 3 hours ago" in flat
    assert "cost data through 2026-09-24" in flat
    assert "a change over budget asks" in flat


def test_the_cli_json_carries_the_budgets(machine):
    _summary(_budget("Cloud total"))
    data = json.loads(_cli(guard_json=True))
    assert data["budgets"]["enforced"][0]["name"] == "Cloud total"


def test_the_readme_documents_the_budget_stop():
    from pathlib import Path
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    guard = readme[readme.index("## Agent guard"):readme.index("## Setup")]
    for needle in ("on_budget_breach: deny", "FINOPS_GUARD_STOP_ON_BUDGET",
                   "nable budget refresh", "nable budget ci-gate --fail-on-breach",
                   "FINOPS_GUARD_TEAM", "FINOPS_GUARD_BUDGET_MAX_AGE_HOURS",
                   "nable guard doctor"):
        assert needle in guard, needle
