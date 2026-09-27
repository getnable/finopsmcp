"""The org model's binding invariants, one regression test per way the review
found to break them:

  - agents only propose; confirming and rejecting take the human path;
  - a proposal never overwrites, redirects or weakens a confirmed fact;
  - a guess may restrict, never enable;
  - a repo someone else wrote (a cloned nable.org/) is not the person's
    word until they trust it, and may only tighten a threshold;
  - rewriting a file never drops what a person wrote in it.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from finops import guard_org, org
from finops.org import cli, store
from finops.org.cli import _who as human
from finops.org.model import local_today

TODAY = local_today()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A clean HOME, no legacy files or org env, a data dir of its own, and a
    work dir outside any repo. The org dir is whatever resolve_dir picks."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for var in ("FINOPS_ORG_DIR", "FINOPS_TAG_RULES", "FINOPS_REQUIRED_TAGS",
                "FINOPS_PROTECTED_TAGS", "FINOPS_PROFILE", "DATABASE_URL",
                "FINOPS_GUARD_TEAM", "FINOPS_GUARD_ACCOUNT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_VELOCITY_CAP_USD", "FINOPS_POLICY_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "no-accounts.yaml"))
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "no-such.db"))
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(store, "_data_dir", lambda: tmp_path / "data")
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setattr(cli, "_is_tty", lambda: False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return tmp_path


@pytest.fixture
def odir(home, monkeypatch):
    d = home / "orgdir"
    monkeypatch.setenv("FINOPS_ORG_DIR", str(d))
    return d


def mkrepo(root: Path, remote: str | None = None) -> Path:
    (root / ".git").mkdir(parents=True)
    if remote:
        (root / ".git" / "config").write_text(
            f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {remote}\n'
            '\tfetch = +refs/heads/*:refs/remotes/origin/*\n')
    return root


def H(kind, subject, value, d=None, **kw):
    """A human fact, through the human path."""
    return org.set_fact(org.make_fact(kind, subject, value, source="human", **kw),
                        human("@maria"), d)


def P(kind, subject, value, d=None, *, source="codeowners:x", confidence=0.5):
    return org.propose(org.make_fact(kind, subject, value, source=source,
                                     confidence=confidence), d)


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


# ── 1. a proposal never redirects or downgrades a confirmed owner ─────────────

def test_a_proposed_team_alias_does_not_redirect_a_confirmed_owner(home):
    """r1: a proposed team fact "growth, also called payments" moved the
    confirmed repo owner (still reported confirmed) to growth, and with it
    the guard's team scope and a $5,000 threshold instead of the org's $50."""
    repo = mkrepo(home / "repo")
    d = repo / "nable.org"
    H("owner", "repo_path:.", {"team": "payments", "channel": "#payments-oncall"}, d)
    H("threshold", "team:growth", {"max_auto_monthly_usd": 5000.0}, d)
    H("threshold", "org:org", {"max_auto_monthly_usd": 50.0}, d)
    assert P("team", "team:growth", {"name": "growth", "aliases": ["payments"],
                                     "channel": "#growth"}, d,
             source="agent:guess", confidence=0.2) == "added"
    m = org.load(d)
    r = m.owner_of("repo_path:.")
    assert (r.team, r.channel, r.confirmed) == ("payments", "#payments-oncall", True)
    team = guard_org.team_scope(m, str(repo))
    assert team[0] == "payments"
    t = guard_org.thresholds(m, team[0], [])
    assert (t["max_auto_monthly_usd"], t["scope"]["max_auto_monthly_usd"]) == (50.0, "org:org")
    assert m.threshold_for("payments")["max_auto_monthly_usd"] == 50.0


def test_a_proposed_tag_alias_does_not_downgrade_a_confirmed_owner(home):
    """r7: a tags-adapter alias payments-svc -> payments made the confirmed
    owner unconfirmed, dropped the team scope, and the team's $10 threshold
    gave way to the org's $500 (threshold_for with FINOPS_GUARD_TEAM too)."""
    repo = mkrepo(home / "repo")
    d = repo / "nable.org"
    H("owner", "repo_path:.", {"team": "payments-svc"}, d)
    H("threshold", "team:payments-svc", {"max_auto_monthly_usd": 10.0}, d)
    H("threshold", "org:org", {"max_auto_monthly_usd": 500.0}, d)
    P("tag_alias", "tag_value:payments-svc", {"canonical_key": "team",
                                              "canonical_value": "payments"}, d,
      source="tags:attributed_costs", confidence=0.75)
    m = org.load(d)
    assert m.owner_of("repo_path:.").confirmed is True
    team = guard_org.team_scope(m, str(repo))
    assert team[0] == "payments-svc"
    assert guard_org.thresholds(m, team[0], [])["max_auto_monthly_usd"] == 10.0
    assert m.threshold_for("payments-svc")["max_auto_monthly_usd"] == 10.0
    # The same holds for a tag the resource carries.
    H("tag_key", "org:org", {"canonical": "team", "keys": ["team"]}, d)
    r = org.load(d).team_for_tags({"team": "payments-svc"})
    assert (r.team, r.confirmed) == ("payments-svc", True)


