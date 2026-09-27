"""Change freezes in the guard.

What has to stay true:
  - during a confirmed freeze covering what a command touches (the org, the
    verdict's team scope, a confirmed environment, an account it names),
    every priced change and every one-way door asks, or with mode deny is
    denied, whatever the thresholds say; the reason names the freeze's scope,
    its reason and its end time with the offset it was written in
  - outside the window, and for scopes it does not cover, nothing changes
  - a proposed freeze, or a confirmed one from a repo's nable.org/ nobody
    trusted, only asks: a guess may restrict, never stop outright
  - a freeze only tightens: an allowlist deny stays a deny; an ordinary
    command stays silent; the MCP door is frozen the same way
  - the ledger records which freeze made the call ask or deny
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

import finops.guard as g
import finops.guard_ledger as gl
from finops import ai_budget, guard_org, org
from finops.org.cli import _who as human

M5_LARGE = "aws ec2 run-instances --instance-type m5.large"     # ~$70/mo: silent today
ACCT = "123456789012"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_VELOCITY_CAP_USD",
                "FINOPS_POLICY_LOOP_COUNT", "FINOPS_GUARD_TEAM", "FINOPS_GUARD_ACCOUNT",
                "FINOPS_POLICY_ON_BUDGET_BREACH", "FINOPS_POLICY_FILE", "FINOPS_TAG_RULES",
                "FINOPS_REQUIRED_TAGS", "FINOPS_PROTECTED_TAGS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "no-accounts.yaml"))
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    for d in ("infra/payments", "infra/search"):
        (root / d).mkdir(parents=True)
    return root


def _window(hours_ago=1, hours_left=2):
    now = datetime.now(UTC)
    return ((now - timedelta(hours=hours_ago)).isoformat(),
            (now + timedelta(hours=hours_left)).isoformat())


def freeze(subject="org:org", *, mode="ask", reason="Black Friday", window=None, human_=True,
           dir=None):
    start, end = window or _window()
    f = org.make_fact("freeze", subject, {"start": start, "end": end, "reason": reason,
                                          "mode": mode}, source="human")
    if human_:
        return org.set_fact(f, human("maria"), dir=dir)
    return org.propose(f, dir=dir)


def confirmed(kind, subject, value, dir=None):
    if subject.startswith("repo_path:") and "//" not in subject and dir is None:
        subject = "repo_path:repo//" + subject.split(":", 1)[1]
    return org.set_fact(org.make_fact(kind, subject, value, source="human"), human("maria"),
                        dir=dir)


def _records():
    p = gl.ledger_path()
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


# ── in and out of the window ──────────────────────────────────────────────────

def test_a_cheap_launch_during_an_org_freeze_asks_and_says_until_when():
    assert g.gate_command(M5_LARGE) is None
    now = datetime.now(UTC).replace(microsecond=0)
    end = (now + timedelta(hours=5)).astimezone(
        datetime.fromisoformat("2026-01-01T00:00:00-05:00").tzinfo)
    freeze(window=((now - timedelta(hours=1)).isoformat(), end.isoformat()))
    v = g.gate_command(M5_LARGE)
    assert v["decision"] == "ask"
    assert v["reason"].startswith("nable guard: A change freeze is in force for the whole org "
                                  f"until {end.strftime('%Y-%m-%d %H:%M')} (UTC-05:00): "
                                  "Black Friday.")
    assert "m5.large" in v["reason"] and "Confirm to proceed." in v["reason"]
    assert v["freeze"]["mode"] == "ask" and v["freeze"]["sure"] is True


def test_before_and_after_the_window_nothing_changes():
    freeze(window=_window(hours_ago=-1, hours_left=3))          # starts in an hour
    freeze(window=_window(hours_ago=5, hours_left=-1), reason="over")   # ended an hour ago
    assert g.gate_command(M5_LARGE) is None


def test_the_window_edges_are_judged_in_utc(monkeypatch):
    freeze(window=("2026-11-27T00:00:00-05:00", "2026-12-01T00:00:00-05:00"), mode="deny")
    monkeypatch.setattr(guard_org, "_now", lambda: datetime(2026, 11, 27, 4, 59, tzinfo=UTC))
    assert g.gate_command(M5_LARGE) is None
    monkeypatch.setattr(guard_org, "_now", lambda: datetime(2026, 11, 27, 5, 0, tzinfo=UTC))
    assert g.gate_command(M5_LARGE)["decision"] == "deny"
    monkeypatch.setattr(guard_org, "_now", lambda: datetime(2026, 12, 1, 4, 59, tzinfo=UTC))
    assert g.gate_command("terraform destroy")["decision"] == "deny"
    monkeypatch.setattr(guard_org, "_now", lambda: datetime(2026, 12, 1, 5, 0, tzinfo=UTC))
    assert g.gate_command("terraform destroy")["decision"] == "ask"      # a door as always


def test_a_deny_mode_freeze_denies_whatever_the_threshold_says(repo):
    confirmed("owner", "repo_path:infra/payments", {"team": "payments"})
    confirmed("threshold", "team:payments", {"max_auto_monthly_usd": 100_000})
    freeze("team:payments", mode="deny", reason="quarter close")
    v = g.gate_command(M5_LARGE, cwd=str(repo / "infra" / "payments"))
    assert v["decision"] == "deny"
    assert "A change freeze is in force for team payments" in v["reason"]
    assert "It is denied until the freeze ends, by the org's policy" in v["reason"]
    assert v["reason"].endswith("Owned by payments.")
    # Another team's directory is not frozen.
    assert g.gate_command(M5_LARGE, cwd=str(repo / "infra" / "search")) is None


def test_one_way_doors_are_frozen_too():
    freeze(mode="deny")
    v = g.gate_command("kubectl delete ns payments")
    assert v["decision"] == "deny" and "freeze" in v["reason"]
    assert "would delete Kubernetes resources" in v["reason"]


def test_ordinary_and_unpriced_commands_are_not_frozen():
    freeze(mode="deny")
    for cmd in ("ls -la", "terraform plan", "kubectl get pods", "terraform apply"):
        assert g.gate_command(cmd) is None, cmd


def test_a_freeze_never_loosens_a_deny(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "tag_fix")
    freeze(mode="ask")
    v = g.gate_command(M5_LARGE)
    assert v["decision"] == "deny"
    assert "freeze" in v["reason"] and "allowlist" in v["reason"]


# ── scopes ────────────────────────────────────────────────────────────────────

def test_an_environment_freeze_needs_a_confirmed_environment(monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD_ACCOUNT", ACCT)
    freeze("environment:prod", mode="deny")
    assert g.gate_command(M5_LARGE) is None
    org.propose(org.make_fact("environment", f"aws_account:{ACCT}", {"env": "prod"},
                              source="agent:x"))
    assert g.gate_command(M5_LARGE) is None          # a guessed environment scopes nothing
    confirmed("environment", f"aws_account:{ACCT}", {"env": "prod"})
    v = g.gate_command(M5_LARGE)
    assert v["decision"] == "deny" and "for environment prod" in v["reason"]


def test_an_account_freeze_covers_commands_that_name_the_account():
    freeze(f"aws_account:{ACCT}", mode="deny")
    assert g.gate_command(M5_LARGE) is None
    v = g.gate_command(f"aws ec2 terminate-instances --instance-ids "
                       f"arn:aws:ec2:us-east-1:{ACCT}:instance/i-1")
    assert v["decision"] == "deny" and f"for account {ACCT}" in v["reason"]


def test_a_team_freeze_follows_finops_guard_team(monkeypatch):
    freeze("team:payments")
    assert g.gate_command(M5_LARGE) is None
    monkeypatch.setenv("FINOPS_GUARD_TEAM", "payments")
    assert g.gate_command(M5_LARGE)["decision"] == "ask"


# ── how sure ──────────────────────────────────────────────────────────────────

def test_a_proposed_freeze_only_asks():
    freeze(mode="deny", human_=False)
    v = g.gate_command(M5_LARGE)
    assert v["decision"] == "ask"
    assert "(proposed, not confirmed, so it only asks)" in v["reason"]
    assert v["freeze"]["sure"] is False
    # A rejected one is gone.
    org.reject(v["freeze"]["key"], human("maria"))
    assert g.gate_command(M5_LARGE) is None


def test_a_freeze_from_an_untrusted_repo_only_asks(repo, monkeypatch):
    monkeypatch.delenv("FINOPS_ORG_DIR")
    d = repo / "nable.org"
    d.mkdir()
    freeze(mode="deny", reason="their freeze", dir=d)
    where = str(repo / "infra")
    v = g.gate_command(M5_LARGE, cwd=where)
    assert v["decision"] == "ask"
    assert "not trusted, so it only asks" in v["reason"]
    assert v["freeze"]["file"].endswith("nable.org/freezes.yaml")
    org.trust(repo, human("maria"))
    assert g.gate_command(M5_LARGE, cwd=where)["decision"] == "deny"


def test_an_untrusted_repo_cannot_hide_a_confirmed_freeze(repo, monkeypatch, tmp_path):
    # The person's own deny freeze lives in the data dir's model; a cloned
    # repo ships an ask freeze with the same start, which outranks it in the
    # slot. The deny still answers.
    monkeypatch.delenv("FINOPS_ORG_DIR")
    from finops.org.store import _data_dir
    window = _window()
    freeze(mode="deny", reason="mine", window=window, dir=_data_dir() / "org")
    d = repo / "nable.org"
    d.mkdir()
    freeze(mode="ask", reason="theirs", window=window, dir=d)
    v = g.gate_command(M5_LARGE, cwd=str(repo / "infra"))
    assert v["decision"] == "deny" and ": mine." in v["reason"]


# ── the other doors, and the record ──────────────────────────────────────────

def test_the_mcp_door_is_frozen_too():
    freeze(mode="deny")
    v = g.gate_mcp_call("mcp__aws__call_aws",
                        {"cli_command": "aws ec2 run-instances --instance-type m5.large"})
    assert v is not None and v["decision"] == "deny" and "freeze" in v["reason"]


def test_the_ledger_records_the_freeze():
    freeze(mode="deny", reason="Black Friday")
    g.gate_command(M5_LARGE)
    rec = _records()[-1]
    assert rec["decision"] == "deny" and rec["outcome"] == "not_run"
    assert rec["freeze"]["mode"] == "deny" and rec["freeze"]["reason"] == "Black Friday"
    assert rec["freeze"]["subject"] == "org:org" and rec["freeze"]["sure"] is True


def test_a_broken_freeze_lookup_is_a_recorded_fail_open(monkeypatch):
    freeze(mode="deny")

    def boom(*_a, **_k):
        raise RuntimeError("bad freeze")
    monkeypatch.setattr(guard_org, "freeze", boom)
    assert g.gate_command(M5_LARGE) is None
    assert {"decision": "fail_open", "check": "org"}.items() <= _records()[-2].items()


def test_the_guard_names_the_org_dir_as_the_org_store_does():
    from finops.org import store
    assert g._ORG_DIR_NAME == store.ORG_DIR_NAME
    assert g._in_org_dir("/repo/nable.org/freezes.yaml")
    assert not g._in_org_dir("/repo/not-nable.org.d/freezes.yaml")
    assert not g._in_org_dir("/home/u/.finops/org/freezes.yaml")
