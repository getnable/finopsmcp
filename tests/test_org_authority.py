"""Purchase authority in the org model: nable.org/authority.yaml (D134, D135).

An authority fact says what a paid workspace may do about one purchase kind
(an action_class subject: rds_ri, elasticache_ri or database_savings_plan):
its rung (off, recommend or approve_each), the most one plan may commit to
it, who may approve it (email: entries) and when the entry expires. The file
only narrows the workspace's opt-in. What has to hold:

  - the schema refuses what does not fit, and the refusal names the field;
  - a person writes an entry through `nable org set authority`, at a
    terminal or with --as; without either it is refused, and no adapter,
    pack or agent proposal may write one;
  - every live entry counts, a proposal included; per class the strictest
    answers; a rejected entry counts for nothing; an expired one holds its
    class at recommend; an empty approver intersection is nobody;
  - a refused entry is never skipped in silence: it holds its class, or
    every class when its class cannot be read, at recommend, and `nable org
    authority` and `nable org status` show it with its file, entry and field;
  - the shared corpus (tests/org_authority_corpus/, byte-identical in the
    paid workspace's tests) reads exactly as expected.json says, which is
    what the workspace's own parser is held to.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
from datetime import date
from pathlib import Path

import pytest

from finops import org
from finops.org import cli, model, store
from finops.org.model import Fact, FactError

CORPUS = Path(__file__).resolve().parent / "org_authority_corpus"
TODAY = date(2026, 10, 22)


@pytest.fixture(autouse=True)
def _frozen_today(monkeypatch):
    """Every date this module's code reads is TODAY, whatever the clock says."""
    monkeypatch.setattr(model, "local_today", lambda: TODAY)
    monkeypatch.setattr(store, "local_today", lambda: TODAY)
    monkeypatch.setattr(model, "utc_today", lambda: TODAY)


def _entry(subject="action_class:rds_ri", value=None, **kw):
    raw = {"fact": "authority", "subject": subject,
           "value": value if value is not None else {"rung": "recommend"},
           "source": "human", "status": kw.pop("status", "confirmed"),
           "confirmed_at": kw.pop("confirmed_at", "2026-10-01"), **kw}
    return Fact.from_dict(raw)


def _each(**over):
    v = {"rung": "approve_each", "approvers": ["email:ana@acme.example"],
         "expires": "2027-06-30"}
    v.update(over)
    return v


# ── the schema ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cls", model.AUTHORITY_CLASSES)
@pytest.mark.parametrize("rung", model.RUNGS)
def test_each_class_and_rung_validates(cls, rung):
    value = _each(rung=rung) if rung == "approve_each" else {"rung": rung}
    f = _entry(f"action_class:{cls}", value)
    assert (f.subject.kind, f.subject.id, f.value["rung"]) == ("action_class", cls, rung)
    assert model.FILE_FOR_KIND["authority"] == "authority.yaml"
    assert "authority.yaml" in model.KNOWN_FILES


@pytest.mark.parametrize("subject,field", [
    ("action_class:rds_rii", "subject.id"), ("action_class:ec2", "subject.id"),
    ("team:payments", "subject.kind"), ("org:org", "subject.kind"),
    ("person:ana@acme.example", "subject.kind"),
])
def test_an_unknown_class_or_another_subject_is_refused_naming_it(subject, field):
    with pytest.raises(FactError) as e:
        _entry(subject)
    assert e.value.field == field and str(e.value).startswith(field)


def test_only_an_authority_fact_may_be_about_an_action_class():
    with pytest.raises(FactError) as e:
        Fact.from_dict({"fact": "owner", "subject": "action_class:rds_ri",
                        "value": {"team": "payments"}})
    assert e.value.field == "subject.kind"


@pytest.mark.parametrize("rung", ["always", "approve", "", None, True, "OFF", 2])
def test_an_unknown_rung_is_refused_naming_value_rung(rung):
    with pytest.raises(FactError) as e:
        _entry(value={"rung": rung})
    assert e.value.field == "value.rung"


def test_off_unquoted_which_yaml_reads_as_false_is_off():
    assert _entry(value={"rung": False}).value["rung"] == "off"