def test_a_deeper_proposal_does_not_shadow_a_confirmed_parent(home):
    """r15: a CODEOWNERS proposal for infra/payments hid the confirmed owner
    of infra/, so the guard lost the team scope and its $10 threshold."""
    repo = mkrepo(home / "repo")
    (repo / "infra" / "payments").mkdir(parents=True)
    d = repo / "nable.org"
    H("owner", "repo_path:infra", {"team": "payments"}, d)
    H("threshold", "team:payments", {"max_auto_monthly_usd": 10.0}, d)
    H("threshold", "org:org", {"max_auto_monthly_usd": 500.0}, d)
    P("owner", "repo_path:infra/payments", {"team": "payments-infra"}, d,
      source="codeowners:.github/CODEOWNERS:3", confidence=0.85)
    m = org.load(d)
    cwd = str(repo / "infra" / "payments")
    team = guard_org.team_scope(m, cwd)
    assert team[0] == "payments"
    assert guard_org.thresholds(m, team[0], [])["max_auto_monthly_usd"] == 10.0


def test_a_proposed_alias_does_not_reroute_a_confirmed_ticket(odir):
    """r2: tickets about a confirmed payments account were labelled
    team:growth and assigned to growth's person, on a proposal."""
    from finops.integrations import ticketing as t
    H("owner", "aws_account:111111111111", {"team": "payments",
                                            "channel": "#payments-oncall"})
    H("team", "team:growth", {"name": "growth", "channel": "#growth",
                              "people": ["github:alice"]})
    rec = {"account_id": "111111111111", "instance_id": "i-1"}
    before = t._route(rec)
    P("team", "team:growth", {"name": "growth", "aliases": ["payments"]},
      source="agent:x", confidence=0.1)
    after = t._route(rec)
    assert (after.team, after.channel, after.confirmed) == \
        (before.team, before.channel, True) == ("payments", "#payments-oncall", True)
    assert t._routed_labels(["finops"], after) == ["finops", "team:payments"]
    assert t._assignee(after, "github") is None


def test_a_proposed_team_fact_does_not_lend_its_channel_to_a_confirmed_owner(odir):
    H("owner", "aws_account:111111111111", {"team": "payments"})
    P("team", "team:payments", {"name": "payments", "channel": "#attacker",
                                "people": ["github:mallory"]}, source="agent:x")
    r = org.load().owner_of("aws_account:111111111111")
    assert (r.team, r.confirmed, r.channel, r.people) == ("payments", True, None, [])


# ── 2. a cloned repo's nable.org/ is not the person's model ───────────────────

POLICY = """# nable org model v1
- fact: threshold
  subject: {kind: org, id: org}
  value: {max_auto_monthly_usd: %s}
  source: human
  status: confirmed
  confirmed_by: "@stranger"
  confirmed_at: 2026-09-01
"""


def test_a_cloned_repo_dir_cannot_raise_the_users_threshold(home):
    """r8: a repo that ships nable.org/ replaced the user's own model: their
    confirmed $50 cap became the repo author's $100,000."""
    H("threshold", "org:org", {"max_auto_monthly_usd": 50.0})     # the data dir
    repo = mkrepo(home / "repo", "git@github.com:someone/else.git")
    (repo / "nable.org").mkdir()
    (repo / "nable.org" / "policy.yaml").write_text(POLICY % 100000)
    m = guard_org.load_model(str(repo))
    assert m.dir_source == "repo" and m.dir == repo / "nable.org"
    assert [layer.source for layer in m.layers] == ["repo", "data_dir"]
    t = guard_org.thresholds(m, None, [])
    assert t["max_auto_monthly_usd"] == 50.0
    assert t["files"]["max_auto_monthly_usd"] == str(home / "data" / "org" / "policy.yaml")
    # Trusted by the person, it is theirs, and says what it says.
    org.trust(repo, human("@me"))
    t = guard_org.thresholds(guard_org.load_model(str(repo)), None, [])
    assert t["max_auto_monthly_usd"] == 100000.0
    assert t["files"]["max_auto_monthly_usd"] == str(repo / "nable.org" / "policy.yaml")
    # A different remote at the same path is not the repo they trusted.
    (repo / ".git" / "config").write_text('[remote "origin"]\n\turl = https://evil.example/x\n')
    assert guard_org.thresholds(guard_org.load_model(str(repo)), None, []) \
        ["max_auto_monthly_usd"] == 50.0


