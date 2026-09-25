"""The guard's budget lens: a priced change judged against the cloud budget.

Invariants under test:
  - a priced change is compared with what is left of the budget, not only with
    the per-action threshold: month-to-date spend plus the change's cost for the
    rest of the period, over the limit, is an ask (cost_verdict "over_budget"
    goes into evaluate_action_gate), and FINOPS_GUARD_STOP_ON_BUDGET=1 makes it
    a deny
  - the reason names the budget, the spend so far, the change's monthly figure
    and the projected overage, and the ledger records the same figures
  - the lens reads a small JSON summary written by the budget checks, never the
    database: the hook does not import SQLAlchemy for it
  - a stale or missing summary skips the lens, and a verdict on a priced change
    says so; a silent allow stays silent
  - a scoped budget applies only to changes the guard can place in its scope
  - savings and unpriced commands never meet the lens
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

import finops.guard as g
import finops.guard_ledger as gl
from finops import ai_budget
from finops.aws_prices import EC2_HOURLY
from finops.budget import summary as bs

M5_2XL = "aws ec2 run-instances --instance-type m5.2xlarge"
M5_2XL_MONTHLY = EC2_HOURLY["m5.2xlarge"] * 730
P4D = "aws ec2 run-instances --instance-type p4d.24xlarge --count 8"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_VELOCITY_CAP_USD",
                "FINOPS_POLICY_LOOP_COUNT", "FINOPS_GUARD_TEAM", "FINOPS_GUARD_ACCOUNT",
                "FINOPS_GUARD_BUDGET_MAX_AGE_HOURS", "FINOPS_POLICY_ON_BUDGET_BREACH",
                "FINOPS_POLICY_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})


def _today() -> date:
    return datetime.now().astimezone().date()


def _month() -> tuple[str, str]:
    today = _today()
    start = today.replace(day=1)
    nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    return start.isoformat(), (nxt - timedelta(days=1)).isoformat()


def _days_left() -> int:
    return (date.fromisoformat(_month()[1]) - _today()).days + 1


def _budget(name="Cloud total", *, spent, limit, scope_type="total", scope_value="*"):
    start, end = _month()
    return {"name": name, "scope_type": scope_type, "scope_value": scope_value,
            "period": "monthly", "period_start": start, "period_end": end,
            "spent": spent, "limit": limit, "pct_used": round(spent / limit * 100, 1),
            "status": "ok"}


def _summary(*budgets, age_hours: float = 2.0):
    as_of = datetime.now(UTC) - timedelta(hours=age_hours)
    bs.write_summary(list(budgets), spend_through=_today().isoformat(), now=as_of)


def _records():
    p = gl.ledger_path()
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


# ── over budget ───────────────────────────────────────────────────────────────

def test_a_cheap_launch_that_breaks_the_budget_asks_and_says_why():
    """$280/mo is well under the $500 per-action threshold: before, silent."""
    _summary(_budget("AWS Total", spent=49_999.0, limit=50_000.0))
    v = g.gate_command(M5_2XL)
    assert v is not None and v["decision"] == "ask"
    reason = v["reason"]
    assert "'AWS Total'" in reason
    assert "$49,999 spent month to date" in reason
    assert f"~${M5_2XL_MONTHLY:,.0f}/mo" in reason
    rest = M5_2XL_MONTHLY * _days_left() * 24 / 730
    over = 49_999.0 + rest - 50_000.0
    assert f"~${over:,.0f} over" in reason
    assert "FINOPS_GUARD_STOP_ON_BUDGET" in reason
    rec = _records()[-1]
    assert rec["decision"] == "ask"
    bc = rec["budget_check"]
    assert bc["state"] == "over" and bc["budget"] == "AWS Total"
    assert bc["spent_mtd"] == 49_999.0 and bc["limit"] == 50_000.0
    assert bc["change_monthly_usd"] == pytest.approx(M5_2XL_MONTHLY, abs=0.01)
    assert bc["projected_overage_usd"] == pytest.approx(over, abs=0.01)


def test_over_budget_reaches_the_policy_gate_as_its_cost_verdict(monkeypatch):
    seen = []
    real = g.evaluate_action_gate

    def spy(*a, **k):
        seen.append(k.get("cost_verdict"))
        return real(*a, **k)
    monkeypatch.setattr(g, "evaluate_action_gate", spy)
    _summary(_budget(spent=49_999.0, limit=50_000.0))
    g.gate_command(M5_2XL)
    assert seen == ["over_budget"]


def test_stop_on_budget_makes_it_a_deny(monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    _summary(_budget(spent=49_999.0, limit=50_000.0))
    v = g.gate_command(M5_2XL)
    assert v["decision"] == "deny"
    assert "Stopped because FINOPS_GUARD_STOP_ON_BUDGET is on" in v["reason"]
    assert _records()[-1]["outcome"] == "not_run"


def test_a_budget_already_blown_asks_on_any_priced_launch():
    _summary(_budget(spent=61_000.0, limit=50_000.0))
    v = g.gate_command(M5_2XL)
    assert v["decision"] == "ask" and "$61,000 spent month to date" in v["reason"]


def test_an_expensive_launch_names_the_budget_not_just_the_threshold():
    _summary(_budget("Cloud total", spent=46_000.0, limit=50_000.0))
    v = g.gate_command(P4D)
    assert v["decision"] == "ask"
    assert "'Cloud total'" in v["reason"]


# ── within budget ─────────────────────────────────────────────────────────────

def test_within_budget_stays_silent_and_is_recorded():
    _summary(_budget(spent=1_000.0, limit=50_000.0))
    assert g.gate_command(M5_2XL) is None
    bc = _records()[-1]["budget_check"]
    assert bc["state"] == "within" and bc["checked"] == ["Cloud total"]


def test_a_saving_never_meets_the_budget():
    _summary(_budget(spent=61_000.0, limit=50_000.0))
    est = {"monthly_usd": -200.0, "line": "saves $200/mo", "basis": "x"}
    assert g.budget_lens(M5_2XL, est) is None


def test_an_unpriced_command_is_not_budget_checked():
    _summary(_budget(spent=61_000.0, limit=50_000.0))
    v = g.gate_command("terraform destroy -auto-approve")
    assert v["decision"] == "ask" and "budget" not in v["reason"].lower()
    assert "budget_check" not in _records()[-1]


# ── stale or missing figures ──────────────────────────────────────────────────

def test_no_summary_skips_the_lens_and_a_priced_ask_says_so():
    v = g.gate_command(P4D)
    assert v["decision"] == "ask"
    assert "Budget not checked" in v["reason"] and "nable budget refresh" in v["reason"]
    assert _records()[-1]["budget_check"]["state"] == "absent"


def test_no_summary_leaves_a_silent_allow_silent():
    assert g.gate_command(M5_2XL) is None
    assert _records()[-1]["budget_check"]["state"] == "absent"


def test_a_stale_summary_is_not_trusted():
    _summary(_budget(spent=49_999.0, limit=50_000.0), age_hours=72)
    assert g.gate_command(M5_2XL) is None, "three-day-old spend must not stop anyone"
    assert _records()[-1]["budget_check"]["state"] == "stale"
    v = g.gate_command(P4D)
    assert "Budget not checked" in v["reason"] and "3 days old" in v["reason"]


def test_the_age_limit_is_configurable(monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD_BUDGET_MAX_AGE_HOURS", "1")
    _summary(_budget(spent=49_999.0, limit=50_000.0), age_hours=2)
    assert g.gate_command(M5_2XL) is None
    assert _records()[-1]["budget_check"]["state"] == "stale"


def test_last_months_figures_are_stale_whatever_their_age():
    doc = {"as_of": "2020-01-31T23:00:00+00:00", "budgets": []}
    assert bs.freshness(doc, now=datetime(2020, 2, 1, 1, 0, tzinfo=UTC))["state"] == "stale"


def test_a_budget_whose_period_has_ended_is_ignored():
    b = _budget(spent=49_999.0, limit=50_000.0)
    b["period_start"], b["period_end"] = "2020-01-01", "2020-01-31"
    _summary(b)
    assert g.gate_command(M5_2XL) is None
    assert _records()[-1]["budget_check"]["state"] == "no_budget"


# ── scope ─────────────────────────────────────────────────────────────────────

def test_a_provider_budget_applies_to_that_provider_only():
    _summary(_budget("GCP", spent=9_999.0, limit=10_000.0,
                     scope_type="provider", scope_value="gcp"))
    assert g.gate_command(M5_2XL) is None          # an AWS launch
    assert _records()[-1]["budget_check"]["state"] == "no_budget"
    _summary(_budget("AWS", spent=9_999.0, limit=10_000.0,
                     scope_type="provider", scope_value="aws"))
    v = g.gate_command(M5_2XL)
    assert v["decision"] == "ask" and "'AWS' budget (provider aws)" in v["reason"]


def test_a_team_budget_applies_when_the_guard_knows_the_team(monkeypatch):
    _summary(_budget("Platform", spent=9_999.0, limit=10_000.0,
                     scope_type="team", scope_value="platform"))
    assert g.gate_command(M5_2XL) is None
    monkeypatch.setenv("FINOPS_GUARD_TEAM", "platform")
    assert g.gate_command(M5_2XL)["decision"] == "ask"


def test_an_account_budget_applies_when_the_guard_knows_the_account(monkeypatch):
    _summary(_budget("Prod account", spent=9_999.0, limit=10_000.0,
                     scope_type="account", scope_value="123456789012"))
    assert g.gate_command(M5_2XL) is None
    monkeypatch.setenv("FINOPS_GUARD_ACCOUNT", "123456789012")
    assert g.gate_command(M5_2XL)["decision"] == "ask"


def test_a_service_budget_applies_to_its_service():
    _summary(_budget("EC2", spent=9_999.0, limit=10_000.0, scope_type="service",
                     scope_value="Amazon Elastic Compute Cloud - Compute"))
    assert g.gate_command(M5_2XL)["decision"] == "ask"


def test_the_largest_overage_is_named_and_the_others_counted():
    _summary(_budget("Small", spent=9_999.0, limit=10_000.0),
             _budget("Big", spent=60_000.0, limit=50_000.0))
    v = g.gate_command(M5_2XL)
    assert "'Big'" in v["reason"] and "1 other budget" in v["reason"]


# ── the MCP door gets the same lens ───────────────────────────────────────────

def test_an_mcp_launch_is_budget_checked_too():
    _summary(_budget(spent=49_999.0, limit=50_000.0))
    v = g.gate_mcp_call("mcp__aws-api__call_aws", {"cli_command": M5_2XL})
    assert v is not None and v["decision"] == "ask" and "month to date" in v["reason"]


# ── the hook stays light ──────────────────────────────────────────────────────

def test_the_budget_lens_does_not_import_sqlalchemy(tmp_path):
    src = Path(g.__file__).resolve().parents[1]
    code = (
        "import sys, json\n"
        "from datetime import date, timedelta\n"
        "import finops.guard as g\n"
        "from finops.budget import summary as bs\n"
        "t = date.today(); s = t.replace(day=1)\n"
        "e = date(t.year + (t.month == 12), t.month % 12 + 1, 1) - timedelta(days=1)\n"
        "bs.write_summary([{'name': 'T', 'scope_type': 'total', 'scope_value': '*',"
        " 'period': 'monthly', 'period_start': s.isoformat(), 'period_end': e.isoformat(),"
        " 'spent': 49999.0, 'limit': 50000.0}], spend_through=t.isoformat())\n"
        f"v = g.gate_command({M5_2XL!r})\n"
        "print(json.dumps({'decision': v and v['decision'],"
        " 'sqlalchemy': 'sqlalchemy' in sys.modules}))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(src), "FINOPS_DATA_DIR": str(tmp_path),
           "HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}
    env.pop("FINOPS_PROFILE", None)
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, timeout=60, check=False)
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got == {"decision": "ask", "sqlalchemy": False}, out.stderr


# ── the summary is written by the budget checks ───────────────────────────────

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


def _snap(provider, account, service, amount, day=None):
    from finops.storage.snapshots import store_snapshot
    store_snapshot(provider, service, account, "us-east-1", day or _today(), amount)


def test_check_all_budgets_writes_the_summary_the_guard_reads(isolated_db):
    from finops.budget.enforcer import check_all_budgets, create_budget
    _snap("aws", "111", "Amazon Elastic Compute Cloud - Compute", 4_000.0)
    create_budget("AWS", "provider", 4_100.0, scope_value="aws")
    check_all_budgets()
    doc = bs.read_summary()
    assert doc is not None and bs.freshness(doc)["state"] == "fresh"
    assert doc["spend_through"] == _today().isoformat()
    (b,) = doc["budgets"]
    assert (b["name"], b["spent"], b["limit"]) == ("AWS", 4_000.0, 4_100.0)
    v = g.gate_command("aws ec2 run-instances --instance-type m5.4xlarge --count 2")
    assert v["decision"] == "ask" and "'AWS' budget" in v["reason"]


def test_creating_a_budget_refreshes_the_summary(isolated_db):
    from finops.budget.enforcer import create_budget, delete_budget
    b = create_budget("Total", "total", 1_000.0)
    assert [x["name"] for x in bs.read_summary()["budgets"]] == ["Total"]
    delete_budget(b["id"])
    assert bs.read_summary()["budgets"] == []


def test_set_budget_tools_block_at_pct_is_accepted(isolated_db):
    """The set_budget MCP tool passes block_at_pct; create_budget refused it."""
    from finops.budget.enforcer import create_budget, list_budgets
    create_budget("Total", "total", 1_000.0, block_at_pct=90.0)
    assert list_budgets()[0]["critical_at_pct"] == 90.0


def test_an_account_budget_sums_that_accounts_spend(isolated_db):
    from finops.budget.enforcer import check_all_budgets, create_budget
    _snap("aws", "111", "EC2", 300.0)
    _snap("aws", "222", "EC2", 700.0)
    create_budget("Acct 111", "account", 1_000.0, scope_value="111")
    (r,) = check_all_budgets()
    assert r["spent"] == 300.0


def test_a_commitment_that_breaks_the_budget_says_both():
    _summary(_budget(spent=79_900.0, limit=80_000.0))
    v = g.gate_command("aws savingsplans create-savings-plan "
                       "--savings-plan-offering-id x --commitment 5")
    assert v["decision"] == "ask"
    assert "cannot be cancelled once bought." in v["reason"]
    assert "'Cloud total' budget" in v["reason"]


# ── `nable budget refresh` ────────────────────────────────────────────────────

def test_budget_refresh_writes_the_summary_and_says_where(isolated_db, capsys):
    from finops.budget.enforcer import create_budget
    from finops.setup_wizard import main
    _snap("aws", "111", "EC2", 250.0)
    create_budget("Total", "total", 1_000.0)
    bs.summary_path().unlink()
    with pytest.raises(SystemExit) as e:
        main(["budget", "refresh", "--json"])
    assert e.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["budgets"][0]["spent"] == 250.0
    assert out["spend_through"] == _today().isoformat()
    assert bs.read_summary()["budgets"][0]["name"] == "Total"
