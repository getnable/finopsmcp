"""The org model: facts agents propose and humans confirm, kept as plain YAML.

What has to stay true, because everything later (guard asks that name an
owner, tickets routed to a team, per-team thresholds) reads it:

  - a bad entry is skipped with a warning naming its file and index, and a
    load never raises;
  - a fact's key is stable across writes, loads and key order;
  - precedence: confirmed (newest) beats proposed (most confident, newest);
    rejected and expired never answer;
  - a proposal is always written as proposed, never replaces a confirmed
    fact, and a rejected fact is never proposed again;
  - nothing is created in a repo unless asked (`init --here`);
  - coverage says "not read" when it has no spend to read, never 0% or 100%.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from finops import org
from finops.org import store
from finops.org.model import Fact, OrgModel, Subject, fact_key, local_today, pick

TODAY = local_today().isoformat()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A clean HOME with no legacy files, no org env, and a work dir outside
    any git repo. The org dir is FINOPS_ORG_DIR unless a test says otherwise."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for var in ("FINOPS_ORG_DIR", "FINOPS_TAG_RULES", "FINOPS_ACCOUNTS_FILE",
                "FINOPS_REQUIRED_TAGS", "FINOPS_PROTECTED_TAGS", "FINOPS_PROFILE",
                "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "no-such.db"))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(store, "_data_dir", lambda: tmp_path / "data")
    return h


@pytest.fixture
def odir(home, tmp_path, monkeypatch):
    d = tmp_path / "orgdir"
    monkeypatch.setenv("FINOPS_ORG_DIR", str(d))
    return d


def owner(subject, team, *, source="codeowners:x", confidence=0.5, dollars=None, **value):
    return org.make_fact("owner", subject, {"team": team, **value}, source=source,
                         confidence=confidence, dollars_monthly=dollars)


def write(path: Path, entries, header=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(entries, sort_keys=False)
    path.write_text(("# nable org model v1\n" if header else "") + text)


# ── validation ────────────────────────────────────────────────────────────────

def test_a_bad_entry_is_skipped_with_its_file_and_index(odir, monkeypatch):
    # The warning is logged too. Captured on the logger itself: other tests
    # reconfigure logging, so the root handler caplog uses may never see it.
    seen: list[str] = []
    monkeypatch.setattr(org.model.log, "warning",
                        lambda msg, *a: seen.append(msg % a))
    write(odir / "owners.yaml", [
        {"fact": "owner", "subject": {"kind": "aws_account", "id": "111111111111"},
         "value": {"team": "payments"}, "source": "human", "status": "confirmed"},
        {"fact": "owner", "subject": {"kind": "planet", "id": "mars"},
         "value": {"team": "x"}, "source": "human"},
        {"fact": "owner", "subject": {"kind": "aws_account", "id": "2"}, "value": {},
         "source": "human"},
        "just a string",
        {"fact": "environment", "subject": {"kind": "k8s_namespace", "id": "ci"},
         "value": {"env": "production-ish"}, "source": "x"},
        {"fact": "owner", "subject": {"kind": "aws_account", "id": "3"},
         "value": {"team": "a"}, "source": "x", "confidence": 1.7},
        {"fact": "owner", "subject": {"kind": "aws_account", "id": "4"},
         "value": {"team": "a"}, "source": "x", "status": "maybe"},
        {"fact": "owner", "subject": {"kind": "aws_account", "id": "5"},
         "value": {"team": "a"}, "source": "x", "review_after": "someday"},
    ])
    m = org.load()
    assert [str(f.subject) for f in m.facts] == ["aws_account:111111111111"]
    for i in range(1, 8):
        assert any(w.startswith(f"owners.yaml[{i}]: skipped") for w in m.warnings), i
    assert any("owners.yaml[1]" in m for m in seen)


def test_a_broken_file_never_crashes_a_load_and_is_never_rewritten(odir):
    odir.mkdir()
    broken = "- fact: owner\n  subject: {kind: aws_account, id: '1'\n"
    (odir / "owners.yaml").write_text(broken)
    (odir / "teams.yaml").write_text("fact: team\n")        # a mapping, not a list
    (odir / "notes.yaml").write_text("[]\n")                 # not an org file
    (odir / "README.md").write_text("hello\n")
    m = org.load()
    assert m.facts == []
    assert any("owners.yaml" in w and "YAML" in w for w in m.warnings)
    assert any("teams.yaml" in w for w in m.warnings)
    assert any(w.startswith("notes.yaml") for w in m.warnings)
    assert not any("README" in w for w in m.warnings)
    with pytest.raises(org.OrgError):
        org.propose(owner("aws_account:123456789012", "payments"))
    assert (odir / "owners.yaml").read_text() == broken


def test_an_unreadable_entry_is_kept_when_the_file_is_rewritten(odir):
    odd = {"fact": "owner", "subject": {"kind": "aws_account", "id": "9"},
           "value": {"team": "x"}, "source": "x", "status": "someday"}
    write(odir / "owners.yaml", [odd])
    assert org.propose(owner("aws_account:123456789012", "payments")) == "added"
    data = yaml.safe_load((odir / "owners.yaml").read_text())
    assert odd in data
    assert len(data) == 2


def test_value_shapes_are_checked_per_kind():
    with pytest.raises(org.FactError):
        org.make_fact("team", "aws_account:1", {"name": "x"}, source="s")
    with pytest.raises(org.FactError):
        org.make_fact("tag_key", "org:org", {"canonical": "colour", "keys": ["c"]}, source="s")
    with pytest.raises(org.FactError):
        org.make_fact("tag_alias", "tag_value:pay", {"canonical_key": "team"}, source="s")
    with pytest.raises(org.FactError):
        org.make_fact("threshold", "team:payments", {}, source="s")
    with pytest.raises(org.FactError):
        org.make_fact("account", "repo_path:x", {"name": "x"}, source="s")
    f = org.make_fact("threshold", "team:payments", {"max_auto_monthly_usd": 900},
                      source="s")
    assert f.value["max_auto_monthly_usd"] == 900.0


def test_an_unquoted_account_id_keeps_its_leading_zeros(odir):
    (odir).mkdir()
    (odir / "owners.yaml").write_text(
        "# nable org model v1\n- fact: owner\n  subject: {kind: aws_account, id: 12345678901}\n"
        "  value: {team: payments}\n  source: human\n  status: confirmed\n")
    m = org.load()
    assert m.owner_of("aws_account:012345678901").team == "payments"


# ── keys ──────────────────────────────────────────────────────────────────────

def test_the_key_is_sha1_of_the_canonical_fact_first_ten_hex():
    s = Subject("aws_account", "123456789012")
    blob = json.dumps(["owner", "aws_account", "123456789012", {"team": "payments"}],
                      sort_keys=True, separators=(",", ":"))
    assert fact_key("owner", s, {"team": "payments"}) == \
        hashlib.sha1(blob.encode()).hexdigest()[:10]


def test_the_key_ignores_value_key_order_and_survives_a_round_trip(odir):
    a = owner("aws_account:123456789012", "payments", channel="#pay")
    b = org.make_fact("owner", "aws_account:123456789012",
                      {"channel": "#pay", "team": "payments"}, source="other", confidence=0.9)
    assert a.key == b.key
    assert a.key != owner("aws_account:123456789012", "search").key
    org.propose(a)
    loaded = org.load().facts
    assert [f.key for f in loaded] == [a.key]
    org.confirm(a.key, "@maria")
    assert org.load().facts[0].key == a.key


# ── precedence ────────────────────────────────────────────────────────────────

def _f(status, *, team="t", conf=0.5, confirmed_at=None, proposed_at=None):
    return Fact("owner", Subject("aws_account", "1"), {"team": team}, "s", conf, status,
                proposed_at=proposed_at, confirmed_at=confirmed_at)


def test_confirmed_beats_proposed_and_the_newest_confirmation_wins():
    old = _f("confirmed", team="old", confirmed_at="2026-01-01")
    new = _f("confirmed", team="new", confirmed_at="2026-06-01")
    sure_guess = _f("proposed", team="guess", conf=0.99)
    assert pick([sure_guess, old, new]).value["team"] == "new"
    assert pick([sure_guess, old]).value["team"] == "old"


def test_among_proposals_confidence_then_recency_wins():
    a = _f("proposed", team="a", conf=0.6, proposed_at="2026-01-01")
    b = _f("proposed", team="b", conf=0.8, proposed_at="2025-01-01")
    c = _f("proposed", team="c", conf=0.8, proposed_at="2026-02-01")
    assert pick([a, b, c]).value["team"] == "c"
    assert pick([a, b]).value["team"] == "b"


def test_rejected_and_expired_never_answer():
    m = OrgModel([_f("rejected", team="r"), _f("expired", team="e")])
    assert m.resolve("owner", "aws_account:1") is None
    assert m.owner_of("aws_account:1") is None
    m = OrgModel([_f("rejected", team="r"), _f("proposed", team="p")])
    assert m.owner_of("aws_account:1").team == "p"


def test_a_stale_fact_is_still_used_and_flagged(odir):
    past = (local_today() - timedelta(days=1)).isoformat()
    write(odir / "owners.yaml", [
        {"fact": "owner", "subject": {"kind": "aws_account", "id": "111111111111"},
         "value": {"team": "payments"}, "source": "human", "status": "confirmed",
         "confirmed_at": "2025-01-01", "review_after": past}])
    m = org.load()
    r = m.owner_of("aws_account:111111111111")
    assert r.team == "payments" and r.confirmed and r.stale
    qs = org.questions(model=m, include_spend=False)
    assert [q.kind for q in qs] == ["recheck"]


# ── proposals ─────────────────────────────────────────────────────────────────

def test_a_proposal_is_always_written_as_proposed(odir):
    f = owner("aws_account:123456789012", "payments", confidence=0.9)
    f.status, f.confirmed_by, f.confirmed_at = "confirmed", "@mallory", TODAY
    assert org.propose(f) == "added"
    saved = org.load().facts[0]
    assert saved.status == "proposed"
    assert saved.confirmed_by is None and saved.confirmed_at is None
    assert saved.proposed_at == TODAY
    assert org.owner_of("aws_account:123456789012").confirmed is False


def test_the_same_proposal_twice_is_a_duplicate(odir):
    f = owner("aws_account:123456789012", "payments")
    assert org.propose(f) == "added"
    assert org.propose(f) == "duplicate"
    assert len(org.load().facts) == 1


def test_a_proposal_never_overwrites_a_confirmed_fact(odir):
    good = owner("aws_account:123456789012", "payments")
    org.propose(good)
    org.confirm(good.key, "@maria")
    rival = owner("aws_account:123456789012", "search", confidence=0.99)
    assert org.propose(rival) == "conflict"
    m = org.load()
    r = m.owner_of("aws_account:123456789012")
    assert r.team == "payments" and r.confirmed
    assert [(w.key, p.key) for w, p in m.conflicts()] == [(good.key, rival.key)]
    assert {f.status for f in m.facts} == {"confirmed", "proposed"}
    q = org.questions(model=m, include_spend=False)[0]
    assert q.kind == "conflict" and q.default == "n" and q.key == rival.key


def test_a_rejected_fact_suppresses_the_same_proposal(odir):
    f = owner("aws_account:123456789012", "payments")
    org.propose(f)
    org.reject(f.key, "@maria")
    assert org.propose(f) == "suppressed_rejected"
    again = owner("aws_account:123456789012", "payments", source="another:adapter",
                  confidence=0.99)
    assert org.propose(again) == "suppressed_rejected"
    m = org.load()
    assert [x.status for x in m.facts] == ["rejected"]
    assert m.facts[0].confirmed_by == "@maria"
    # A different value for the same subject is a different fact.
    assert org.propose(owner("aws_account:123456789012", "search")) == "added"


def test_confirming_one_answer_expires_the_old_confirmed_one(odir):
    a = owner("aws_account:123456789012", "payments")
    b = owner("aws_account:123456789012", "search")
    org.propose(a)
    org.confirm(a.key, "@maria")
    org.propose(b)
    org.confirm(b.key, "@maria")
    by_key = {f.key: f.status for f in org.load().facts}
    assert by_key == {a.key: "expired", b.key: "confirmed"}
    assert org.owner_of("aws_account:123456789012").team == "search"


def test_an_expired_fact_can_be_proposed_again(odir):
    a = owner("aws_account:123456789012", "payments")
    b = owner("aws_account:123456789012", "search")
    for f in (a, b):
        org.propose(f)
        org.confirm(f.key, "@maria")
    assert org.propose(a) == "conflict"
    assert {f.key: f.status for f in org.load().facts}[a.key] == "proposed"


def test_confirm_needs_a_name_and_a_real_key(odir):
    f = owner("aws_account:123456789012", "payments")
    org.propose(f)
    with pytest.raises(org.OrgError):
        org.confirm(f.key, "  ")
    with pytest.raises(org.OrgError):
        org.confirm("0000000000", "@maria")
    assert org.confirm(f.key[:6], "@maria").status == "confirmed"


# ── owner_of ──────────────────────────────────────────────────────────────────

def test_repo_path_longest_prefix_wins_on_whole_components(odir):
    org.set_fact(owner("repo_path:infra", "platform"), "@lead")
    org.set_fact(owner("repo_path:infra/payments/", "payments"), "@lead")
    org.set_fact(owner("repo_path:.", "everyone"), "@lead")
    m = org.load()
    assert m.owner_of("repo_path:infra/payments/api/main.tf").team == "payments"
    assert m.owner_of("repo_path:./infra/payments").team == "payments"
    assert m.owner_of("repo_path:infra/paymentsx/main.tf").team == "platform"
    assert m.owner_of("repo_path:infra").team == "platform"
    assert m.owner_of("repo_path:docs/readme.md").team == "everyone"
    r = m.owner_of({"kind": "repo_path", "id": "infra/payments/x"})
    assert r.matched == "repo_path:infra/payments" and r.confirmed


def test_a_deeper_proposal_is_cited_but_not_confirmed(odir):
    org.set_fact(owner("repo_path:infra", "platform"), "@lead")
    org.propose(owner("repo_path:infra/payments", "payments", confidence=0.7))
    r = org.owner_of("repo_path:infra/payments/main.tf")
    assert (r.team, r.confirmed) == ("payments", False)


def test_owner_carries_the_team_channel_and_resolves_aliases(odir):
    org.set_fact(org.make_fact("team", "team:payments",
                               {"name": "payments", "aliases": ["pay"],
                                "channel": "#payments-oncall", "people": ["@maria"]},
                               source="human"), "@lead")
    org.set_fact(owner("aws_account:123456789012", "pay"), "@lead")
    org.set_fact(owner("k8s_namespace:checkout", "payments", channel="#checkout"), "@lead")
    m = org.load()
    r = m.owner_of("aws_account:123456789012")
    assert (r.team, r.channel, r.people, r.confirmed) == \
        ("payments", "#payments-oncall", ["@maria"], True)
    assert m.owner_of("k8s_namespace:prod-cluster/checkout").channel == "#checkout"
    assert m.owner_of("team:pay").team == "payments"
    assert m.owner_of("aws_account:999999999999") is None


# ── tags ──────────────────────────────────────────────────────────────────────

def test_team_for_tags_reads_tag_keys_then_aliases(odir):
    org.set_fact(org.make_fact("tag_key", "org:org",
                               {"canonical": "team", "keys": ["Team", "costcenter"]},
                               source="human"), "@finops")
    org.set_fact(org.make_fact("tag_alias", "tag_value:pay",
                               {"canonical_key": "team", "canonical_value": "payments"},
                               source="human"), "@finops")
    org.set_fact(org.make_fact("team", "team:payments", {"name": "payments",
                                                         "channel": "#payments-oncall"},
                               source="human"), "@finops")
    m = org.load()
    r = m.team_for_tags({"TEAM": "Pay", "env": "prod"})
    assert (r.team, r.channel, r.confirmed) == ("payments", "#payments-oncall", True)
    assert m.team_for_tags({"costcenter": "pay"}).team == "payments"
    # A key nobody said means team is not read as one.
    assert m.team_for_tags({"squad": "pay"}) is None
    # A team key with a value no fact knows is still that team.
    r = m.team_for_tags({"team": "search"})
    assert (r.team, r.confirmed) == ("search", True)


def test_a_proposed_alias_gives_an_unconfirmed_answer(odir):
    org.set_fact(org.make_fact("tag_key", "org:org", {"canonical": "team", "keys": ["team"]},
                               source="human"), "@finops")
    org.propose(org.make_fact("tag_alias", "tag_value:pay",
                              {"canonical_key": "team", "canonical_value": "payments"},
                              source="inference:tag-values", confidence=0.8))
    r = org.team_for_tags({"team": "pay"})
    assert (r.team, r.confirmed) == ("payments", False)
    # Without any tag_key fact the conventional key is used, unconfirmed.
    assert org.load().team_for_tags({"team": "x"}).confirmed is True
    assert OrgModel([]).team_for_tags({"team": "x"}).confirmed is False
    assert OrgModel([]).team_for_tags({"owner": "x"}) is None


def test_tag_value_subjects_resolve_through_aliases(odir):
    org.set_fact(org.make_fact("tag_alias", "tag_value:pay",
                               {"canonical_key": "team", "canonical_value": "payments"},
                               source="human"), "@finops")
    r = org.owner_of("tag_value:PAY")
    assert (r.team, r.confirmed) == ("payments", True)


def test_environment_guesses_never_come_back_confirmed(odir):
    org.set_fact(org.make_fact("tag_key", "org:org",
                               {"canonical": "environment", "keys": ["stage"]},
                               source="human"), "@finops")
    org.set_fact(org.make_fact("environment", "aws_account:222222222222", {"env": "nonprod"},
                               source="human"), "@finops")
    org.propose(org.make_fact("environment", "k8s_namespace:ci", {"env": "nonprod"},
                              source="inference:name", confidence=0.9))
    m = org.load()
    assert m.environment_of("aws_account:222222222222") == ("nonprod", True)
    assert m.environment_of({"kind": "k8s_namespace", "id": "ci"}) == ("nonprod", False)
    assert m.environment_of({"stage": "nonprod"}) == ("nonprod", True)
    assert m.environment_of({"stage": "staging"}) == ("nonprod", False)
    assert m.environment_of({"stage": "wibble"}) == ("unknown", False)
    assert m.environment_of("aws_account:333333333333") == ("unknown", False)


def test_thresholds_use_confirmed_facts_narrowest_scope_first(odir):
    org.set_fact(org.make_fact("threshold", "org:org",
                               {"max_auto_monthly_usd": 500, "velocity_cap_usd": 2000},
                               source="human"), "@cfo")
    org.set_fact(org.make_fact("threshold", "environment:prod",
                               {"max_auto_monthly_usd": 100}, source="human"), "@cfo")
    org.set_fact(org.make_fact("threshold", "team:payments",
                               {"max_auto_monthly_usd": 2500}, source="human"), "@cfo")
    org.propose(org.make_fact("threshold", "team:search", {"max_auto_monthly_usd": 99999},
                              source="inference", confidence=0.9))
    m = org.load()
    assert m.threshold_for("payments", "prod")["max_auto_monthly_usd"] == 2500
    t = m.threshold_for(None, "prod")
    assert t["max_auto_monthly_usd"] == 100 and t["velocity_cap_usd"] == 2000
    assert t["scope"] == {"max_auto_monthly_usd": "environment:prod",
                          "velocity_cap_usd": "org:org"}
    assert m.threshold_for("search")["max_auto_monthly_usd"] == 500
    assert OrgModel([]).threshold_for("payments") == {}


# ── legacy ────────────────────────────────────────────────────────────────────

TAG_RULES = """\
rules:
  - tag_key: "team"
    maps_to_field: "team"
    priority: 10
  - tag_key: "costcenter"
    maps_to_field: "team"
    priority: 50
  - tag_key: "env"
    maps_to_field: "environment"
  - tag_key: "team"
    tag_value_pattern: "infra*"
    maps_to_field: "team"
    maps_to_value: "platform"
    priority: 5
  - tag_key: "team"
    tag_value_pattern: "fin"
    maps_to_field: "team"
    maps_to_value: "finance"