def test_an_untrusted_repo_threshold_may_only_tighten(home):
    repo = mkrepo(home / "repo")
    (repo / "nable.org").mkdir()
    # No threshold of the user's own: the policy's $500 is the ceiling.
    (repo / "nable.org" / "policy.yaml").write_text(POLICY % 100000)
    assert guard_org.thresholds(guard_org.load_model(str(repo)), None, []) == {}
    (repo / "nable.org" / "policy.yaml").write_text(POLICY % 25)
    t = guard_org.thresholds(guard_org.load_model(str(repo)), None, [])
    assert t["max_auto_monthly_usd"] == 25.0
    assert guard_org.whose(t, "max_auto_monthly_usd") == \
        f"for the org (in {repo / 'nable.org' / 'policy.yaml'})"


def test_an_untrusted_repo_owner_is_likely_and_never_picks_the_team(home):
    repo = mkrepo(home / "repo")
    (repo / "infra").mkdir()
    (repo / "nable.org").mkdir()
    (repo / "nable.org" / "owners.yaml").write_text(
        "# nable org model v1\n- fact: owner\n  subject: {kind: repo_path, id: infra}\n"
        "  value: {team: stranger}\n  source: human\n  status: confirmed\n")
    m = guard_org.load_model(str(repo / "infra"))
    assert guard_org.team_scope(m, str(repo / "infra")) == (None, None)
    r = guard_org.owner(m, guard_org.subjects("terraform destroy", str(repo / "infra"), m))
    assert (r.team, r.confirmed) == ("stranger", False)
    assert guard_org.owner_words(r) == "Likely owned by stranger, not confirmed."
    org.trust(repo, human("@me"))
    m = guard_org.load_model(str(repo / "infra"))
    assert guard_org.team_scope(m, str(repo / "infra"))[0] == "stranger"


def test_trust_is_a_human_decision_and_init_here_records_it(home, monkeypatch, capsys):
    repo = mkrepo(home / "repo", "https://github.com/acme/payments.git")
    monkeypatch.chdir(repo)
    with pytest.raises(org.OrgError):
        org.trust(repo, "@me")
    code, _, err = run(capsys, "trust", "--here")
    assert code == 2 and "--as" in err
    code, out, _ = run(capsys, "init", "--here", "--no-adapters", "--as", "@me")
    assert code == 0 and "trusted" in out
    rows = org.trusted_repos()
    assert rows == [{"root": str(repo.resolve()),
                     "remote": "https://github.com/acme/payments.git", "by": "@me",
                     "at": TODAY.isoformat()}]
    assert org.is_trusted(repo)
    code, out, _ = run(capsys, "trust")
    assert code == 0 and ": trusted" in out
    code, out, _ = run(capsys, "trust", "--here", "--revoke", "--as", "@me")
    assert code == 0 and not org.is_trusted(repo)
    # The trust file lives beside the data dir's org model.
    assert store.trust_path() == home / "data" / "org" / "trusted-repos.json"


def test_init_without_here_never_writes_into_a_repos_org_dir(home, monkeypatch, capsys):
    """Default init writes to the data dir: the user's legacy facts are not
    the cloned repo's to commit."""
    rules = home / "tag_rules.yaml"
    rules.write_text("team_aliases:\n  payments: [pay]\n")
    monkeypatch.setenv("FINOPS_TAG_RULES", str(rules))
    repo = mkrepo(home / "repo")
    (repo / "nable.org").mkdir()
    monkeypatch.chdir(repo)
    code, out, _ = run(capsys, "init", "--no-adapters")
    assert code == 0 and str(home / "data" / "org") in out
    assert list((repo / "nable.org").iterdir()) == []
    assert "pay" in (home / "data" / "org" / "teams.yaml").read_text()