@pytest.mark.parametrize("bad", [-1, -0.01, math.nan, math.inf, -math.inf, "5000", True])
def test_a_negative_nan_or_infinite_bound_is_refused_naming_it(bad):
    with pytest.raises(FactError) as e:
        _entry(value={"rung": "recommend", "max_commitment_usd_per_plan": bad})
    assert e.value.field == "value.max_commitment_usd_per_plan"


def test_a_bound_of_zero_or_more_is_kept_as_a_float():
    assert _entry(value={"rung": "recommend",
                         "max_commitment_usd_per_plan": 0}).value[
        "max_commitment_usd_per_plan"] == 0.0
    assert _entry(value=_each(max_commitment_usd_per_plan=50000)).value[
        "max_commitment_usd_per_plan"] == 50000.0


@pytest.mark.parametrize("approvers", [["github:ana"], ["ana@acme.example"], ["team:platform"],
                                       ["email:not an address"], ["email:"], [7], {"a": 1}])
def test_a_non_email_approver_is_refused_naming_value_approvers(approvers):
    with pytest.raises(FactError) as e:
        _entry(value=_each(approvers=approvers))
    assert e.value.field == "value.approvers"


def test_approvers_are_email_entries_lowercased_each_once():
    f = _entry(value=_each(approvers=["Email:Ana@ACME.example", "email:ana@acme.example",
                                      " email: bo@acme.example "]))
    assert f.value["approvers"] == ["email:ana@acme.example", "email:bo@acme.example"]


@pytest.mark.parametrize("who", ["email:\u212aate@example.com", "email:ana@\u0430cme.example",
                                 "email:J\u00f6rg@example.com"])
def test_an_approver_that_is_not_plain_ascii_is_refused(who):
    """str.lower maps KELVIN SIGN onto k: a name a person may approve by is
    plain ASCII, so it can never be lowercased onto another mailbox."""
    with pytest.raises(FactError) as e:
        _entry(value=_each(approvers=[who]))
    assert e.value.field == "value.approvers"


@pytest.mark.parametrize("key", ["max_commitment_usd", "approver", "expiry", "note"])
def test_a_value_field_the_schema_does_not_name_is_refused_never_kept(key):
    """A misspelt bound kept and ignored would let a plan over it be
    approved: refused, naming value.<key>, it holds its class at recommend."""
    with pytest.raises(FactError) as e:
        _entry(value=_each(**{key: 1000}))
    assert e.value.field == f"value.{key}"


def test_a_key_twice_in_a_mapping_makes_authority_yaml_unreadable(tmp_path):
    """YAML keeps the last of two values in silence (rung off, then
    approve_each): authority.yaml refuses it, and holds every class."""
    d = tmp_path / "org"
    d.mkdir()
    (d / "authority.yaml").write_text(
        "# nable org model v1\n- fact: authority\n  subject: action_class:rds_ri\n"
        "  value: {rung: \"off\", approvers: ['email:ana@acme.example'], expires: 2027-06-30, "
        "rung: approve_each}\n  status: confirmed\n  confirmed_at: 2026-10-01\n")
    a = store.load(d, legacy=False).authority(today=TODAY)
    assert [(r["entry"], r["field"], r["holds"]) for r in a["refused"]] == [
        (None, "file", "every class")]
    assert "duplicate" in a["refused"][0]["message"]
    assert {c: v["rung"] for c, v in a["classes"].items()} == dict.fromkeys(
        model.AUTHORITY_CLASSES, "recommend")
    # The other org files keep YAML's own reading of a key twice.
    (d / "authority.yaml").unlink()
    (d / "teams.yaml").write_text("# nable org model v1\n- fact: team\n  subject: team:a\n"
                                  "  value: {name: a, name: b}\n  status: confirmed\n")
    assert store.load(d, legacy=False).by_kind("team")


