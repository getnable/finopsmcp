"""The org model in the guard: owners in asks, team scope, per-scope thresholds.

What has to stay true:
  - an ask or a deny on a priced change or a one-way door names the owner of
    what it touches, when the org model says; a proposed owner is "likely"
  - the team scope for budgets comes from the confirmed owner of the working
    directory's repo path, only when FINOPS_GUARD_TEAM is unset, and never
    from a proposal
  - a confirmed threshold for the team or environment replaces the auto
    threshold and the velocity cap, either way; a proposed one is ignored
  - an agent confirming, rejecting or setting an org fact is asked about;
    reading the model is not
  - an ordinary command never imports finops.org, and an org model that
    fails is a recorded fail-open with today's verdict
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
from finops import ai_budget, org
from finops.aws_prices import EC2_HOURLY
from finops.budget import summary as bs
from finops.org.cli import _who as human

M5_2XL = "aws ec2 run-instances --instance-type m5.2xlarge"
M5_2XL_X4 = "aws ec2 run-instances --instance-type m5.2xlarge --count 4"   # ~$1,121/mo
ACCT = "123456789012"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_VELOCITY_CAP_USD",
                "FINOPS_POLICY_LOOP_COUNT", "FINOPS_GUARD_TEAM", "FINOPS_GUARD_ACCOUNT",
                "FINOPS_GUARD_BUDGET_MAX_AGE_HOURS", "FINOPS_POLICY_ON_BUDGET_BREACH",
                "FINOPS_POLICY_FILE", "FINOPS_TAG_RULES", "FINOPS_REQUIRED_TAGS",
                "FINOPS_PROTECTED_TAGS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "no-accounts.yaml"))
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})


@pytest.fixture
def repo(tmp_path):
    """A git repo with infra/payments and infra/search; returns its root."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    for d in ("infra/payments", "infra/search"):
        (root / d).mkdir(parents=True)
    return root


def _in_repo(subject):
    """The org dir here is FINOPS_ORG_DIR, outside the repo: a repo path
    there names its repo (the fixture's has no remote, so its name)."""
    if subject.startswith("repo_path:") and "//" not in subject:
        return "repo_path:repo//" + subject.split(":", 1)[1]
    return subject


def confirmed(kind, subject, value):
    return org.set_fact(org.make_fact(kind, _in_repo(subject), value, source="human"),
                        human("maria"))


def proposed(kind, subject, value, confidence=0.8):
    return org.propose(org.make_fact(kind, _in_repo(subject), value, source="codeowners:x",
                                     confidence=confidence))


def payments_team():
    confirmed("team", "team:payments", {"name": "payments", "channel": "#payments-oncall"})


def _records():
    p = gl.ledger_path()
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def _month() -> tuple[str, str]:
    today = datetime.now().astimezone().date()
    start = today.replace(day=1)
    nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    return start.isoformat(), (nxt - timedelta(days=1)).isoformat()


def _team_budget(team, *, spent, limit):
    start, end = _month()
    b = {"name": team.title(), "scope_type": "team", "scope_value": team,
         "period": "monthly", "period_start": start, "period_end": end,
         "spent": spent, "limit": limit, "pct_used": round(spent / limit * 100, 1),
         "status": "ok"}
    bs.write_summary([b], spend_through=datetime.now().astimezone().date().isoformat(),
                     now=datetime.now(UTC) - timedelta(hours=1))


# ── owner citation ────────────────────────────────────────────────────────────

def test_a_one_way_door_names_the_confirmed_owner_of_the_repo_path(repo):
    payments_team()
    confirmed("owner", "repo_path:infra/payments", {"team": "payments"})
    v = g.gate_command("terraform destroy", cwd=str(repo / "infra" / "payments"))
    assert v["decision"] == "ask"
    assert v["reason"].endswith("Owned by payments (#payments-oncall).")
    assert v["owner"] == {"team": "payments", "channel": "#payments-oncall",
                          "confirmed": True}
    assert _records()[-1]["owner"]["team"] == "payments"


def test_a_proposed_owner_is_cited_as_likely(repo):
    proposed("owner", "repo_path:infra/search", {"team": "search", "channel": "#search"})
    v = g.gate_command("terraform destroy", cwd=str(repo / "infra" / "search"))
    assert v["reason"].endswith("Likely owned by search (#search), not confirmed.")
    assert v["owner"]["confirmed"] is False