def test_the_repo_model_is_read_over_the_data_dirs(home, monkeypatch):
    H("owner", "aws_account:111111111111", {"team": "payments"})    # data dir
    repo = mkrepo(home / "repo")
    (repo / "nable.org").mkdir()
    monkeypatch.chdir(repo)
    H("owner", "aws_account:222222222222", {"team": "search"})      # repo
    m = org.load()
    assert m.dir_source == "repo"
    assert m.owner_of("aws_account:111111111111").team == "payments"
    assert m.owner_of("aws_account:222222222222").team == "search"
    # A fact is decided where it lives.
    f = org.make_fact("owner", "aws_account:333333333333", {"team": "x"}, source="x:y")
    org.propose(f, home / "data" / "org")
    org.reject(f.key, human("@maria"))
    assert "333333333333" in (home / "data" / "org" / "owners.yaml").read_text()
    assert "333333333333" not in (repo / "nable.org" / "owners.yaml").read_text()
    assert org.load().find(f.key)[0].status == "rejected"


# ── 3. a bulk answer decides only what it showed ──────────────────────────────

def test_a_bulk_command_is_bound_to_the_set_it_showed(odir, capsys):
    """r19: the printed `--owner-bulk payments` re-derived its set when run,
    so proposals added after the person read the question (an agent's team
    fact with its own channel and people, an alias) were confirmed with it."""
    for a in ("111111111111", "222222222222"):
        P("owner", f"aws_account:{a}", {"team": "payments", "channel": "#pay"},
          confidence=0.8)
    q = org.questions(include_spend=False)[0]
    assert q.kind == "bulk" and q.command.endswith(f"--owner-bulk payments@{q.digest}")
    assert all("channel #pay" in i for i in q.items) and len(q.items) == 2
    # After the person read it, an agent proposes more; none of it may ride along.
    P("tag_alias", "tag_value:growth", {"canonical_key": "team", "canonical_value": "payments"},
      source="agent:x", confidence=0.1)
    P("team", "team:payments", {"name": "payments", "channel": "#attacker",
                                "people": ["github:mallory"]}, source="agent:x",
      confidence=0.1)
    assert org.bulk_facts(org.load(), owner="payments") and \
        {f.source for f in org.bulk_facts(org.load(), owner="payments")} == {"codeowners:x"}
    # A proposal a person has not seen joins the group: the old command is refused.
    P("owner", "aws_account:333333333333", {"team": "payments"}, confidence=0.8)
    argv = q.command.split()[2:] + ["--as", "@maria"]
    code, _, err = run(capsys, *argv)
    assert code == 1 and "changed since" in err and "333333333333" in err
    assert all(f.status == "proposed" for f in org.load().facts)
    # The refusal prints the command for the set as it is now.
    new = next(line for line in err.splitlines() if "nable org confirm --owner-bulk" in line)
    code, _, _ = run(capsys, *new.split("nable org ", 1)[1].split(), "--as", "@maria")
    assert code == 0
    m = org.load()
    assert {f.subject.id for f in m.facts if f.confirmed} == \
        {"111111111111", "222222222222", "333333333333"}
    assert m.team_for_tags({"team": "growth"}).confirmed is False
    # Without a digest there is nothing to bind to.
    code, _, err = run(capsys, "confirm", "--owner-bulk", "payments", "--as", "@maria")
    assert code == 1


def test_agent_team_and_alias_facts_and_thresholds_are_asked_one_by_one(odir):
    for a in ("111111111111", "222222222222"):
        P("owner", f"aws_account:{a}", {"team": "a"})
    P("team", "team:a", {"name": "a", "channel": "#a"}, source="agent:x")
    P("tag_alias", "tag_value:aa", {"canonical_key": "team", "canonical_value": "a"},
      source="agent:x")
    P("team", "team:b", {"name": "b"}, source="codeowners:x")
    P("tag_alias", "tag_value:bb", {"canonical_key": "team", "canonical_value": "b"},
      source="tags:x")
    for t in ("team:a", "org:org"):
        P("threshold", t, {"max_auto_monthly_usd": 10.0}, source="inference:x")
    P("threshold", "team:b", {"max_auto_monthly_usd": 10.0}, source="inference:x")
    qs = org.questions(include_spend=False, limit=50)
    bulk = {q.group: sorted(q.subjects) for q in qs if q.kind == "bulk"}
    assert bulk == {"owner:a": ["aws_account:111111111111", "aws_account:222222222222"],
                    "owner:b": ["tag_value:bb", "team:b"]}
    single = {q.subject for q in qs if q.kind == "confirm"}
    assert single == {"team:a", "tag_value:aa", "team:b", "org:org"}
    th = [q for q in qs if q.fact and q.fact["fact"] == "threshold"]
    assert len(th) == 3 and {q.default for q in th} == {"n"}


# ── 4. the Python API is not a way around the terminal ────────────────────────