@pytest.mark.parametrize("anchor", ["confirmed_at", "proposed_at"])
def test_an_entry_dated_after_today_is_not_in_force_before_its_date(anchor):
    """confirmed_at 2030-07-01, expires 2031-06-30: counted from its own
    date, it would hold approve_each for years. It is in force from that
    date (a day early for a laptop ahead of UTC), so a year is a year."""
    status = "confirmed" if anchor == "confirmed_at" else "proposed"
    raw = {"fact": "authority", "subject": "action_class:rds_ri",
           "value": _each(expires="2031-06-30"), "source": "human", "status": status,
           anchor: "2030-07-01"}
    f = Fact.from_dict(raw)
    got = _model(f).authority(today=TODAY)["classes"]["rds_ri"]
    assert got["rung"] == "recommend" and got["expires"] == "2031-06-30"
    assert any("2030-07-01" in h for h in got["held"])
    assert _model(f).authority(today=date(2030, 6, 30))["classes"]["rds_ri"]["rung"] == \
        "approve_each"
    assert _model(f).authority(today=date(2030, 6, 29))["classes"]["rds_ri"]["rung"] == \
        "recommend"


@pytest.mark.parametrize("missing,field", [("approvers", "value.approvers"),
                                           ("expires", "value.expires")])
def test_approve_each_needs_approvers_and_an_expiry(missing, field):
    v = _each()
    del v[missing]
    with pytest.raises(FactError) as e:
        _entry(value=v)
    assert e.value.field == field
    with pytest.raises(FactError) as e:
        _entry(value=_each(approvers=[]))
    assert e.value.field == "value.approvers"


@pytest.mark.parametrize("expires", [
    "2026-10-01",            # the day it was confirmed: not after it
    "2026-09-30",            # before it
    "2027-10-03",            # 367 days after it
    "2027-02-30", "someday", "2027-1-1",
])
def test_an_expiry_out_of_range_is_refused_naming_value_expires(expires):
    with pytest.raises(FactError) as e:
        _entry(value=_each(expires=expires), confirmed_at="2026-10-01")
    assert e.value.field == "value.expires"


def test_an_expiry_up_to_366_days_after_confirmation_is_kept():
    assert _entry(value=_each(expires="2027-10-02"),
                  confirmed_at="2026-10-01").value["expires"] == "2027-10-02"
    assert _entry(value=_each(expires=date(2027, 1, 1))).value["expires"] == "2027-01-01"


def test_a_proposal_is_counted_from_its_proposed_at():
    f = _entry(value=_each(expires="2027-01-01"), status="proposed", confirmed_at=None,
               proposed_at="2026-10-05")
    assert f.status == "proposed"
    with pytest.raises(FactError) as e:
        _entry(value=_each(expires="2027-01-01"), status="proposed", confirmed_at=None)
    assert e.value.field == "value.expires"


def test_a_rejected_entry_is_never_refused_for_its_dates():
    f = _entry(value=_each(expires="2026-01-01"), status="rejected",
               confirmed_at="2026-10-05")
    assert f.status == "rejected"


def test_authority_facts_are_authority_yamls_alone():
    with pytest.raises(FactError) as e:
        Fact.from_dict({"fact": "authority", "subject": "action_class:rds_ri",
                        "value": {"rung": "off"}}, origin="owners.yaml")
    assert e.value.field == "fact"
    with pytest.raises(FactError) as e:
        Fact.from_dict({"fact": "owner", "subject": "aws_account:123456789012",
                        "value": {"team": "x"}}, origin="authority.yaml")
    assert e.value.field == "fact"


# ── the strictest of every live entry ────────────────────────────────────────

def _model(*facts: Fact, refused=()) -> model.OrgModel:
    for f in facts:
        f.origin, f.src_dir = "authority.yaml", "/org"
    return model.OrgModel(list(facts), refused=list(refused))


def test_two_entries_give_the_lowest_rung_smallest_bound_common_approvers_earliest_expiry():
    a = _entry(value=_each(max_commitment_usd_per_plan=50000,
                           approvers=["email:ana@acme.example", "email:bo@acme.example"],
                           expires="2027-06-30"))
    b = _entry(value=_each(max_commitment_usd_per_plan=20000,
                           approvers=["email:BO@acme.example", "email:cy@acme.example"],
                           expires="2027-03-01"), status="proposed", confirmed_at=None,
               proposed_at="2026-10-05")
    c = _entry(value={"rung": "recommend"}, status="proposed", confirmed_at=None,
               proposed_at="2026-10-06")
    got = _model(a, b).authority(today=TODAY)["classes"]["rds_ri"]
    assert got["rung"] == "approve_each"
    assert got["max_commitment_usd_per_plan"] == 20000.0
    assert got["approvers"] == ["email:bo@acme.example"]
    assert got["expires"] == "2027-03-01" and got["expired_on"] is None
    assert sorted(got["keys"]) == sorted([a.key, b.key])
    assert _model(a, b, c).authority(today=TODAY)["classes"]["rds_ri"]["rung"] == "recommend"


