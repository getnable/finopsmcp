"""AI budgets in the org model: nable.org/budgets.yaml (D85).

A budget is an ai_budget fact about a team, a person or the org, in
list-price-equivalent US dollars a month (monthly_usd), and on a team an
optional per_person_usd. What has to hold:

  - the schema refuses what does not fit, and the error names the field;
  - a person is an email or github:login, matched without regard to case;
  - only an ai_budget fact may be about a person, and ai_budget facts live in
    budgets.yaml alone, so a 0.13.0 loader, which reads only its own eight
    files, never meets either;
  - a person states one through `nable org set ai_budget`, at a terminal or
    with --as; without either it is refused;
  - the model answers which budgets apply to a person: their own, else their
    confirmed team's per_person_usd, and the team's and the org's monthly_usd,
    each with whether a person confirmed it where it was read.
"""
from __future__ import annotations

import math

import pytest

from finops import org
from finops.org import cli, model, store
from finops.org.model import Fact, FactError, OrgModel, Subject


def _budget(subject, value, **kw):
    return Fact.from_dict({"fact": "ai_budget", "subject": subject, "value": value,
                           "source": "human", "status": kw.pop("status", "confirmed"), **kw})


# ── the schema ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("subject", ["team:payments", "person:alice@acme.example", "org:org",
                                     "person:github:alice"])
def test_a_budget_on_a_team_a_person_or_the_org_validates(subject):
    f = _budget(subject, {"monthly_usd": 200})
    assert f.fact == "ai_budget" and f.value["monthly_usd"] == 200.0


@pytest.mark.parametrize("bad", [0, -5, "200", True, None, math.nan, math.inf])
def test_monthly_usd_must_be_a_number_above_zero_and_the_error_names_it(bad):
    with pytest.raises(FactError, match=r"value\.monthly_usd"):
        _budget("team:payments", {"monthly_usd": bad})
    with pytest.raises(FactError, match=r"value\.monthly_usd"):
        _budget("team:payments", {})


def test_per_person_usd_is_a_teams_alone_and_the_error_names_it():
    f = _budget("team:payments", {"monthly_usd": 5000, "per_person_usd": 400})
    assert f.value["per_person_usd"] == 400.0
    for subject in ("person:alice@acme.example", "org:org"):
        with pytest.raises(FactError, match=r"value\.per_person_usd"):
            _budget(subject, {"monthly_usd": 5000, "per_person_usd": 400})
    for bad in (0, -1, "x", math.nan):
        with pytest.raises(FactError, match=r"value\.per_person_usd"):
            _budget("team:payments", {"monthly_usd": 5000, "per_person_usd": bad})


@pytest.mark.parametrize("subject", ["aws_account:123456789012", "environment:prod",
                                     "repo_path:infra"])
def test_a_budget_about_anything_else_is_refused_naming_the_subject(subject):
    with pytest.raises(FactError, match=r"subject\.kind"):
        _budget(subject, {"monthly_usd": 200})


@pytest.mark.parametrize("ident", ["alice", "alice@", "@acme.example", "github:", "github:a b",
                                   "github:" + "a" * 40, "slack:alice"])
def test_a_person_is_an_email_or_a_github_login(ident):
    with pytest.raises(FactError, match=r"subject\.id"):
        _budget(f"person:{ident}", {"monthly_usd": 200})


def test_a_person_is_one_person_whatever_the_case():
    a = _budget("person:Alice@ACME.example", {"monthly_usd": 200})
    b = _budget("person:alice@acme.example", {"monthly_usd": 200})
    assert a.subject == b.subject == Subject("person", "alice@acme.example")
    assert a.key == b.key
    assert _budget("person:GitHub:Alice", {"monthly_usd": 1}).subject.id == "github:alice"


def test_only_an_ai_budget_may_be_about_a_person():
    with pytest.raises(FactError, match="person"):
        Fact.from_dict({"fact": "owner", "subject": "person:alice@acme.example",
                        "value": {"team": "payments"}, "source": "human"})