def test_the_python_api_takes_a_human_decision_not_a_name(odir):
    """r12: an agent could run `python -c "org.confirm(KEY, '@maria')"`."""
    f = org.make_fact("owner", "aws_account:111111111111", {"team": "a"}, source="x:y")
    org.propose(f)
    for call in (lambda: org.confirm(f.key, "@maria"),
                 lambda: org.reject(f.key, "@maria"),
                 lambda: org.confirm_many([f.key], "@maria"),
                 lambda: org.reject_many([f.key], "@maria"),
                 lambda: org.set_fact(f, "@maria")):
        with pytest.raises(org.OrgError, match="person"):
            call()
    with pytest.raises(org.OrgError):
        org.HumanDecision("@maria", "forged")
    assert org.load().facts[0].status == "proposed"
    # Legacy import is not a decision of its own and still works.
    assert org.import_legacy() == 0
    assert org.confirm(f.key, human("@maria")).confirmed_by == "@maria"


# ── 5. a repo path belongs to a repo ──────────────────────────────────────────

def test_a_data_dir_repo_path_names_its_repo(home, monkeypatch, capsys):
    """r16: a confirmed owner of repo A's infra/, in the data dir, was the
    owner of the infra/ directory of every repo on the machine."""
    a = mkrepo(home / "a", "git@github.com:acme/payments.git")
    b = mkrepo(home / "b", "https://github.com/acme/search")
    for r in (a, b):
        (r / "infra").mkdir()
    monkeypatch.chdir(a)
    code, out, _ = run(capsys, "set", "owner", "--subject", "repo_path:infra",
                       "--team", "payments", "--as", "@maria")
    assert code == 0 and "repo_path:github.com/acme/payments//infra" in out
    m = guard_org.load_model(str(b / "infra"))
    assert m.dir_source == "data_dir"
    assert guard_org.team_scope(m, str(b / "infra")) == (None, None)
    assert guard_org.team_scope(m, str(a / "infra"))[0] == "payments"
    assert org.repo_subject(a / "infra") == "repo_path:github.com/acme/payments//infra"


def test_a_bare_data_dir_repo_path_belongs_to_no_repo(home, monkeypatch, capsys):
    a = mkrepo(home / "a")
    (a / "infra").mkdir()
    H("owner", "repo_path:infra", {"team": "payments"})     # an old, bare fact
    m = guard_org.load_model(str(a / "infra"))
    assert guard_org.team_scope(m, str(a / "infra")) == (None, None)
    r = m.owner_of(org.repo_subject(a / "infra"), strict=True)
    assert (r.team, r.confirmed) == ("payments", False)
    _, out, _ = run(capsys, "status")
    assert "repo paths that name no repo: 1" in out and "repo_path:<repo>//<path>" in out


def test_adapters_qualify_repo_paths_outside_the_repo(home, monkeypatch):
    repo = mkrepo(home / "platform")
    (repo / "CODEOWNERS").write_text("/infra/ @acme/payments\n")
    (repo / "infra").mkdir()
    (repo / "infra" / "main.tf").write_text('resource "aws_s3_bucket" "b" {}\n')
    monkeypatch.chdir(repo)
    from finops.org.adapters.data import CostData
    org.run_adapters(data=CostData(), only=["codeowners"])
    assert [str(f.subject) for f in org.load().facts] == ["repo_path:platform//infra"]
    here = repo / "nable.org"
    org.run_adapters(here, data=CostData(), only=["codeowners"])
    assert [str(f.subject) for f in org.load(here).facts] == ["repo_path:infra"]


# ── 6. a recheck clears ───────────────────────────────────────────────────────

def test_confirming_a_stale_fact_moves_its_review_forward(odir):
    """r3: a recheck answered yes stayed stale, and was asked again forever."""
    f = org.set_fact(org.Fact.from_dict({
        "fact": "owner", "subject": "aws_account:111111111111", "value": {"team": "payments"},
        "source": "human", "status": "confirmed", "review_after": "2026-01-01"}),
        human("@maria"))
    qs = org.questions(include_spend=False)
    assert [q.kind for q in qs] == ["recheck"]
    org.confirm(qs[0].key, human("@maria"))
    m = org.load()
    assert m.stale() == []
    # No interval to repeat (confirmed after its own review date): 180 days.
    assert m.facts[0].review_after == (TODAY + timedelta(days=180)).isoformat()
    assert org.questions(include_spend=False) == []
    # A fact with its own interval keeps it.
    g = org.Fact.from_dict({"fact": "owner", "subject": "aws_account:222222222222",
                            "value": {"team": "search"}, "source": "human",
                            "status": "confirmed", "confirmed_at": "2025-01-01",
                            "review_after": "2025-04-01"})
    odir.joinpath("owners.yaml").write_text(
        odir.joinpath("owners.yaml").read_text()
        + yaml.safe_dump([g.to_dict()], sort_keys=False))
    org.confirm(g.key, human("@maria"))
    assert org.load().find(g.key)[0].review_after == (TODAY + timedelta(days=90)).isoformat()
    assert f.key