def test_a_rejected_entry_counts_for_nothing_and_a_proposed_one_counts():
    each = _entry(value=_each())
    off = _entry(value={"rung": "off"}, status="rejected")
    assert _model(each, off).authority(today=TODAY)["classes"]["rds_ri"]["rung"] == \
        "approve_each"
    off = _entry(value={"rung": "off"}, status="proposed", confirmed_at=None,
                 proposed_at="2026-10-05")
    assert _model(each, off).authority(today=TODAY)["classes"]["rds_ri"]["rung"] == "off"


def test_an_expired_entry_holds_its_class_at_recommend_saying_expired_on():
    f = _entry(value=_each(expires="2026-10-31"), confirmed_at="2026-01-15")
    before = _model(f).authority(today=date(2026, 10, 30))["classes"]["rds_ri"]
    assert before["rung"] == "approve_each" and before["expired_on"] is None
    on = _model(f).authority(today=date(2026, 10, 31))["classes"]["rds_ri"]
    assert on["rung"] == "recommend" and on["expired_on"] == "2026-10-31"
    assert any("expired on 2026-10-31" in h for h in on["held"])
    # An expired off entry stays off: an expiry never widens.
    off = _entry(value={"rung": "off", "expires": "2026-10-31"}, confirmed_at="2026-01-15")
    assert _model(off).authority(today=date(2027, 1, 1))["classes"]["rds_ri"]["rung"] == "off"


def test_an_empty_intersection_reads_as_nobody_may_approve():
    a = _entry(value=_each(approvers=["email:ana@acme.example"]))
    b = _entry(value=_each(approvers=["email:bo@acme.example"]), confirmed_at="2026-10-02")
    got = _model(a, b).authority(today=TODAY)["classes"]["rds_ri"]
    assert got["approvers"] == [] and got["nobody_may_approve"] is True


def test_a_class_the_file_does_not_name_is_the_opt_ins_alone():
    got = _model(_entry(value=_each())).authority(today=TODAY)["classes"]
    for cls in ("elasticache_ri", "database_savings_plan"):
        assert got[cls]["named"] is False and got[cls]["rung"] is None
    assert model.OrgModel([]).authority(today=TODAY)["files"] == []


def test_a_refused_entry_holds_its_class_or_every_class_at_recommend():
    each = _entry(value=_each())
    mine = model.refusal("/org/authority.yaml", 3, {"fact": "authority",
                                                    "subject": "action_class:rds_ri",
                                                    "value": {"rung": "x"}},
                         FactError("value.rung must be one of off", field="value.rung"))
    assert mine["holds"] == "rds_ri"
    got = _model(each, refused=[mine]).authority(today=TODAY)
    assert got["classes"]["rds_ri"]["rung"] == "recommend"
    assert got["classes"]["elasticache_ri"]["rung"] is None
    assert got["refused"][0] | {"message": ""} == {
        "file": "/org/authority.yaml", "entry": 3, "field": "value.rung", "message": "",
        "class": "rds_ri", "holds": "rds_ri"}
    typo = model.refusal("/org/authority.yaml", 4, {"fact": "authority",
                                                    "subject": "action_class:rds_rii"},
                         FactError("subject.id 'rds_rii' is not", field="subject.id"))
    got = _model(each, refused=[typo]).authority(today=TODAY)["classes"]
    assert {c: v["rung"] for c, v in got.items()} == {
        "rds_ri": "recommend", "elasticache_ri": "recommend",
        "database_savings_plan": "recommend"}


# ── the corpus the paid workspace's parser is held to ────────────────────────

def _expected() -> dict:
    """expected.json, with the prefixes it writes so that no line is a bare
    hex string (a secret scanner's false alarm) taken off again."""
    doc = json.loads((CORPUS / "expected.json").read_text())
    for want in doc["files"].values():
        want["sha256"] = want["sha256"].removeprefix("sha256:")
        for c in want["classes"].values():
            c["keys"] = [k.removeprefix("key:") for k in c["keys"]]
    return doc