def test_budgets_live_in_budgets_yaml_and_nothing_else_does():
    assert model.FILE_FOR_KIND["ai_budget"] == "budgets.yaml"
    assert [k for k, v in model.FILE_FOR_KIND.items() if v == "budgets.yaml"] == ["ai_budget"]
    assert "budgets.yaml" in model.KNOWN_FILES
    assert model.KNOWN_FILES == tuple(sorted(model.KNOWN_FILES))


# ── reading and writing ──────────────────────────────────────────────────────

BUDGETS = """# nable org model v1
- fact: ai_budget
  subject: {kind: person, id: Alice@Acme.example}
  value: {monthly_usd: 150}
  source: human
  status: confirmed
- fact: ai_budget
  subject: {kind: team, id: payments}
  value: {monthly_usd: 5000, per_person_usd: 400}
  source: human
  status: confirmed
- fact: ai_budget
  subject: {kind: org, id: org}
  value: {monthly_usd: 20000}
  source: adapter
  status: proposed
"""
TEAMS = """# nable org model v1
- fact: team
  subject: {kind: team, id: payments}
  value: {people: [ALICE@acme.example, bob@acme.example]}
  source: human
  status: confirmed
- fact: team
  subject: {kind: team, id: search}
  value: {people: [carol@acme.example]}
  source: adapter
  status: proposed
"""