# ── 7. a rewrite keeps what a person wrote ────────────────────────────────────

def test_a_rewrite_keeps_extra_fields_and_entry_comments(odir):
    """r4: a proposal rewrote owners.yaml and dropped a person's `ticket` and
    `note` fields, a subject's extra key, and the comment above the entry."""
    odir.mkdir()
    (odir / "owners.yaml").write_text("""# nable org model v1
# Owners. Reviewed by FinOps every quarter.

# --- Payments: agreed with @maria in FIN-123, do not change without her ---
- fact: owner
  subject: {kind: aws_account, id: "333333333333", note: "prod billing"}
  value: {team: payments}
  source: human
  status: confirmed
  confirmed_by: "@maria"
  confirmed_at: 2026-09-01
  ticket: FIN-123
  note: "escalate to CFO before changing"
""")
    org.propose(org.make_fact("owner", "aws_account:111111111111", {"team": "growth"},
                              source="agent:x"))
    text = (odir / "owners.yaml").read_text()
    lines = text.splitlines()
    assert lines[:2] == ["# nable org model v1", "# Owners. Reviewed by FinOps every quarter."]
    # The comment moved with its entry (now second, after the sort).
    i = lines.index("# --- Payments: agreed with @maria in FIN-123, do not change without her ---")
    assert "333333333333" in lines[i + 2]
    data = yaml.safe_load(text)
    kept = next(e for e in data if e["subject"]["id"] == "333333333333")
    assert kept["ticket"] == "FIN-123" and kept["note"] == "escalate to CFO before changing"
    assert kept["subject"]["note"] == "prod billing"
    f = org.load().find(org.make_fact("owner", "aws_account:333333333333",
                                      {"team": "payments"}, source="h").key)[0]
    assert f.extra == {"ticket": "FIN-123", "note": "escalate to CFO before changing"}


def test_a_bom_does_not_cost_the_header(odir):
    """r18: a file saved with a UTF-8 BOM lost its whole comment header."""
    odir.mkdir()
    (odir / "owners.yaml").write_bytes(
        "﻿# nable org model v1\r\n# Owners: reviewed quarterly by FinOps (FIN-123)\r\n"
        "- fact: owner\r\n  subject: {kind: aws_account, id: '111111111111'}\r\n"
        "  value: {team: payments}\r\n  status: confirmed\r\n".encode())
    org.propose(org.make_fact("owner", "aws_account:222222222222", {"team": "growth"},
                              source="agent:x"))
    lines = (odir / "owners.yaml").read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["# nable org model v1",
                         "# Owners: reviewed quarterly by FinOps (FIN-123)"]


# ── 8. fast enough for thousands of facts ─────────────────────────────────────

def test_keys_are_computed_once(odir):
    f = org.make_fact("owner", "aws_account:111111111111", {"team": "a"}, source="x:y")
    assert f.key is f.key


# ── 9. coverage never reads a false 100% ──────────────────────────────────────

@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    import finops.storage.db as db_mod
    prev_engine, prev_dir = db_mod._ENGINE, db_mod._DATA_DIR
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "finops.db"))
    db_mod._ENGINE = None
    db_mod._DATA_DIR = None
    yield db_mod
    if db_mod._ENGINE is not None and db_mod._ENGINE is not prev_engine:
        db_mod._ENGINE.dispose()
    db_mod._ENGINE = prev_engine
    db_mod._DATA_DIR = prev_dir