def _read_corpus_file(tmp_path: Path, name: str) -> dict:
    d = tmp_path / name.removesuffix(".yaml")
    d.mkdir()
    shutil.copy(CORPUS / name, d / "authority.yaml")
    m = store.load(d, legacy=False)
    a = m.authority(today=date.fromisoformat(_expected()["today"]))
    return {"refused": [{"entry": r["entry"], "field": r["field"], "holds": r["holds"]}
                        for r in a["refused"]],
            "classes": {c: {k: v[k] for k in ("rung", "max_commitment_usd_per_plan",
                                              "approvers", "nobody_may_approve", "expires",
                                              "expired_on", "keys")}
                        for c, v in a["classes"].items()}}


def test_the_corpus_files_are_the_ones_expected_json_was_made_from():
    files = _expected()["files"]
    assert sorted(files) == sorted(p.name for p in CORPUS.glob("*.yaml"))
    for name, want in files.items():
        assert hashlib.sha256((CORPUS / name).read_bytes()).hexdigest() == want["sha256"], name


@pytest.mark.parametrize("name", sorted(p.name for p in CORPUS.glob("*.yaml")))
def test_the_core_reads_each_corpus_file_as_expected(tmp_path, monkeypatch, name):
    monkeypatch.delenv("FINOPS_ORG_DIR", raising=False)
    want = _expected()["files"][name]
    got = _read_corpus_file(tmp_path, name)
    assert got == {"refused": want["refused"], "classes": want["classes"]}


def test_a_date_yaml_cannot_construct_makes_the_file_unreadable_never_a_crash(tmp_path):
    """Kept out of the corpus directory: 2027-02-30 stops a YAML scanner too."""
    d = tmp_path / "org"
    d.mkdir()
    (d / "authority.yaml").write_text(
        "# nable org model v1\n- fact: authority\n  subject: action_class:rds_ri\n"
        "  value: {rung: approve_each, approvers: ['email:ana@acme.example'], "
        "expires: 2027-02-30}\n  status: confirmed\n  confirmed_at: 2026-10-01\n")
    a = store.load(d, legacy=False).authority(today=TODAY)
    assert [(r["entry"], r["field"], r["holds"]) for r in a["refused"]] == [
        (None, "file", "every class")]
    assert "day is out of range" in a["refused"][0]["message"]
    assert {c: v["rung"] for c, v in a["classes"].items()} == dict.fromkeys(
        model.AUTHORITY_CLASSES, "recommend")


def test_a_refusal_comes_back_the_same_from_the_parse_cache(tmp_path):
    d = tmp_path / "org"
    d.mkdir()
    shutil.copy(CORPUS / "07_bad_rung_readable_class.yaml", d / "authority.yaml")
    first = store.load(d, legacy=False).authority(today=TODAY)
    again = store.load(d, legacy=False).authority(today=TODAY)
    assert first == again and first["refused"][0]["field"] == "value.rung"


def test_a_refused_entry_is_surfaced_in_the_warning_as_holding_its_class(tmp_path):
    d = tmp_path / "org"
    d.mkdir()
    shutil.copy(CORPUS / "06_typo_class.yaml", d / "authority.yaml")
    m = store.load(d, legacy=False)
    assert any(re.match(r"^authority\.yaml\[0\]: skipped, ", w)
               and "holds every purchase kind at recommend" in w for w in m.warnings)
    assert m.refused and m.refused[0]["field"] == "subject.id"


# ── the store: who may write an entry ────────────────────────────────────────

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


def test_no_adapter_pack_or_agent_may_propose_an_entry(cli_dir):
    f = org.make_fact("authority", "action_class:rds_ri", {"rung": "off"}, source="agent:x")
    with pytest.raises(FactError) as e:
        org.propose(f)
    assert e.value.field == "fact"
    assert org.propose_many([f]) == ["invalid"]
    assert not (cli_dir / "authority.yaml").exists()


def test_init_does_not_make_an_authority_yaml(cli_dir):
    made = store.ensure_dir(cli_dir)
    assert "authority.yaml" not in {p.name for p in made}
    assert not (cli_dir / "authority.yaml").exists()