@pytest.fixture
def odir(tmp_path, monkeypatch):
    d = tmp_path / "org"
    d.mkdir()
    (d / "budgets.yaml").write_text(BUDGETS)
    (d / "teams.yaml").write_text(TEAMS)
    monkeypatch.setenv("FINOPS_ORG_DIR", str(d))
    monkeypatch.setattr(store, "_data_dir", lambda: tmp_path / "data")
    for var in ("FINOPS_TAG_RULES", "FINOPS_ACCOUNTS_FILE", "FINOPS_REQUIRED_TAGS",
                "FINOPS_PROTECTED_TAGS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return d


def test_the_loader_reads_budgets_yaml(odir):
    m = org.load()
    assert m.warnings == []
    got = {(f.subject.kind, f.subject.id): (f.status, f.value) for f in m.by_kind("ai_budget")}
    assert got == {("person", "alice@acme.example"): ("confirmed", {"monthly_usd": 150.0}),
                   ("team", "payments"): ("confirmed", {"monthly_usd": 5000.0,
                                                        "per_person_usd": 400.0}),
                   ("org", "org"): ("proposed", {"monthly_usd": 20000.0})}
    assert all(f.file == f"{odir}/budgets.yaml" for f in m.by_kind("ai_budget"))


def test_the_0_13_0_loader_ignores_budgets_yaml_and_reads_the_rest(odir, monkeypatch):
    """0.13.0 reads its eight files and warns about any other YAML file. Its
    file list, put back, finds the team and leaves budgets.yaml alone: no
    ai_budget fact, no person subject, nothing of it parsed."""
    old = ("accounts.yaml", "approvals.yaml", "environments.yaml", "freezes.yaml",
           "owners.yaml", "policy.yaml", "tags.yaml", "teams.yaml")
    monkeypatch.setattr(store, "KNOWN_FILES", old)
    m = org.load()
    assert m.by_kind("ai_budget") == []
    assert {f.subject.id for f in m.by_kind("team")} == {"payments", "search"}
    assert any(w.startswith("budgets.yaml: not an org model file") for w in m.warnings)


def test_a_budget_round_trips_through_the_store(odir):
    m = org.load()
    text = (odir / "budgets.yaml").read_text()
    f = next(f for f in m.by_kind("ai_budget") if f.subject.kind == "org")
    from finops.org.store import _MINT, HumanDecision
    store.confirm(f.key, HumanDecision("@lead", "--as", _token=_MINT))
    again = org.load()
    assert [g.status for g in again.by_kind("ai_budget") if g.subject.kind == "org"] == [
        "confirmed"]
    assert (odir / "budgets.yaml").read_text() != text
    assert not (odir / "owners.yaml").exists() or "ai_budget" not in (
        odir / "owners.yaml").read_text()


# ── `nable org set ai_budget` ────────────────────────────────────────────────

@pytest.fixture
def cli_dir(tmp_path, monkeypatch):
    d = tmp_path / "orgdir"
    monkeypatch.setenv("FINOPS_ORG_DIR", str(d))
    monkeypatch.setattr(store, "_data_dir", lambda: tmp_path / "data")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for var in ("FINOPS_TAG_RULES", "FINOPS_ACCOUNTS_FILE", "FINOPS_REQUIRED_TAGS",
                "FINOPS_PROTECTED_TAGS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "_is_tty", lambda: False)
    monkeypatch.setattr(cli, "_git_email", lambda: None)
    return d


def _run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_a_person_sets_a_budget_with_as(cli_dir, capsys):
    code, out, err = _run(capsys, "set", "ai_budget", "--subject", "person:Alice@Acme.example",
                          "--monthly-usd", "150", "--as", "@lead")
    assert code == 0, err
    (f,) = org.load().by_kind("ai_budget")
    assert (f.subject.id, f.value, f.status, f.confirmed_by, f.source) == (
        "alice@acme.example", {"monthly_usd": 150.0}, "confirmed", "@lead", "human")
    assert (cli_dir / "budgets.yaml").exists()
    assert f.key in out and "list-price" in out


def test_a_team_budget_takes_per_person_usd(cli_dir, capsys):
    code, _, err = _run(capsys, "set", "ai_budget", "--subject", "team:payments",
                        "--monthly-usd", "5000", "--per-person-usd", "400", "--as", "@lead")
    assert code == 0, err
    (f,) = org.load().by_kind("ai_budget")
    assert f.value == {"monthly_usd": 5000.0, "per_person_usd": 400.0}


def test_on_a_terminal_the_git_email_records_it(cli_dir, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_git_email", lambda: "maria@acme.example")
    assert _run(capsys, "set", "ai_budget", "--subject", "org:org", "--monthly-usd",
                "20000")[0] == 0
    assert org.load().by_kind("ai_budget")[0].confirmed_by == "maria@acme.example"


def test_without_a_terminal_or_as_setting_a_budget_is_refused(cli_dir, capsys):
    code, _, err = _run(capsys, "set", "ai_budget", "--subject", "team:payments",
                        "--monthly-usd", "5000")
    assert code == 2 and "--as" in err
    assert not (cli_dir / "budgets.yaml").exists()


@pytest.mark.parametrize("argv,field", [
    (["--subject", "team:payments"], "monthly"),
    (["--subject", "team:payments", "--monthly-usd", "0"], "monthly_usd"),
    (["--subject", "person:alice@acme.example", "--monthly-usd", "10",
      "--per-person-usd", "5"], "per_person_usd"),
    (["--subject", "aws_account:123456789012", "--monthly-usd", "10"], "subject.kind"),
    (["--monthly-usd", "10"], "--subject"),
])
def test_a_budget_that_does_not_fit_is_refused_naming_the_field(cli_dir, capsys, argv, field):
    code, _, err = _run(capsys, "set", "ai_budget", *argv, "--as", "@lead")
    assert code in (1, 2) and field in err, err
    assert org.load().by_kind("ai_budget") == []


# ── which budgets apply to a person ──────────────────────────────────────────

def _model(*facts: Fact, trusted: bool = True) -> OrgModel:
    for f in facts:
        f.trusted = trusted
        f.origin = model.FILE_FOR_KIND[f.fact]
        f.src_dir = "/org"
    return OrgModel(list(facts))


def _team(tid, people, status="confirmed"):
    return Fact.from_dict({"fact": "team", "subject": f"team:{tid}", "value": {"people": people},
                           "source": "human", "status": status})


def _scopes(hits):
    return {(h["scope"], h["subject"]): (h["usd"], h["sure"]) for h in hits}


def test_a_persons_own_budget_answers_before_their_teams_per_person():
    m = _model(_budget("person:alice@acme.example", {"monthly_usd": 150}),
               _budget("team:payments", {"monthly_usd": 5000, "per_person_usd": 400}),
               _budget("org:org", {"monthly_usd": 20000}),
               _team("payments", ["Alice@Acme.example"]))
    assert _scopes(m.ai_budgets_for("ALICE@acme.example")) == {
        ("person", "person:alice@acme.example"): (150.0, True),
        ("team", "team:payments"): (5000.0, True),
        ("org", "org:org"): (20000.0, True)}


def test_without_a_person_budget_the_teams_per_person_usd_applies():
    m = _model(_budget("team:payments", {"monthly_usd": 5000, "per_person_usd": 400}),
               _team("payments", ["bob@acme.example"]))
    assert _scopes(m.ai_budgets_for("bob@acme.example")) == {
        ("per_person", "team:payments"): (400.0, True),
        ("team", "team:payments"): (5000.0, True)}


def test_a_proposed_personal_budget_leaves_the_teams_confirmed_per_person_in_force():
    """A proposal only asks: it never displaces a budget a person confirmed,
    so the team's per_person_usd still applies, and the stricter answers."""
    m = _model(_budget("person:alice@acme.example", {"monthly_usd": 1000}, status="proposed"),
               _budget("team:payments", {"monthly_usd": 100, "per_person_usd": 4}),
               _team("payments", ["alice@acme.example"]))
    assert _scopes(m.ai_budgets_for("alice@acme.example")) == {
        ("person", "person:alice@acme.example"): (1000.0, False),
        ("per_person", "team:payments"): (4.0, True),
        ("team", "team:payments"): (100.0, True)}


def test_an_untrusted_personal_budget_leaves_the_teams_per_person_in_force_too():
    own = _budget("person:alice@acme.example", {"monthly_usd": 1000})
    m = _model(_budget("team:payments", {"monthly_usd": 100, "per_person_usd": 4}),
               _team("payments", ["alice@acme.example"]))
    m = OrgModel([*m.facts, _model(own, trusted=False).facts[0]])
    hits = _scopes(m.ai_budgets_for("alice@acme.example"))
    assert hits[("person", "person:alice@acme.example")] == (1000.0, False)
    assert hits[("per_person", "team:payments")] == (4.0, True)


def test_a_proposed_budget_or_membership_is_never_sure_and_a_proposed_team_names_nobody():
    m = _model(_budget("team:search", {"monthly_usd": 100, "per_person_usd": 10}),
               _team("search", ["carol@acme.example"], status="proposed"),
               _budget("org:org", {"monthly_usd": 20000}, status="proposed"))
    assert _scopes(m.ai_budgets_for("carol@acme.example")) == {
        ("org", "org:org"): (20000.0, False)}


def test_an_untrusted_repos_budget_is_never_sure():
    m = _model(_budget("person:alice@acme.example", {"monthly_usd": 1}), trusted=False)
    assert _scopes(m.ai_budgets_for("alice@acme.example")) == {
        ("person", "person:alice@acme.example"): (1.0, False)}


def test_nobody_known_still_meets_the_orgs_budget():
    m = _model(_budget("org:org", {"monthly_usd": 20000}),
               _budget("person:alice@acme.example", {"monthly_usd": 1}))
    assert _scopes(m.ai_budgets_for(None)) == {("org", "org:org"): (20000.0, True)}


def test_each_hit_names_its_file_and_key():
    f = _budget("person:alice@acme.example", {"monthly_usd": 150})
    m = _model(f)
    (hit,) = m.ai_budgets_for("alice@acme.example")
    assert hit["key"] == f.key and hit["file"] == "/org/budgets.yaml"