def test_a_cd_before_terraform_names_the_directory_it_acts_on(repo):
    confirmed("owner", "repo_path:infra/search", {"team": "search"})
    v = g.gate_command("cd infra/search && terraform destroy", cwd=str(repo))
    assert "Owned by search." in v["reason"]


def test_the_account_in_an_arn_and_the_namespace_name_their_owners(repo):
    payments_team()
    confirmed("owner", f"aws_account:{ACCT}", {"team": "payments"})
    confirmed("owner", "k8s_namespace:checkout", {"team": "checkout", "channel": "#co"})
    v = g.gate_command("aws rds delete-db-instance --db-instance-identifier "
                       f"arn:aws:rds:us-east-1:{ACCT}:db:orders", cwd=str(repo))
    assert "Owned by payments (#payments-oncall)." in v["reason"]
    for cmd in ("kubectl delete deployment web -n checkout",
                "helm uninstall web --namespace=checkout"):
        v = g.gate_command(cmd, cwd=str(repo))
        assert "Owned by checkout (#co)." in v["reason"], cmd


def test_a_profile_maps_to_its_account_through_accounts_yaml(repo, tmp_path, monkeypatch):
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text(f"accounts:\n  - name: pay\n    account_id: '{ACCT}'\n"
                        "    profile: pay-prod\n")
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(accounts))
    confirmed("owner", f"aws_account:{ACCT}", {"team": "payments"})
    v = g.gate_command("aws ec2 terminate-instances --instance-ids i-1 --profile pay-prod",
                       cwd=str(repo))
    assert "Owned by payments." in v["reason"]
    v = g.gate_command("aws ec2 terminate-instances --instance-ids i-1 --profile other",
                       cwd=str(repo))
    assert "wned by" not in v["reason"]


def test_a_confirmed_owner_is_cited_before_a_proposed_one(repo):
    proposed("owner", "k8s_namespace:checkout", {"team": "guessers"})
    confirmed("owner", "repo_path:infra", {"team": "platform"})
    v = g.gate_command("kubectl delete ns checkout -n checkout",
                       cwd=str(repo / "infra" / "payments"))
    assert v["reason"].endswith("Owned by platform.")


def test_a_priced_ask_names_the_owner_and_a_silent_allow_stays_silent(repo):
    confirmed("owner", "repo_path:infra/payments", {"team": "payments"})
    where = str(repo / "infra" / "payments")
    v = g.gate_command(M5_2XL_X4, cwd=where)
    assert v["decision"] == "ask" and v["reason"].endswith("Owned by payments.")
    assert g.gate_command(M5_2XL, cwd=where) is None
    assert "owner" not in _records()[-1]


def test_no_org_model_means_no_citation(repo):
    v = g.gate_command("terraform destroy", cwd=str(repo / "infra" / "payments"))
    assert v["decision"] == "ask" and "wned by" not in v["reason"] and "owner" not in v


# ── team scope ────────────────────────────────────────────────────────────────

def test_a_confirmed_repo_owner_scopes_team_budgets(repo, monkeypatch):
    _team_budget("payments", spent=9_999.0, limit=10_000.0)
    where = str(repo / "infra" / "payments")
    assert g.gate_command(M5_2XL, cwd=where) is None
    confirmed("owner", "repo_path:infra/payments", {"team": "payments"})
    v = g.gate_command(M5_2XL, cwd=where)
    assert v["decision"] == "ask" and "'Payments' budget (team payments)" in v["reason"]
    # FINOPS_GUARD_TEAM names the process's team and wins.
    monkeypatch.setenv("FINOPS_GUARD_TEAM", "search")
    assert g.gate_command(M5_2XL, cwd=where) is None


def test_a_proposed_owner_never_picks_the_team(repo):
    _team_budget("payments", spent=9_999.0, limit=10_000.0)
    proposed("owner", "repo_path:infra/payments", {"team": "payments"}, confidence=0.99)
    assert g.gate_command(M5_2XL, cwd=str(repo / "infra" / "payments")) is None


# ── per-scope thresholds ──────────────────────────────────────────────────────