def test_a_late_confirmation_that_would_put_the_expiry_past_a_year_is_refused(cli_dir,
                                                                             monkeypatch):
    cli_dir.mkdir()
    (cli_dir / "authority.yaml").write_text(
        "# nable org model v1\n- fact: authority\n  subject: action_class:rds_ri\n"
        "  value: {rung: approve_each, approvers: ['email:ana@acme.example'], "
        "expires: 2026-12-01}\n  source: human\n  status: proposed\n"
        "  proposed_at: 2026-10-01\n")
    (f,) = org.load().by_kind("authority")
    late = date(2026, 12, 2)
    monkeypatch.setattr(store, "local_today", lambda: late)
    with pytest.raises(org.OrgError, match=r"value\.expires"):
        org.confirm(f.key, cli._who("@lead"))
    assert org.load().by_kind("authority")[0].status == "proposed"
    # Saying no is never refused for the dates.
    assert org.reject(f.key, cli._who("@lead")).status == "rejected"
    assert org.load().refused == []


# ── `nable org set authority` ────────────────────────────────────────────────

def _run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_set_with_as_writes_a_confirmed_entry_to_authority_yaml(cli_dir, capsys):
    code, out, err = _run(capsys, "set", "authority", "--class", "rds_ri", "--rung",
                          "approve_each", "--max-commitment-usd", "50000", "--approver",
                          "email:Ana@Acme.example", "--approver", "email:bo@acme.example",
                          "--expires", "2027-06-30", "--as", "@lead")
    assert code == 0, err
    (f,) = org.load().by_kind("authority")
    assert (f.subject.id, f.status, f.confirmed_by, f.confirmed_at, f.origin) == (
        "rds_ri", "confirmed", "@lead", TODAY.isoformat(), "authority.yaml")
    assert f.value == {"rung": "approve_each", "max_commitment_usd_per_plan": 50000.0,
                       "approvers": ["email:ana@acme.example", "email:bo@acme.example"],
                       "expires": "2027-06-30"}
    assert (cli_dir / "authority.yaml").exists() and f.key in out and "only narrows" in out
    # Read back by the loader that wrote it, quoted as YAML must quote it.
    code, _, err = _run(capsys, "set", "authority", "--class", "elasticache_ri", "--rung",
                        "off", "--as", "@lead")
    assert code == 0, err
    off = org.load().resolve("authority", "action_class:elasticache_ri")
    assert off.value == {"rung": "off"} and org.load().refused == []


def test_set_without_a_terminal_or_as_is_refused(cli_dir, capsys):
    code, _, err = _run(capsys, "set", "authority", "--class", "rds_ri", "--rung", "off")
    assert code == 2 and "--as" in err
    assert not (cli_dir / "authority.yaml").exists()


@pytest.mark.parametrize("argv,field", [
    (["--class", "rds_rii", "--rung", "off"], "subject.id"),
    (["--class", "rds_ri", "--rung", "always"], "value.rung"),
    (["--class", "rds_ri", "--rung", "approve_each", "--approver", "github:ana",
      "--expires", "2027-01-01"], "value.approvers"),
    (["--class", "rds_ri", "--rung", "approve_each", "--expires", "2027-01-01"],
     "value.approvers"),
    (["--class", "rds_ri", "--rung", "approve_each", "--approver",
      "email:ana@acme.example"], "value.expires"),
    (["--class", "rds_ri", "--rung", "recommend", "--max-commitment-usd", "-5"],
     "value.max_commitment_usd_per_plan"),
    (["--class", "rds_ri", "--rung", "recommend", "--max-commitment-usd", "nan"],
     "value.max_commitment_usd_per_plan"),
    (["--class", "rds_ri", "--rung", "recommend", "--max-commitment-usd", "inf"],
     "value.max_commitment_usd_per_plan"),
    (["--class", "rds_ri", "--rung", "approve_each", "--approver", "email:ana@acme.example",
      "--expires", "2026-10-21"], "value.expires"),
    (["--class", "rds_ri", "--rung", "approve_each", "--approver", "email:ana@acme.example",
      "--expires", "2027-10-24"], "value.expires"),
    (["--rung", "off"], "--class"),
])
def test_each_refusal_names_its_field(cli_dir, capsys, argv, field):
    code, _, err = _run(capsys, "set", "authority", *argv, "--as", "@lead")
    assert code in (1, 2) and field in err, err
    assert not (cli_dir / "authority.yaml").exists()