team_aliases:
  payments: [pay, payment-svc]
"""

ACCOUNTS = """\
accounts:
  - name: payments-prod
    account_id: "123456789012"
    region: us-east-1
    tags: {team: payments, cost_center: CC-42}
  - name: no-id
"""


@pytest.fixture
def legacy_home(home, odir):
    (home / ".finops").mkdir()
    (home / ".finops" / "tag_rules.yaml").write_text(TAG_RULES)
    (home / ".finops-mcp").mkdir()
    (home / ".finops-mcp" / "accounts.yaml").write_text(ACCOUNTS)
    return home


def test_legacy_facts_from_tag_rules_and_accounts(legacy_home, monkeypatch):
    monkeypatch.setenv("FINOPS_REQUIRED_TAGS", "team,Environment,app")
    facts = org.legacy_facts()
    assert all(f.status == "confirmed" and f.origin == "legacy" for f in facts)
    got = {(f.fact, str(f.subject), f.source, json.dumps(f.value, sort_keys=True))
           for f in facts}
    src = "legacy:tag_rules.yaml"
    assert ("tag_key", "org:org", src,
            json.dumps({"canonical": "team", "keys": ["team", "costcenter"]}, sort_keys=True)) in got
    assert ("tag_key", "org:org", src,
            json.dumps({"canonical": "environment", "keys": ["env"]}, sort_keys=True)) in got
    assert ("tag_alias", "tag_value:fin", src, json.dumps(
        {"canonical_key": "team", "canonical_value": "finance"}, sort_keys=True)) in got
    assert ("team", "team:payments", src, json.dumps(
        {"name": "payments", "aliases": ["pay", "payment-svc"]}, sort_keys=True)) in got
    assert ("tag_alias", "tag_value:pay", src, json.dumps(
        {"canonical_key": "team", "canonical_value": "payments"}, sort_keys=True)) in got
    # A glob stays with the mapper: the model has no pattern facts.
    assert not any(f.subject.id == "infra*" for f in facts)
    assert ("account", "aws_account:123456789012", "legacy:accounts.yaml", json.dumps(
        {"name": "payments-prod", "cost_center": "CC-42"}, sort_keys=True)) in got
    assert ("owner", "aws_account:123456789012", "legacy:accounts.yaml",
            json.dumps({"team": "payments"}, sort_keys=True)) in got
    assert ("tag_key", "org:org", "legacy:FINOPS_REQUIRED_TAGS", json.dumps(
        {"canonical": "environment", "keys": ["Environment"]}, sort_keys=True)) in got
    assert not any("app" in f.value.get("keys", []) for f in facts)


def test_legacy_facts_answer_queries_and_a_file_fact_wins(legacy_home):
    m = org.load()
    assert m.team_for_tags({"CostCenter": "payment-svc"}).team == "payments"
    assert m.owner_of("aws_account:123456789012").confirmed
    assert m.team_for_tags({"team": "fin"}).team == "finance"
    alias = next(f for f in m.facts if f.fact == "tag_alias" and f.subject.id == "fin")
    org.reject(alias.key, "@finops")
    m = org.load()
    assert m.team_for_tags({"team": "fin"}).team == "fin"   # the alias no longer applies
    assert [f.status for f in m.facts if f.key == alias.key] == ["rejected"]
    # A confirmed file fact in the slot replaces the legacy one.
    org.set_fact(owner("aws_account:123456789012", "search"), "@lead")
    assert org.owner_of("aws_account:123456789012").team == "search"


def test_no_legacy_files_means_no_legacy_facts(home):
    assert org.legacy_facts() == []


def test_a_broken_legacy_file_is_a_warning(home):
    (home / ".finops").mkdir()
    (home / ".finops" / "tag_rules.yaml").write_text("rules: [\n")
    warnings: list[str] = []
    assert org.legacy_facts(warnings=warnings) == []
    assert any("tag_rules.yaml" in w for w in warnings)


def test_import_legacy_writes_them_once(legacy_home, odir):
    n = org.import_legacy()
    assert n > 0
    assert org.import_legacy() == 0
    assert "legacy:tag_rules.yaml" in (odir / "tags.yaml").read_text()
    assert len(org.load().facts) == len(org.load(legacy=False).facts)


# ── where the model lives ─────────────────────────────────────────────────────

def _repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    (r / ".git").mkdir(parents=True)
    (r / "svc" / "api").mkdir(parents=True)
    return r


def test_dir_resolution_order(home, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo / "svc" / "api")
    # No nable.org in the repo: the data dir, and the repo is left alone.
    assert org.resolve_dir() == (tmp_path / "data" / "org", "data_dir")
    org.propose(owner("aws_account:123456789012", "payments"))
    assert not (repo / "nable.org").exists()
    assert (tmp_path / "data" / "org" / "owners.yaml").exists()
    # An existing nable.org at the repo root wins over the data dir.
    (repo / "nable.org").mkdir()
    assert org.resolve_dir() == (repo.resolve() / "nable.org", "repo")
    # FINOPS_ORG_DIR wins over both, and an explicit dir over that.
    monkeypatch.setenv("FINOPS_ORG_DIR", str(tmp_path / "env-org"))
    assert org.resolve_dir() == (tmp_path / "env-org", "FINOPS_ORG_DIR")
    assert org.resolve_dir(tmp_path / "x") == (tmp_path / "x", "argument")


def test_load_creates_nothing(home, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    org.load()
    org.questions(include_spend=False)
    assert not (repo / "nable.org").exists()
    assert not (tmp_path / "data" / "org").exists()


def test_repo_path_of_is_relative_to_the_git_root(tmp_path):
    repo = _repo(tmp_path)
    assert org.repo_path_of(repo / "svc" / "api") == "svc/api"
    assert org.repo_path_of(repo) == "."
    assert org.git_root(repo / "svc") == repo.resolve()


# ── writes ────────────────────────────────────────────────────────────────────

def test_writes_are_sorted_deterministic_and_carry_the_header(home, tmp_path, monkeypatch):
    facts = [owner("aws_account:333333333333", "c"), owner("aws_account:111111111111", "a"),
             owner("repo_path:infra", "p"),
             org.make_fact("environment", "aws_account:111111111111", {"env": "prod"},
                           source="s"),
             owner("aws_account:222222222222", "b", people=["x", "y"])]
    texts = []
    for order in (facts, list(reversed(facts))):
        d = tmp_path / f"o{len(texts)}"
        for f in order:
            org.propose(f, d)
        texts.append({p.name: p.read_text() for p in sorted(d.iterdir())})
    assert texts[0] == texts[1]
    owners = texts[0]["owners.yaml"]
    assert owners.splitlines()[0] == "# nable org model v1"
    ids = [f["subject"]["id"] for f in yaml.safe_load(owners)]
    assert ids == ["111111111111", "222222222222", "333333333333", "infra"]
    assert "subject: {kind: aws_account, id: '111111111111'}" in owners
    assert f"proposed_at: {TODAY}\n" in owners
    assert not [p for p in (tmp_path / "o0").iterdir() if p.name.startswith(".")]


def test_a_failed_write_leaves_the_old_file(odir, monkeypatch):
    org.propose(owner("aws_account:111111111111", "a"))
    before = (odir / "owners.yaml").read_text()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(store.os, "replace", boom)
    with pytest.raises(OSError):
        org.propose(owner("aws_account:222222222222", "b"))
    assert (odir / "owners.yaml").read_text() == before
    assert [p.name for p in odir.iterdir() if p.name.startswith(".")] == []


def test_the_top_comment_block_and_file_mode_are_kept(odir):
    odir.mkdir()
    p = odir / "owners.yaml"
    p.write_text("# nable org model v1\n# Owned by the platform team. Edit by PR.\n[]\n")
    p.chmod(0o640)
    org.propose(owner("aws_account:111111111111", "a"))
    lines = p.read_text().splitlines()
    assert lines[:2] == ["# nable org model v1", "# Owned by the platform team. Edit by PR."]
    assert (p.stat().st_mode & 0o777) == 0o640


def test_export_yaml_and_json(odir, tmp_path, capsys):
    org.propose(owner("aws_account:111111111111", "a"))
    org.export()
    out = capsys.readouterr().out
    assert out.startswith("# nable org model v1\n")
    org.export(tmp_path / "x.json", "json")
    rows = json.loads((tmp_path / "x.json").read_text())
    assert rows[0]["key"] and rows[0]["subject"] == "aws_account:111111111111"


# ── questions ─────────────────────────────────────────────────────────────────

def test_questions_rank_by_dollars_then_least_confident(odir):
    org.propose(owner("aws_account:111111111111", "a", confidence=0.9, dollars=100))
    org.propose(owner("aws_account:222222222222", "b", confidence=0.9, dollars=8210))
    org.propose(owner("aws_account:333333333333", "c", confidence=0.3, dollars=500))
    org.propose(owner("aws_account:444444444444", "d", confidence=0.8, dollars=500))
    org.propose(owner("aws_account:555555555555", "e", confidence=0.1))
    qs = org.questions(include_spend=False)
    assert [q.subject for q in qs] == ["aws_account:222222222222", "aws_account:333333333333",
                                       "aws_account:444444444444", "aws_account:111111111111",
                                       "aws_account:555555555555"]
    assert all(q.default == "y" and q.command == f"nable org confirm {q.key}" for q in qs)
    assert len(org.questions(2, include_spend=False)) == 2


# ── coverage ──────────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    import finops.storage.db as db_mod
    prev_engine, prev_dir = db_mod._ENGINE, db_mod._DATA_DIR
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "finops.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    db_mod._ENGINE = None
    db_mod._DATA_DIR = None
    yield db_mod
    if db_mod._ENGINE is not None and db_mod._ENGINE is not prev_engine:
        db_mod._ENGINE.dispose()
    db_mod._ENGINE = prev_engine
    db_mod._DATA_DIR = prev_dir


def test_coverage_without_cost_data_says_not_read(odir):
    org.set_fact(owner("aws_account:111111111111", "a"), "@lead")
    org.propose(owner("aws_account:222222222222", "b"))
    cov = org.coverage()
    assert cov["basis"] == "subjects"
    assert cov["pct_confirmed"] is None and cov["spend_total"] is None
    assert "not read" in cov["summary"]
    assert "0%" not in cov["summary"] and "100%" not in cov["summary"]
    assert cov["not_read"]
    assert cov["subjects"] == {"total": 2, "confirmed_owner": 1, "proposed_owner": 1,
                               "unowned": 0}


def test_coverage_with_an_empty_history_says_not_read(odir, fresh_db):
    fresh_db.get_engine()
    cov = org.coverage()
    assert cov["pct_confirmed"] is None and "not read" in cov["summary"]


def _seed(db, month="2026-08"):
    now = datetime.now(UTC)
    snaps = [("aws", "111111111111", 600.0), ("aws", "222222222222", 300.0),
             ("aws", "333333333333", 100.0), ("gcp", "proj-x", 200.0)]
    with db.get_engine().begin() as conn:
        for provider, acct, usd in snaps:
            for day in ("01", "15"):
                conn.execute(db.cost_snapshots.insert().values(
                    provider=provider, service="EC2", account_id=acct, region="us-east-1",
                    snapshot_date=f"{month}-{day}", amount_usd=usd / 2, granularity="DAILY",
                    captured_at=now))
        # An older month that must not be counted.
        conn.execute(db.cost_snapshots.insert().values(
            provider="aws", service="EC2", account_id="111111111111", region="",
            snapshot_date="2026-07-10", amount_usd=99999.0, granularity="DAILY",
            captured_at=now))
        for acct, team, usd in (("333333333333", "search", 60.0),
                                ("333333333333", "unattributed", 40.0)):
            conn.execute(db.attributed_costs.insert().values(
                provider="aws", service="EC2", account_id=acct, team=team, environment="",
                snapshot_date=f"{month}-15", amount_usd=usd, captured_at=now))


def test_coverage_reads_the_latest_month_by_account_and_team(odir, fresh_db):
    _seed(fresh_db)
    org.set_fact(owner("aws_account:111111111111", "payments"), "@lead")
    org.propose(owner("aws_account:222222222222", "search", confidence=0.7))
    org.set_fact(org.make_fact("team", "team:search", {"name": "search"}, source="human"),
                 "@lead")
    cov = org.coverage()
    assert cov["basis"] == "spend" and cov["month"] == "2026-08"
    assert cov["spend_total"] == 1200.0
    assert cov["spend_confirmed_owner"] == 660.0          # 600 payments + 60 tagged search
    assert cov["spend_proposed_owner"] == 300.0
    assert cov["spend_unowned"] == 240.0                  # 40 untagged + gcp 200
    assert cov["pct_confirmed"] == 55.0
    assert cov["by_team"]["search"] == {"confirmed": 60.0, "proposed": 300.0}
    assert [u["subject"] for u in cov["unowned"]] == ["gcp_project:proj-x",
                                                      "aws_account:333333333333"]
    assert "55.0% of 2026-08 spend" in cov["summary"]


def test_questions_include_unowned_spend_and_price_facts(odir, fresh_db):
    _seed(fresh_db)
    org.propose(owner("aws_account:222222222222", "search", confidence=0.7))
    qs = org.questions()
    assert [(q.kind, q.subject) for q in qs] == [
        ("unowned", "aws_account:111111111111"), ("confirm", "aws_account:222222222222"),
        ("unowned", "gcp_project:proj-x"), ("unowned", "aws_account:333333333333")]
    assert qs[1].dollars_monthly == 300.0
    assert qs[0].command == ("nable org set owner --subject aws_account:111111111111 "
                             "--team TEAM")


# ── import cost ───────────────────────────────────────────────────────────────

def test_importing_finops_org_stays_light(tmp_path):
    """The guard hook will import finops.org on every agent tool call. No
    SQLAlchemy, boto3, httpx or PyYAML at import, and a generous time bound."""
    code = ("import sys, time; t = time.perf_counter(); import finops.org; "
            "dt = time.perf_counter() - t; "
            "heavy = [m for m in ('sqlalchemy', 'boto3', 'httpx', 'yaml', 'mcp') "
            "if m in sys.modules]; print(dt, ','.join(heavy))")
    env = {**os.environ, "HOME": str(tmp_path)}
    src = str(Path(org.__file__).resolve().parents[2])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    best = None
    for _ in range(3):
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env=env, check=True).stdout.split()
        assert len(out) == 1, f"heavy modules imported: {out[1:]}"
        best = min(best or 99.0, float(out[0]))
    assert best < 0.5, f"import finops.org took {best * 1000:.0f} ms"