def test_a_confirmed_team_threshold_tightens_and_says_whose(repo):
    confirmed("owner", "repo_path:infra/payments", {"team": "payments"})
    confirmed("threshold", "team:payments", {"max_auto_monthly_usd": 100})
    where = str(repo / "infra" / "payments")
    v = g.gate_command(M5_2XL, cwd=where)
    assert v["decision"] == "ask"
    assert "over the $100 auto threshold for team payments (org model)" in v["reason"]
    # Elsewhere in the repo the default holds.
    assert g.gate_command(M5_2XL, cwd=str(repo / "infra" / "search")) is None


def test_a_confirmed_team_threshold_may_loosen(repo):
    where = str(repo / "infra" / "payments")
    assert g.gate_command(M5_2XL_X4, cwd=where)["decision"] == "ask"
    confirmed("owner", "repo_path:infra/payments", {"team": "payments"})
    confirmed("threshold", "team:payments", {"max_auto_monthly_usd": 5000})
    assert g.gate_command(M5_2XL_X4, cwd=where) is None


def test_a_warn_names_the_org_threshold(repo):
    confirmed("owner", "repo_path:infra/payments", {"team": "payments"})
    confirmed("threshold", "team:payments", {"max_auto_monthly_usd": 300})
    v = g.gate_command(M5_2XL, cwd=str(repo / "infra" / "payments"))
    assert v["decision"] == "warn"
    assert "of the $300/mo auto threshold for team payments (org model)" in v["reason"]


def test_a_proposed_threshold_is_ignored(repo):
    confirmed("owner", "repo_path:infra/payments", {"team": "payments"})
    proposed("threshold", "team:payments", {"max_auto_monthly_usd": 10})
    assert g.gate_command(M5_2XL, cwd=str(repo / "infra" / "payments")) is None
    proposed("threshold", "team:payments", {"max_auto_monthly_usd": 50_000})
    assert g.gate_command(M5_2XL_X4, cwd=str(repo / "infra" / "payments"))["decision"] == "ask"


def test_an_environment_threshold_needs_a_confirmed_environment(repo, monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD_ACCOUNT", ACCT)
    confirmed("threshold", "environment:prod", {"max_auto_monthly_usd": 50})
    proposed("environment", f"aws_account:{ACCT}", {"env": "prod"})
    assert g.gate_command(M5_2XL, cwd=str(repo)) is None
    confirmed("environment", f"aws_account:{ACCT}", {"env": "prod"})
    v = g.gate_command(M5_2XL, cwd=str(repo))
    assert v["decision"] == "ask" and "for environment prod (org model)" in v["reason"]


def test_a_confirmed_velocity_cap_for_the_team(repo):
    confirmed("owner", "repo_path:infra/payments", {"team": "payments"})
    confirmed("threshold", "team:payments", {"velocity_cap_usd": 400})
    where = str(repo / "infra" / "payments")
    assert g.gate_command(M5_2XL, cwd=where) is None
    v = g.gate_command(M5_2XL, cwd=where)
    assert v["decision"] == "ask" and v.get("history") == "velocity"
    assert "velocity cap per 60 minutes for team payments (org model)" in v["reason"]
    assert v["reason"].endswith("Owned by payments.")


# ── the org model's own confirmations ─────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "nable org confirm 3f2a9c1b7e --as maria",
    "finops org reject 3f2a9c1b7e",
    "nable org set owner --subject aws_account:123456789012 --team payments --as maria",
    "uvx --from finops-mcp nable org confirm abcd",
    "python -m finops.org.cli confirm abcd --as maria",
    "cd x && nable org --help; nable org set team --team payments",
])
def test_an_agent_deciding_an_org_fact_is_asked(cmd):
    v = g.gate_command(cmd)
    assert v is not None and v["decision"] == "ask", cmd
    assert v["action_type"] == "org_change"
    assert "deciding an org model fact for a person" in v["reason"]


@pytest.mark.parametrize("cmd", [
    "nable org status", "nable org status --json", "nable org review --kind owner",
    "nable org questions --limit 5", "nable org export --format json",
    "git commit -m 'nable org confirm is a human path'", "nable org init",
])
def test_reading_the_org_model_is_silent(cmd):
    assert g.gate_command(cmd) is None, cmd