def test_coverage_waits_for_every_provider(odir, fresh_db):
    """r13: on the first of the month the latest month held one day of AWS
    only, and coverage read 100% while a $90k/mo unowned GCP project was not
    in it."""
    fresh_db.get_engine()
    from finops.storage.snapshots import store_snapshots
    rows = []
    for day in range(1, 31):
        rows.append({"provider": "aws", "service": "ec2", "account_id": "111111111111",
                     "region": "us-east-1", "snapshot_date": date(2026, 9, day),
                     "amount_usd": 300.0})
        rows.append({"provider": "gcp", "service": "gce", "account_id": "big-project",
                     "region": "us", "snapshot_date": date(2026, 9, day),
                     "amount_usd": 3000.0})
    rows.append({"provider": "aws", "service": "ec2", "account_id": "111111111111",
                 "region": "us-east-1", "snapshot_date": date(2026, 10, 1),
                 "amount_usd": 300.0})
    # An export that stopped a month ago is named, not silently dropped.
    rows.append({"provider": "azure", "service": "vm", "account_id": "sub-1",
                 "region": "eu", "snapshot_date": date(2026, 8, 25), "amount_usd": 50.0})
    store_snapshots(rows)
    H("owner", "aws_account:111111111111", {"team": "payments"})
    c = org.coverage()
    assert (c["start"], c["through"]) == ("2026-09-01", "2026-09-30")
    assert c["spend_total"] == 99000.0 and c["pct_confirmed"] == 9.1
    assert any(n.startswith("azure: no cost rows since 2026-08-25") for n in c["not_read"])


# ── 10. an imported legacy fact follows its file ──────────────────────────────

def test_an_edited_legacy_file_wins_over_its_imported_copy(odir, tmp_path, monkeypatch):
    """r14: after `nable org init` imported tag_rules.yaml, an edit to the
    file changed attribution but not the org model."""
    from finops.attribution import mapper
    rules = tmp_path / "tag_rules.yaml"
    monkeypatch.setenv("FINOPS_TAG_RULES", str(rules))
    rules.write_text("rules:\n  - tag_key: team\n    maps_to_field: team\n"
                     "team_aliases:\n  payments: [pay]\n")
    assert org.import_legacy() == 3
    rules.write_text("rules:\n  - tag_key: team\n    maps_to_field: team\n"
                     "team_aliases:\n  growth: [pay]\n")
    os.utime(rules, (2e9, 2e9))
    mapper.reload_rules()
    assert mapper.tags_to_attribution({"team": "pay"})["team"] == "growth"
    assert org.team_for_tags({"team": "pay"}).team == "growth"
    # With the file gone, the imported copy is the model's again.
    rules.unlink()
    assert org.team_for_tags({"team": "pay"}).team == "payments"


# ── 11, 12. a rejection holds ─────────────────────────────────────────────────

def test_a_rejection_suppresses_every_spelling_of_the_same_fact(odir):
    """r6: a rejected alias came back as "PAY", or with an extra value key."""
    f = org.make_fact("tag_alias", "tag_value:pay", {"canonical_key": "team",
                                                     "canonical_value": "growth"},
                      source="agent:x", confidence=0.4)
    assert org.propose(f) == "added"
    org.reject(f.key, human("@maria"))
    for variant in (
            ("tag_alias", "tag_value:PAY", {"canonical_key": "team", "canonical_value": "growth"}),
            ("tag_alias", "tag_value:pay", {"canonical_key": "team", "canonical_value": "Growth",
                                            "why": "x"})):
        assert org.propose(org.make_fact(*variant, source="agent:x")) == "suppressed_rejected"
    g = org.make_fact("owner", "aws_account:111111111111", {"team": "growth"}, source="agent:x")
    org.propose(g)
    org.reject(g.key, human("@maria"))
    assert org.propose(org.make_fact("owner", "aws_account:111111111111", {"team": "Growth"},
                                     source="agent:x")) == "suppressed_rejected"
    assert org.owner_of("aws_account:111111111111") is None
    # Something else said about the same subject is still a proposal.
    assert org.propose(org.make_fact("owner", "aws_account:111111111111", {"team": "search"},
                                     source="agent:x")) == "added"


def test_a_rejected_proposal_does_not_seed_later_adapters(home, monkeypatch):
    """r20: a rejected CODEOWNERS owner still went into ctx.prior, and the
    terraform adapter derived new owner proposals from it."""
    from finops.org.adapters.data import CostData
    repo = mkrepo(home / "repo")
    (repo / ".github").mkdir()
    (repo / ".github" / "CODEOWNERS").write_text("/infra/ @acme/payments\n")
    (repo / "infra").mkdir()
    (repo / "infra" / "main.tf").write_text('provider "aws" {\n  region = "us-east-1"\n}\n')
    monkeypatch.chdir(repo)
    d = home / "org"
    org.run_adapters(d, data=CostData(), only=["codeowners", "terraform"])
    for f in org.load(d).proposals():
        org.reject(f.key, human("@maria"), d)
    (repo / "infra" / "main.tf").write_text(
        'provider "aws" {\n  allowed_account_ids = ["111111111111"]\n}\n')
    runs = org.run_adapters(d, data=CostData(), only=["codeowners", "terraform"])
    assert runs[0].results == ["suppressed_rejected"]
    assert runs[1].facts == []