def test_a_second_set_replaces_the_first_for_that_class(cli_dir, capsys):
    for bound in ("50000", "20000"):
        assert _run(capsys, "set", "authority", "--class", "rds_ri", "--rung", "recommend",
                    "--max-commitment-usd", bound, "--as", "@lead")[0] == 0
    live = [f for f in org.load().by_kind("authority") if f.live]
    assert [f.value["max_commitment_usd_per_plan"] for f in live] == [20000.0]


# ── `nable org authority` and `nable org status` ─────────────────────────────

def _corpus_into(cli_dir: Path, name: str) -> None:
    cli_dir.mkdir(exist_ok=True)
    shutil.copy(CORPUS / name, cli_dir / "authority.yaml")


def test_authority_shows_each_class_and_each_refused_entry(cli_dir, capsys):
    _corpus_into(cli_dir, "07_bad_rung_readable_class.yaml")
    code, out, _ = _run(capsys, "authority")
    assert code == 0
    assert "rds_ri" in out and "held at recommend" in out
    assert "elasticache_ri" in out and "approve_each" in out and "email:ana@acme.example" in out
    assert "database_savings_plan" in out and "opt-in alone governs it" in out
    assert "refused:" in out and "authority.yaml entry 0" in out and "value.rung" in out
    assert "only narrows" in out


def test_authority_json_is_the_models_answer(cli_dir, capsys):
    _corpus_into(cli_dir, "02_strictest_of_two.yaml")
    code, out, _ = _run(capsys, "authority", "--json")
    doc = json.loads(out)
    assert code == 0 and doc["today"] == TODAY.isoformat()
    rds = doc["classes"]["rds_ri"]
    assert (rds["rung"], rds["max_commitment_usd_per_plan"], rds["approvers"],
            rds["expires"]) == ("approve_each", 20000.5, ["email:bo@acme.example"],
                                "2027-03-01")
    assert len(rds["keys"]) == 2 and doc["refused"] == []


def test_authority_names_an_expired_entry_and_nobody_in_common(cli_dir, capsys):
    _corpus_into(cli_dir, "05_nobody_in_common.yaml")
    _, out, _ = _run(capsys, "authority")
    assert "nobody may approve" in out
    (cli_dir / "authority.yaml").write_text(
        "# nable org model v1\n- fact: authority\n  subject: action_class:rds_ri\n"
        "  value: {rung: approve_each, approvers: ['email:ana@acme.example'], "
        f"expires: {TODAY.isoformat()}}}\n  source: human\n  status: confirmed\n"
        "  confirmed_at: 2026-01-15\n")
    _, out, _ = _run(capsys, "authority")
    assert f"expired on {TODAY.isoformat()}" in out and "rds_ri                 recommend" in out


def test_status_has_an_authority_line_and_the_refusals(cli_dir, capsys):
    _corpus_into(cli_dir, "06_typo_class.yaml")
    code, out, _ = _run(capsys, "status")
    assert code == 0
    line = next(x for x in out.splitlines() if x.strip().startswith("authority ("))
    assert "rds_ri recommend" in line and "elasticache_ri recommend" in line
    assert "refused:" in out and "subject.id" in out and "holds every class" in out
    code, out, _ = _run(capsys, "status", "--json")
    doc = json.loads(out)
    assert doc["authority"]["refused"][0]["field"] == "subject.id"


def test_status_without_a_file_says_the_opt_in_alone_governs(cli_dir, capsys):
    _, out, _ = _run(capsys, "status")
    assert "no authority.yaml" in out and "opt-in alone governs" in out


def test_an_unreadable_file_holds_every_class_and_is_shown(cli_dir, capsys):
    _corpus_into(cli_dir, "11_not_yaml.yaml")
    code, out, _ = _run(capsys, "authority", "--json")
    doc = json.loads(out)
    assert code == 0
    assert {c: v["rung"] for c, v in doc["classes"].items()} == dict.fromkeys(
        model.AUTHORITY_CLASSES, "recommend")
    assert doc["refused"][0]["entry"] is None and doc["refused"][0]["field"] == "file"
