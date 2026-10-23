"""Classes that move no money in authority.yaml: tag, ticket and
offhours_schedule (D172, D173, D179).

They sit beside the purchase kinds in the same file and never change a
purchase kind's answer. What has to hold:

  - the purchase kinds are exactly as they were, and none of them takes
    `bounded`: that is refused naming value.rung;
  - each class takes its own bound fields, checked and named on refusal, and
    a field the class does not take is refused, never kept;
  - approvers and expires are required at approve_each and at bounded, and
    a bounded entry names its class's bound (tag_keys, max_per_day,
    environments);
  - per class the strictest live entry answers: the lowest rung and the
    narrowest bound;
  - only a confirmed entry in a trusted org model holds bounded: a
    proposal, or an entry from a repo nobody trusted, holds at most
    approve_each, so it only narrows;
  - an expired, dated-ahead or refused entry, or a file that cannot be
    read, holds a class that moves no money at approve_each, never bounded;
  - the class lists are disjoint, so no purchase kind can ever reach
    bounded through them.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import date
from pathlib import Path

import pytest

from finops.org import model, store
from finops.org.model import Fact, FactError, OrgModel

TODAY = date(2026, 10, 22)
APPROVER = "email:ana@acme.example"


@pytest.fixture(autouse=True)
def _frozen_today(monkeypatch):
    monkeypatch.setattr(model, "utc_today", lambda: TODAY)
    monkeypatch.setattr(model, "local_today", lambda: TODAY)


def _value(rung="approve_each", **bound):
    v = {"rung": rung}
    if rung in ("approve_each", "bounded"):
        v.update(approvers=[APPROVER], expires="2027-06-30")
    v.update(bound)
    return v


def _fact(cls, value, *, status="confirmed", trusted=True, **kw):
    raw = {"fact": "authority", "subject": {"kind": "action_class", "id": cls},
           "value": value, "source": "human", "status": status,
           ("confirmed_at" if status == "confirmed" else "proposed_at"):
               kw.pop("dated", "2026-10-01"), **kw}
    f = Fact.from_dict(raw, origin="authority.yaml")
    f.trusted = trusted
    return f


def _ladder(*facts, refused=()):
    return OrgModel(list(facts), refused=list(refused)).ladder(today=TODAY)["classes"]


def _refused_field(cls, value) -> str:
    with pytest.raises(FactError) as e:
        _fact(cls, value)
    return e.value.field


# ── the vocabulary ───────────────────────────────────────────────────────────

def test_the_purchase_kinds_and_their_rungs_are_unchanged():
    assert model.AUTHORITY_CLASSES == ("rds_ri", "elasticache_ri", "database_savings_plan")
    assert model.RUNGS == ("off", "recommend", "approve_each")
    assert model.NO_MONEY_CLASSES == ("tag", "ticket", "offhours_schedule")
    assert model.CLASS_RUNGS == ("off", "recommend", "approve_each", "bounded")


def test_the_classes_that_move_no_money_are_disjoint_from_every_purchase_kind():
    """The ratchet: bounded is reachable only through NO_MONEY_CLASSES, and
    no purchase kind is ever one of them."""
    assert not set(model.NO_MONEY_CLASSES) & set(model.AUTHORITY_CLASSES)
    assert "bounded" not in model.RUNGS
    for cls in model.AUTHORITY_CLASSES:
        assert cls not in model.CLASS_VALUE_KEYS


# ── the schema ───────────────────────────────────────────────────────────────

_BOUNDS = {"tag": {"tag_keys": ["team"]}, "ticket": {"max_per_day": 3},
           "offhours_schedule": {"environments": ["nonprod"]}}


@pytest.mark.parametrize("cls", model.NO_MONEY_CLASSES)
@pytest.mark.parametrize("rung", ["off", "recommend", "approve_each", "bounded"])
def test_each_class_takes_each_rung(cls, rung):
    v = _value(rung, **(_BOUNDS[cls] if rung == "bounded" else {}))
    f = _fact(cls, v)
    assert (f.subject.id, f.value["rung"]) == (cls, rung)


@pytest.mark.parametrize("cls", model.AUTHORITY_CLASSES)
def test_a_purchase_kind_at_bounded_is_refused_naming_value_rung(cls):
    with pytest.raises(FactError) as e:
        _fact(cls, {"rung": "bounded", "approvers": [APPROVER], "expires": "2027-06-30"})
    assert e.value.field == "value.rung"
    assert "purchase kind" in str(e.value)


@pytest.mark.parametrize("cls,value,field", [
    ("tag", _value("bounded", tag_keys=[]), "value.tag_keys"),
    ("tag", _value("bounded", tag_keys=[f"k{i}" for i in range(21)]), "value.tag_keys"),
    ("tag", _value("bounded", tag_keys=["team", 7]), "value.tag_keys"),
    ("tag", _value("bounded", tag_keys=["x" * 129]), "value.tag_keys"),
    ("tag", _value("bounded"), "value.tag_keys"),
    ("tag", _value("bounded", tag_keys=["team"], max_resources_per_call=0),
     "value.max_resources_per_call"),
    ("tag", _value("bounded", tag_keys=["team"], max_resources_per_call=101),
     "value.max_resources_per_call"),
    ("tag", _value("bounded", tag_keys=["team"], max_resources_per_call=2.0),
     "value.max_resources_per_call"),
    ("tag", _value("bounded", tag_keys=["team"], max_resources_per_call=True),
     "value.max_resources_per_call"),
    ("tag", _value("approve_each", max_commitment_usd_per_plan=10),
     "value.max_commitment_usd_per_plan"),
    ("tag", _value("approve_each", environments=["nonprod"]), "value.environments"),
    ("ticket", _value("bounded"), "value.max_per_day"),
    ("ticket", _value("bounded", max_per_day=0), "value.max_per_day"),
    ("ticket", _value("bounded", max_per_day=51), "value.max_per_day"),
    ("ticket", _value("bounded", max_per_day="3"), "value.max_per_day"),
    ("ticket", _value("bounded", max_per_day=3, repos=[]), "value.repos"),
    ("ticket", _value("bounded", max_per_day=3, repos=["not a repo"]), "value.repos"),
    ("ticket", _value("bounded", max_per_day=3, tag_keys=["team"]), "value.tag_keys"),
    ("offhours_schedule", _value("bounded"), "value.environments"),
    ("offhours_schedule", _value("bounded", environments=[]), "value.environments"),
    ("offhours_schedule", _value("bounded", environments=["nonprod", "prod"]),
     "value.environments"),
    ("offhours_schedule", _value("bounded", environments=["shared"]), "value.environments"),
    ("offhours_schedule", _value("bounded", environments=["unknown"]), "value.environments"),
    ("offhours_schedule", _value("bounded", environments=["staging"]), "value.environments"),
    ("offhours_schedule", _value("bounded", environments=["nonprod"],
                                 max_resources_per_call=51), "value.max_resources_per_call"),
    ("offhours_schedule", _value("bounded", environments=["nonprod"], windows=["night"]),
     "value.windows"),
    ("tag", {"rung": "approve_each", "expires": "2027-06-30"}, "value.approvers"),
    ("tag", {"rung": "bounded", "approvers": [APPROVER], "tag_keys": ["team"]},
     "value.expires"),
    ("ticket", {"rung": "bounded", "expires": "2027-06-30", "max_per_day": 3},
     "value.approvers"),
    ("tag", {"rung": "autonomous"}, "value.rung"),
])
def test_a_bad_bound_is_refused_naming_its_field(cls, value, field):
    assert _refused_field(cls, value) == field


def test_a_bound_is_kept_as_written_and_cleaned():
    f = _fact("offhours_schedule", _value("bounded", environments=["NonProd", "dr", "dr"],
                                          max_resources_per_call=5))
    assert f.value["environments"] == ["nonprod", "dr"]
    t = _fact("ticket", _value("bounded", max_per_day=3, repos="Acme/Payments"))
    assert t.value["repos"] == ["acme/payments"]
    g = _fact("tag", _value("bounded", tag_keys=["team", "Owner", "team"]))
    assert g.value["tag_keys"] == ["team", "Owner"]


def test_off_unquoted_reads_as_off_for_a_new_class_too():
    assert _fact("tag", {"rung": False}).value["rung"] == "off"


# ── the answer ───────────────────────────────────────────────────────────────

def test_a_class_the_file_does_not_name_has_no_rung():
    got = _ladder()
    assert set(got) == set(model.NO_MONEY_CLASSES)
    assert all(not c["named"] and c["rung"] is None for c in got.values())


def test_a_confirmed_trusted_entry_holds_bounded_with_its_bound():
    got = _ladder(_fact("tag", _value("bounded", tag_keys=["team"])))["tag"]
    assert got["rung"] == "bounded"
    assert got["bound"] == {"tag_keys": ["team"], "max_resources_per_call": 20}
    assert got["approvers"] == [APPROVER]


def test_the_defaults_are_each_class_own():
    sched = _ladder(_fact("offhours_schedule",
                          _value("bounded", environments=["nonprod"])))["offhours_schedule"]
    assert sched["bound"] == {"environments": ["nonprod"], "max_resources_per_call": 10}
    tick = _ladder(_fact("ticket", _value("bounded", max_per_day=3)))["ticket"]
    assert tick["bound"] == {"max_per_day": 3, "repos": None}


def test_per_class_the_strictest_entry_answers():
    a = _fact("tag", _value("bounded", tag_keys=["team", "env"], max_resources_per_call=50))
    b = _fact("tag", _value("bounded", tag_keys=["team", "owner"], max_resources_per_call=5,
                            approvers=[APPROVER, "email:bo@acme.example"]))
    got = _ladder(a, b)["tag"]
    assert got["rung"] == "bounded"
    assert got["bound"] == {"tag_keys": ["team"], "max_resources_per_call": 5}
    assert got["approvers"] == [APPROVER]
    low = _fact("tag", {"rung": "recommend"})
    assert _ladder(a, b, low)["tag"]["rung"] == "recommend"


def test_a_proposal_at_bounded_only_narrows():
    prop = _fact("tag", _value("bounded", tag_keys=["team"]), status="proposed")
    got = _ladder(prop)["tag"]
    assert got["rung"] == "approve_each"
    assert any("proposal" in h for h in got["held"])
    # Beside a confirmed bounded entry it still holds the class down.
    conf = _fact("tag", _value("bounded", tag_keys=["team"]))
    assert _ladder(conf, prop)["tag"]["rung"] == "approve_each"
    # A proposal at off narrows all the way.
    assert _ladder(conf, _fact("tag", {"rung": "off"}, status="proposed"))["tag"]["rung"] == "off"


def test_an_entry_from_an_untrusted_org_model_holds_at_most_approve_each():
    got = _ladder(_fact("ticket", _value("bounded", max_per_day=3), trusted=False))["ticket"]
    assert got["rung"] == "approve_each"
    assert any("trusted" in h for h in got["held"])


def test_an_expired_entry_holds_approve_each_never_bounded():
    f = _fact("tag", _value("bounded", tag_keys=["team"], expires="2026-10-22"),
              dated="2026-01-01")
    got = _ladder(f)["tag"]
    assert got["rung"] == "approve_each" and got["expired_on"] == "2026-10-22"
    assert any("expired" in h for h in got["held"])


def test_an_entry_dated_ahead_holds_approve_each():
    f = _fact("tag", _value("bounded", tag_keys=["team"], expires="2027-06-30"),
              dated="2026-10-30")
    assert _ladder(f)["tag"]["rung"] == "approve_each"


def test_a_refused_entry_holds_its_class_at_approve_each_and_nothing_else():
    raw = {"fact": "authority", "subject": {"kind": "action_class", "id": "tag"},
           "value": _value("bounded"), "status": "confirmed", "confirmed_at": "2026-10-01"}
    with pytest.raises(FactError) as e:
        Fact.from_dict(raw, origin="authority.yaml")
    r = model.refusal("authority.yaml", 0, raw, e.value)
    assert (r["class"], r["holds"], r["field"]) == ("tag", "tag", "value.tag_keys")
    ok = _fact("tag", _value("bounded", tag_keys=["team"]))
    rds = _fact("rds_ri", _value("approve_each"))
    m = OrgModel([ok, rds], refused=[r])
    assert m.ladder(today=TODAY)["classes"]["tag"]["rung"] == "approve_each"
    assert m.ladder(today=TODAY)["classes"]["ticket"]["rung"] is None
    # The purchase kinds read as if the tag entry were not there.
    assert m.authority(today=TODAY)["classes"] == OrgModel([rds]).authority(
        today=TODAY)["classes"]


def test_a_file_that_cannot_be_read_holds_every_named_or_unnamed_class_at_approve_each():
    whole = model.refusal("authority.yaml", None, None, "authority.yaml is not YAML")
    got = _ladder(refused=[whole])
    assert {c: v["rung"] for c, v in got.items()} == dict.fromkeys(model.NO_MONEY_CLASSES,
                                                                   "approve_each")


def test_a_class_that_moves_no_money_never_changes_a_purchase_kind_answer():
    rds = _fact("rds_ri", _value("approve_each", max_commitment_usd_per_plan=5000))
    news = [_fact("tag", _value("bounded", tag_keys=["team"])),
            _fact("ticket", {"rung": "off"}),
            _fact("offhours_schedule", _value("approve_each"), status="proposed")]
    alone = OrgModel([rds]).authority(today=TODAY)
    both = OrgModel([rds, *news]).authority(today=TODAY)
    assert both["classes"] == alone["classes"]
    assert set(both["classes"]) == set(model.AUTHORITY_CLASSES)


def test_a_typo_class_holds_every_class_purchase_kinds_at_recommend():
    raw = {"fact": "authority", "subject": {"kind": "action_class", "id": "tags"},
           "value": {"rung": "off"}, "status": "confirmed", "confirmed_at": "2026-10-01"}
    with pytest.raises(FactError) as e:
        Fact.from_dict(raw, origin="authority.yaml")
    assert e.value.field == "subject.id"
    r = model.refusal("authority.yaml", 0, raw, e.value)
    m = OrgModel([], refused=[r])
    assert {c: v["rung"] for c, v in m.authority(today=TODAY)["classes"].items()} == \
        dict.fromkeys(model.AUTHORITY_CLASSES, "recommend")
    assert {c: v["rung"] for c, v in m.ladder(today=TODAY)["classes"].items()} == \
        dict.fromkeys(model.NO_MONEY_CLASSES, "approve_each")


def test_the_fact_key_and_the_core_value_carry_the_bound():
    a = _fact("tag", _value("bounded", tag_keys=["team"]))
    b = _fact("tag", _value("bounded", tag_keys=["owner"]))
    assert a.key != b.key and a.said != b.said


# ── the corpus the paid workspace's parser is held to ────────────────────────

CORPUS = Path(__file__).resolve().parent / "org_authority_corpus"
LADDER_KEEP = ("rung", "bound", "approvers", "nobody_may_approve", "expires", "expired_on",
               "keys")


def _expected() -> dict:
    doc = json.loads((CORPUS / "expected.json").read_text())
    for want in doc["files"].values():
        for c in want["ladder"].values():
            c["keys"] = [k.removeprefix("key:") for k in c["keys"]]
    return doc


@pytest.mark.parametrize("name", sorted(p.name for p in CORPUS.glob("*.yaml")))
def test_the_core_reads_each_corpus_file_ladder_as_expected(tmp_path, monkeypatch, name):
    """expected.json's `ladder` for each file is what the core's loader makes
    of the classes that move no money; the workspace's parser is held to it
    too (tests/rds_ri/authority in the paid workspace)."""
    monkeypatch.delenv("FINOPS_ORG_DIR", raising=False)
    d = tmp_path / "org"
    d.mkdir()
    shutil.copy(CORPUS / name, d / "authority.yaml")
    doc = _expected()
    got = store.load(d, legacy=False).ladder(today=date.fromisoformat(doc["today"]))
    view = {c: {k: v[k] for k in LADDER_KEEP} for c, v in got["classes"].items()}
    assert view == doc["files"][name]["ladder"]


def _older_parse(path: Path) -> None:
    """Rewrite the parse cache entry for `path` as code from before the
    classes kept it: every class entry refused by subject.id, holding every
    class (its class unreadable to that code), with the version it wrote."""
    import yaml
    raw = json.loads(json.dumps(yaml.safe_load(path.read_text()), default=str))
    cached = store._cache_file(path)
    doc = json.loads(cached.read_text())
    keep = [i for i, r in enumerate(raw) if r["subject"]["id"] not in model.NO_MONEY_CLASSES]
    gone = [i for i in range(len(raw)) if i not in keep]
    why = ["subject.id: not one of the purchase kinds"] * len(gone)
    doc.update(v=3, facts=[doc["facts"][i] for i in keep],
               unparsed=[["raw", raw[i], []] for i in gone], why=why,
               warnings=[f"authority.yaml[{i}]: skipped, {w} (it holds every class at recommend)"
                         for i, w in zip(gone, why, strict=True)],
               refusals=[{"file": str(path), "entry": i, "field": "subject.id",
                          "message": why[0], "class": None, "holds": "every class"}
                         for i in gone])
    cached.write_text(json.dumps(doc))


def test_a_parse_made_before_the_classes_is_not_reused(tmp_path, monkeypatch):
    """A machine that read a teammate's authority.yaml before it knew the
    classes kept a parse that refused them, and so held every purchase kind
    at recommend. After the upgrade the same file must be read again, not
    answered from that parse."""
    monkeypatch.delenv("FINOPS_ORG_DIR", raising=False)
    d = tmp_path / "org"
    d.mkdir()
    shutil.copy(CORPUS / "28_classes_each_rung.yaml", d / "authority.yaml")
    assert not store.load(d, legacy=False).refused
    _older_parse(d / "authority.yaml")
    m = store.load(d, legacy=False)
    assert m.refused == []
    assert m.authority(today=TODAY)["classes"]["rds_ri"]["rung"] == "approve_each"
    assert m.ladder(today=TODAY)["classes"]["ticket"]["rung"] == "approve_each"


_FIRST_ENTRY = re.compile(r"^authority\.yaml\[0\]: skipped, ")


def test_a_refused_class_entry_warns_that_it_holds_approve_each(tmp_path, monkeypatch):
    monkeypatch.delenv("FINOPS_ORG_DIR", raising=False)
    d = tmp_path / "org"
    d.mkdir()
    shutil.copy(CORPUS / "30_class_bad_bounds.yaml", d / "authority.yaml")
    warned = [w for w in store.load(d, legacy=False).warnings if _FIRST_ENTRY.match(w)]
    assert len(warned) == 1 and warned[0].endswith("(it holds tag at approve_each)")
    assert not [w for w in store.load(d, legacy=False).warnings if "tag at recommend" in w]


def test_a_typo_class_warns_what_it_holds_for_each_kind_of_class(tmp_path, monkeypatch):
    monkeypatch.delenv("FINOPS_ORG_DIR", raising=False)
    d = tmp_path / "org"
    d.mkdir()
    shutil.copy(CORPUS / "06_typo_class.yaml", d / "authority.yaml")
    warned = [w for w in store.load(d, legacy=False).warnings if _FIRST_ENTRY.match(w)]
    assert len(warned) == 1 and warned[0].endswith(
        "(it holds every purchase kind at recommend and every class that moves no money "
        "at approve_each)")


def test_the_corpus_covers_each_class_at_each_rung_and_each_refusal():
    doc = _expected()
    rungs = {(c, v["rung"]) for f in doc["files"].values() for c, v in f["ladder"].items()}
    for cls in model.NO_MONEY_CLASSES:
        for rung in model.CLASS_RUNGS:
            assert (cls, rung) in rungs, (cls, rung)
    fields = {(r["field"], r["holds"]) for f in doc["files"].values() for r in f["refused"]}
    for want in (("value.rung", "rds_ri"), ("value.rung", "elasticache_ri"),
                 ("value.rung", "database_savings_plan"), ("value.tag_keys", "tag"),
                 ("value.environments", "offhours_schedule"), ("value.max_per_week", "ticket"),
                 ("value.max_per_day", "ticket"), ("value.approvers", "tag"),
                 ("value.max_commitment_usd_per_plan", "tag")):
        assert want in fields, want


# ── `nable org set authority`, `nable org authority` and `nable org status` ──

@pytest.fixture
def cli_dir(tmp_path, monkeypatch):
    from finops.org import cli
    d = tmp_path / "orgdir"
    monkeypatch.setenv("FINOPS_ORG_DIR", str(d))
    monkeypatch.setattr(store, "_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(store, "local_today", lambda: TODAY)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for var in ("FINOPS_TAG_RULES", "FINOPS_ACCOUNTS_FILE", "FINOPS_REQUIRED_TAGS",
                "FINOPS_PROTECTED_TAGS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "_is_tty", lambda: False)
    monkeypatch.setattr(cli, "_git_email", lambda: None)
    return d


def _run(capsys, *argv):
    from finops.org import cli
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def _live(cls):
    from finops import org
    return [f for f in org.load().by_kind("authority") if f.live and f.subject.id == cls]


def test_set_a_class_defaults_to_approve_each(cli_dir, capsys):
    code, out, err = _run(capsys, "set", "authority", "--class", "tag", "--approver", APPROVER,
                          "--expires", "2027-06-30", "--as", "@lead")
    assert code == 0, err
    (f,) = _live("tag")
    assert f.value == {"rung": "approve_each", "approvers": [APPROVER], "expires": "2027-06-30"}
    # A class moves no money: the guard judges it here and a workspace only
    # narrows it, and the purchase sentence is not said for it.
    assert f.status == "confirmed" and "can only narrow it" in out
    assert "approves every purchase" not in out


@pytest.mark.parametrize("argv,cls,value", [
    (["--class", "tag", "--rung", "bounded", "--tag-key", "team", "--tag-key", "env",
      "--max-resources", "5"], "tag",
     {"tag_keys": ["team", "env"], "max_resources_per_call": 5}),
    (["--class", "ticket", "--rung", "bounded", "--max-per-day", "3", "--repo",
      "Acme/Payments"], "ticket", {"max_per_day": 3, "repos": ["acme/payments"]}),
    (["--class", "offhours_schedule", "--rung", "bounded", "--environment", "nonprod",
      "--environment", "sandbox", "--max-resources", "4"], "offhours_schedule",
     {"environments": ["nonprod", "sandbox"], "max_resources_per_call": 4}),
])
def test_set_bounded_writes_each_class_bound(cli_dir, capsys, argv, cls, value):
    code, _, err = _run(capsys, "set", "authority", *argv, "--approver", APPROVER,
                        "--expires", "2027-06-30", "--as", "@lead")
    assert code == 0, err
    (f,) = _live(cls)
    assert f.value == {"rung": "bounded", "approvers": [APPROVER], "expires": "2027-06-30",
                       **value}


@pytest.mark.parametrize("argv,field", [
    (["--class", "rds_ri", "--rung", "bounded", "--approver", APPROVER, "--expires",
      "2027-06-30"], "value.rung"),
    (["--class", "rds_ri"], "--rung"),
    (["--class", "tag", "--rung", "bounded", "--approver", APPROVER, "--expires",
      "2027-06-30"], "value.tag_keys"),
    (["--class", "offhours_schedule", "--rung", "bounded", "--environment", "prod",
      "--approver", APPROVER, "--expires", "2027-06-30"], "value.environments"),
    (["--class", "ticket", "--rung", "bounded", "--max-per-day", "60", "--approver",
      APPROVER, "--expires", "2027-06-30"], "value.max_per_day"),
    (["--class", "tag", "--max-per-day", "3", "--approver", APPROVER, "--expires",
      "2027-06-30"], "value.max_per_day"),
    (["--class", "tag", "--max-commitment-usd", "5", "--approver", APPROVER, "--expires",
      "2027-06-30"], "value.max_commitment_usd_per_plan"),
])
def test_each_class_refusal_names_its_field(cli_dir, capsys, argv, field):
    code, _, err = _run(capsys, "set", "authority", *argv, "--as", "@lead")
    assert code in (1, 2) and field in err, err
    assert not (cli_dir / "authority.yaml").exists()


def test_set_a_class_without_a_terminal_or_as_is_refused(cli_dir, capsys):
    code, _, err = _run(capsys, "set", "authority", "--class", "tag", "--rung", "off")
    assert code == 2 and "--as" in err
    assert not (cli_dir / "authority.yaml").exists()


def test_authority_and_status_show_the_classes(cli_dir, capsys):
    cli_dir.mkdir()
    shutil.copy(CORPUS / "28_classes_each_rung.yaml", cli_dir / "authority.yaml")
    code, out, _ = _run(capsys, "authority")
    assert code == 0
    assert "Classes that move no money" in out
    assert "tag" in out and "bounded" in out and "tag keys env, team" in out
    assert "ticket" in out and "approve_each" in out
    assert "offhours_schedule" in out and "recommend" in out
    code, out, _ = _run(capsys, "authority", "--json")
    doc = json.loads(out)
    assert doc["ladder"]["classes"]["tag"]["rung"] == "bounded"
    assert set(doc["classes"]) == set(model.AUTHORITY_CLASSES)
    code, out, _ = _run(capsys, "status")
    assert "classes that move no money" in out and "tag bounded" in out
    code, out, _ = _run(capsys, "status", "--json")
    assert json.loads(out)["ladder"]["classes"]["ticket"]["rung"] == "approve_each"


def test_a_refused_class_entry_says_it_holds_approve_each(cli_dir, capsys):
    cli_dir.mkdir()
    shutil.copy(CORPUS / "30_class_bad_bounds.yaml", cli_dir / "authority.yaml")
    code, out, _ = _run(capsys, "authority")
    assert code == 0
    assert "holds tag at approve_each" in out
    assert "holds tag at recommend" not in out


def test_no_mcp_tool_writes_an_entry_for_a_class(cli_dir):
    """propose_org_fact is the one MCP tool that writes the org model: an
    authority entry is a person's to write, so it is refused for every
    class, and no tool confirms, promotes, reverses or lifts."""
    import asyncio

    from finops import server
    for cls in model.NO_MONEY_CLASSES:
        got = asyncio.run(server.propose_org_fact(
            fact="authority", subject_kind="action_class", subject_id=cls,
            value={"rung": "bounded", "tag_keys": ["team"], "approvers": [APPROVER],
                   "expires": "2027-06-30"}, source="agent:x"))
        assert got.get("result") in ("invalid", "refused"), got
    assert not (cli_dir / "authority.yaml").exists()
    names = {n for n in dir(server) if not n.startswith("_")}
    for word in ("promote", "reverse", "lift", "demote"):
        assert not [n for n in names if word in n], word