# ── 15. two confirmed answers are a question ──────────────────────────────────

def test_two_confirmed_facts_after_a_merge_are_reported(odir, capsys):
    odir.mkdir()
    a = org.make_fact("owner", "aws_account:111111111111", {"team": "payments"}, source="human")
    b = org.make_fact("owner", "aws_account:111111111111", {"team": "search"}, source="human")
    rows = [{**a.to_dict(), "status": "confirmed", "confirmed_by": "@a",
             "confirmed_at": "2026-09-01"},
            {**b.to_dict(), "status": "confirmed", "confirmed_by": "@b",
             "confirmed_at": "2026-09-02"}]
    (odir / "owners.yaml").write_text("# nable org model v1\n" + yaml.safe_dump(rows))
    m = org.load()
    assert [(w.key, o.key) for w, o in m.conflicts()] == [(b.key, a.key)]
    qs = org.questions(include_spend=False)
    assert [(q.kind, q.default, q.key) for q in qs] == [("conflict", "y", b.key)]
    assert q_text_names_both(qs[0].text, "payments", "search")
    _, out, _ = run(capsys, "status")
    assert "also confirmed" in out
    org.confirm(b.key, human("@maria"))
    assert org.load().conflicts() == []


def q_text_names_both(text: str, *teams: str) -> bool:
    return all(t in text for t in teams)


# ── 16. CRLF Terraform ────────────────────────────────────────────────────────

def test_a_crlf_heredoc_does_not_swallow_the_file():
    """r10: with CRLF line endings a heredoc never ended, and every block
    after it was lost."""
    from finops.org.adapters import _hcl
    text = ('resource "aws_iam_policy" "p" {\n  policy = <<POL\n{\n  "Statement": [{\nPOL\n}\n'
            'provider "aws" {\n  region = "us-east-1"\n}\n')
    for t in (text, text.replace("\n", "\r\n")):
        assert [b.type for b in _hcl.parse(t).blocks] == ["resource", "provider"]


# ── 17, 18. the MCP tool proposes, as an agent ────────────────────────────────

def _tool(name, **kwargs):
    from finops import server
    return asyncio.run(getattr(server, name)(**kwargs))


def test_an_mcp_proposal_is_always_marked_as_an_agents(odir):
    for src in ("codeowners:infra/", "legacy:tag_rules.yaml", "human", "", "agent:x"):
        r = _tool("propose_org_fact", fact="owner", subject_kind="aws_account",
                  subject_id="111111111111", value={"team": f"t{len(src)}"}, source=src)
        assert r["fact"]["source"].startswith("agent:"), src
        assert not r["fact"]["source"].startswith("agent:agent:")


def test_an_mcp_threshold_is_refused_with_the_human_command(odir):
    r = _tool("propose_org_fact", fact="threshold", subject_kind="team",
              subject_id="payments", value={"max_auto_monthly_usd": 99999}, source="x")
    assert r["result"] == "refused" and "nable org set threshold" in r["error"]
    assert org.load().facts == []


def test_the_propose_tool_has_a_precise_schema():
    from finops import server
    tool = next(t for t in server.mcp._tool_manager.list_tools()
                if t.name == "propose_org_fact")
    props = tool.parameters["properties"]
    assert "owner" in props["fact"]["enum"] and "repo_path" in props["subject_kind"]["enum"]
    assert all(props[k].get("description") for k in props)
    assert "tag_alias {canonical_key, canonical_value}" in tool.description


def test_set_threshold_is_the_human_form(odir, capsys):
    code, _, _ = run(capsys, "set", "threshold", "--subject", "team:payments",
                       "--max-auto-usd", "200", "--as", "@cfo")
    assert code == 0
    t = org.load().threshold_for("payments")
    assert t["max_auto_monthly_usd"] == 200.0
    code, _, err = run(capsys, "set", "threshold", "--subject", "team:payments", "--as", "@cfo")
    assert code == 1 and "max_auto_monthly_usd" in err


def test_status_json_lists_layers_and_conflicts(odir, capsys):
    H("owner", "aws_account:111111111111", {"team": "a"})
    _, out, _ = run(capsys, "status", "--json")
    doc = json.loads(out)
    assert doc["layers"] == [{"dir": str(odir), "source": "FINOPS_ORG_DIR", "trusted": True,
                              "repo": None}]
    assert doc["repo_paths_without_repo"] == []