# ── cost and failure ──────────────────────────────────────────────────────────

def test_ordinary_and_silent_commands_never_import_the_org_model(tmp_path):
    src = Path(g.__file__).resolve().parents[1]
    code = (
        "import sys, finops.guard as g\n"
        "for c in ['ls -la', 'git status', 'terraform plan', 'kubectl get pods',\n"
        "          'terraform apply']:\n"
        "    assert g.gate_command(c, record=False) is None, c\n"
        "print('finops.org' in sys.modules, 'finops.guard_org' in sys.modules)\n"
        "g.gate_command('terraform destroy', record=False)\n"
        "print('finops.org' in sys.modules)\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.update(PYTHONPATH=str(src), HOME=str(tmp_path), FINOPS_DATA_DIR=str(tmp_path),
               FINOPS_ORG_DIR=str(tmp_path / "org"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, cwd=str(tmp_path), timeout=60, check=False)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["False", "False", "True"]


def test_an_org_model_that_fails_is_a_recorded_fail_open(repo, monkeypatch):
    from finops import guard_org
    _team_budget("payments", spent=9_999.0, limit=10_000.0)
    confirmed("owner", "repo_path:infra/payments", {"team": "payments"})
    confirmed("threshold", "team:payments", {"max_auto_monthly_usd": 100})
    where = str(repo / "infra" / "payments")

    def boom(*_a, **_k):
        raise RuntimeError("bad org model")
    monkeypatch.setattr(guard_org, "load_model", boom)
    v = g.gate_command("terraform destroy", cwd=where)
    assert v["decision"] == "ask" and "wned by" not in v["reason"]
    recs = _records()
    assert {"decision": "fail_open", "error": "RuntimeError", "check": "org"}.items() \
        <= recs[-2].items()
    assert recs[-1]["decision"] == "ask"
    # A priced change: judged as today, no team budget and no org threshold.
    assert g.gate_command(M5_2XL, cwd=where) is None
    assert _records()[-2]["check"] == "org"


def test_a_failure_partway_judges_again_without_the_org_model(repo, monkeypatch):
    from finops import guard_org
    confirmed("owner", "repo_path:infra/payments", {"team": "payments"})
    confirmed("threshold", "team:payments", {"max_auto_monthly_usd": 100})

    def boom(*_a, **_k):
        raise ValueError("odd subject")
    monkeypatch.setattr(guard_org, "confirmed_envs", boom)
    # The team scope worked and the threshold did not: the verdict is today's,
    # not a half-org one.
    assert g.gate_command(M5_2XL, cwd=str(repo / "infra" / "payments")) is None
    assert _records()[-2]["check"] == "org"


def test_a_citation_failure_keeps_the_verdict(repo, monkeypatch):
    from finops import guard_org
    confirmed("owner", "repo_path:infra/payments", {"team": "payments"})
    monkeypatch.setattr(guard_org, "owner_words", lambda r: 1 / 0)
    v = g.gate_command("terraform destroy", cwd=str(repo / "infra" / "payments"))
    assert v["decision"] == "ask" and "wned by" not in v["reason"]
    assert _records()[-2]["check"] == "org"


def test_the_mcp_door_cites_the_owner_too(repo, monkeypatch):
    monkeypatch.chdir(repo / "infra" / "payments")
    confirmed("owner", f"aws_account:{ACCT}", {"team": "payments"})
    v = g.gate_mcp_call("mcp__aws-api__call_aws", {
        "cli_command": "aws rds delete-db-instance --db-instance-identifier "
                       f"arn:aws:rds:us-east-1:{ACCT}:db:orders"})
    assert v["decision"] == "ask" and v["reason"].endswith("Owned by payments.")
    confirmed("owner", "repo_path:infra/payments", {"team": "platform"})
    v = g.gate_mcp_call("mcp__aws-api__call_aws", {
        "cli_command": "aws ec2 terminate-instances --instance-ids i-1"})
    assert v["reason"].endswith("Owned by platform.")


# ── doctor ────────────────────────────────────────────────────────────────────

def test_org_status_reports_the_model_and_the_team_scope(repo, monkeypatch):
    s = g.org_status(str(repo))
    assert s["loaded"] is False and s["team"] is None
    confirmed("owner", "repo_path:infra/payments", {"team": "payments"})
    proposed("owner", "aws_account:111111111111", {"team": "search"})
    confirmed("threshold", "team:payments", {"max_auto_monthly_usd": 100})
    s = g.org_status(str(repo / "infra" / "payments"))
    assert s["loaded"] and s["exists"] and s["dir_source"] == "FINOPS_ORG_DIR"
    assert (s["confirmed"], s["proposed"]) == (2, 1)
    assert (s["team"], s["team_source"]) == ("payments",
                                             "org model, repo_path:repo//infra/payments")
    assert s["thresholds"]["max_auto_monthly_usd"] == 100
    monkeypatch.setenv("FINOPS_GUARD_TEAM", "search")
    assert g.org_status(str(repo))["team_source"] == "FINOPS_GUARD_TEAM"


def test_the_doctor_shows_the_org_model(repo, monkeypatch, capsys):
    confirmed("owner", "repo_path:infra/payments", {"team": "payments"})
    monkeypatch.chdir(repo / "infra" / "payments")
    d = g.doctor()
    assert d["org"]["team"] == "payments"
    assert any("as team payments" in c for c in d["covered"])
    from finops import setup_wizard
    setup_wizard._guard_doctor_org(d["org"])
    out = capsys.readouterr().out
    assert "Org model" in out and "1 confirmed, 0 proposed" in out
    assert "team scope: payments" in out


def test_a_repo_org_dir_is_read_from_the_agents_directory(repo, monkeypatch, tmp_path):
    monkeypatch.delenv("FINOPS_ORG_DIR")
    d = repo / "nable.org"
    d.mkdir()
    (d / "owners.yaml").write_text(
        "# nable org model v1\n- fact: owner\n  subject: {kind: repo_path, id: infra}\n"
        "  value: {team: platform}\n  source: human\n  status: confirmed\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    v = g.gate_command("terraform destroy", cwd=str(repo / "infra" / "search"))
    # A repo's own nable.org/ is somebody else's word until a person trusts it.
    assert v["reason"].endswith("Likely owned by platform, not confirmed.")
    org.trust(repo, human("maria"))
    v = g.gate_command("terraform destroy", cwd=str(repo / "infra" / "search"))
    assert v["reason"].endswith("Owned by platform.")


def test_the_hook_reads_a_cached_model_until_a_file_changes(repo, monkeypatch, tmp_path):
    from finops import guard_org
    confirmed("owner", "repo_path:infra", {"team": "platform"})
    where = str(repo / "infra")
    assert guard_org.load_model(where).owner_of("repo_path:repo//infra").team == "platform"
    assert guard_org._cache_path().is_file()
    real = org.load

    def no_reads(*_a, **_k):
        raise AssertionError("parsed the YAML again")
    monkeypatch.setattr(org, "load", no_reads)
    m = guard_org.load_model(where)
    assert m.owner_of("repo_path:repo//infra").team == "platform"
    assert m.dir_source == "FINOPS_ORG_DIR"
    monkeypatch.setattr(org, "load", real)
    confirmed("owner", "repo_path:infra", {"team": "search"})
    assert guard_org.load_model(where).owner_of("repo_path:repo//infra").team == "search"
    # A legacy file counts too.
    rules = tmp_path / "tag_rules.yaml"
    rules.write_text("team_aliases:\n  payments: [pay]\n")
    monkeypatch.setenv("FINOPS_TAG_RULES", str(rules))
    assert guard_org.load_model(where).owner_of("tag_value:pay").team == "payments"


def test_a_broken_cache_is_a_miss(repo):
    from finops import guard_org
    confirmed("owner", "repo_path:infra", {"team": "platform"})
    guard_org._cache_path().parent.mkdir(parents=True, exist_ok=True)
    guard_org._cache_path().write_text("{not json")
    assert guard_org.load_model(str(repo)).owner_of("repo_path:repo//infra").team == "platform"


def test_hourly_price_used_here_is_the_list_price():
    # The figures above assume these list prices; a price table change should
    # fail here, not in a threshold test.
    assert EC2_HOURLY["m5.2xlarge"] * 730 == pytest.approx(280.32, abs=0.5)
