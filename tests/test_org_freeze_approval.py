"""Freeze windows and approval chains as org model facts.

What has to stay true:
  - a freeze needs a start and an end that carry a UTC offset (a time
    without one could be any of 24 hours), a reason and a mode (ask or
    deny); it is kept as written and compared in UTC
  - an approval chain names action classes and approvers (github:, team:,
    jira:, linear:, email:), and `min` no more than the approvers it names
  - several freezes may hold for one subject (a slot per start), and every
    live one counts: a proposal or an untrusted repo's word never takes a
    confirmed freeze away, and only a sure (confirmed, trusted) one is sure
  - only confirmed approval chains name anybody
  - `nable org set freeze|approval` is the human path, reads a time without
    an offset in --tz or this machine's zone, refuses an action class nable
    does not know, and `nable org status` lists freezes and approvals
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from finops import org
from finops.org import cli
from finops.org.cli import _who as human
from finops.org.model import Fact, FactError, OrgModel, Subject, parse_when
from finops.org.store import safe_yaml

NOV27 = "2026-11-27T00:00:00-05:00"      # midnight in New York is 05:00 UTC
DEC01 = "2026-12-01T00:00:00-05:00"


def _freeze(subject="environment:prod", start=NOV27, end=DEC01, reason="Black Friday",
            mode="deny", status="confirmed", **kw):
    kind, _, ident = subject.partition(":")
    return Fact.from_dict({"fact": "freeze", "subject": {"kind": kind, "id": ident},
                           "value": {"start": start, "end": end, "reason": reason,
                                     "mode": mode},
                           "source": "human", "status": status, **kw})


def _approval(subject="team:payments", classes=("rightsizing",), approvers=("github:alice",),
              status="confirmed", **value):
    kind, _, ident = subject.partition(":")
    return Fact.from_dict({"fact": "approval", "subject": {"kind": kind, "id": ident},
                           "value": {"action_classes": list(classes),
                                     "approvers": list(approvers), **value},
                           "source": "human", "status": status})


def _utc(*a):
    return datetime(*a, tzinfo=UTC)


# ── validation ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,needle", [
    ({"start": "2026-11-27T00:00:00", "end": DEC01}, "no UTC offset"),
    ({"start": "2026-11-27", "end": DEC01}, "no UTC offset"),
    ({"start": "Black Friday", "end": DEC01}, "not an ISO 8601"),
    ({"start": NOV27}, "value.end"),
    ({"start": DEC01, "end": NOV27}, "after value.start"),
    ({"start": NOV27, "end": NOV27}, "after value.start"),
    ({"start": NOV27, "end": DEC01, "mode": "block"}, "value.mode"),
    ({"start": NOV27, "end": DEC01, "reason": ""}, "value.reason"),
])
def test_a_freeze_that_does_not_say_when_or_why_is_refused(value, needle):
    value = {"reason": "Black Friday", **value}
    with pytest.raises(FactError, match=needle):
        Fact.from_dict({"fact": "freeze", "subject": {"kind": "org", "id": "org"},
                        "value": value})


def test_a_freeze_keeps_the_offset_it_was_written_in():
    f = _freeze(start="2026-11-27T05:00:00Z", end=DEC01, mode=None)
    assert f.value["start"] == "2026-11-27T05:00:00+00:00"
    assert f.value["end"] == DEC01
    assert f.value["mode"] == "ask"          # the default
    # The same instant spelled in another zone says the same thing.
    other = _freeze(start=NOV27, end=DEC01, mode="ask")
    assert f.said == other.said


def test_yaml_timestamps_are_read_with_their_offset():
    raw = safe_yaml("- fact: freeze\n  subject: {kind: org, id: org}\n"
                    "  value: {start: 2026-11-27T00:00:00-05:00, end: 2026-12-01T00:00:00Z,"
                    " reason: x}\n")
    f = Fact.from_dict(raw[0])
    assert parse_when(f.value["start"]) == _utc(2026, 11, 27, 5)
    # Unquoted and without an offset, YAML reads a naive time: refused.
    raw = safe_yaml("- fact: freeze\n  subject: {kind: org, id: org}\n"
                    "  value: {start: 2026-11-27 00:00:00, end: 2026-12-01T00:00:00Z,"
                    " reason: x}\n")
    with pytest.raises(FactError, match="no UTC offset"):
        Fact.from_dict(raw[0])


@pytest.mark.parametrize("subject", ["repo_path:infra", "service:api", "tag_value:x"])
def test_a_freeze_covers_the_org_a_team_an_environment_or_an_account(subject):
    with pytest.raises(FactError, match="freeze fact's subject.kind"):
        _freeze(subject)
    for ok in ("org:org", "team:payments", "environment:prod", "aws_account:123456789012",
               "gcp_project:p", "azure_subscription:s"):
        assert _freeze(ok).subject.kind == ok.split(":")[0]


@pytest.mark.parametrize("value,needle", [
    ({"action_classes": [], "approvers": ["github:a"]}, "action_classes is empty"),
    ({"action_classes": ["Rightsizing!"], "approvers": ["github:a"]}, "not an action class"),
    ({"action_classes": ["rightsizing"], "approvers": []}, "approvers is empty"),
    ({"action_classes": ["rightsizing"], "approvers": ["alice"]}, "kind:id"),
    ({"action_classes": ["rightsizing"], "approvers": ["slack:#x"]}, "kind:id"),
    ({"action_classes": ["rightsizing"], "approvers": ["github:a b"]}, "kind:id"),
    ({"action_classes": ["rightsizing"], "approvers": ["github:a"], "min": 2}, "more than"),
    ({"action_classes": ["rightsizing"], "approvers": ["github:a"], "min": 0}, "value.min"),
    ({"action_classes": ["rightsizing"], "approvers": ["github:a"], "change_ticket": "yes"},
     "change_ticket"),
])
def test_an_approval_chain_is_checked(value, needle):
    with pytest.raises(FactError, match=needle):
        Fact.from_dict({"fact": "approval", "subject": {"kind": "team", "id": "payments"},
                        "value": value})


def test_an_approval_chain_is_a_teams_or_an_environments():
    with pytest.raises(FactError, match="approval fact's subject.kind"):
        _approval("org:org")
    f = _approval(classes=["Delete_Resource", "*"], approvers=["GitHub:alice", "team:platform"])
    assert f.value["action_classes"] == ["delete_resource", "*"]
    assert f.value["approvers"] == ["github:alice", "team:platform"]
    assert f.value["min"] == 1


# ── precedence and queries ────────────────────────────────────────────────────

def test_several_freezes_on_one_subject_each_hold():
    a = _freeze(start=NOV27, end=DEC01, reason="Black Friday")
    b = _freeze(start="2026-12-20T00:00:00Z", end="2027-01-02T00:00:00Z", reason="Holidays")
    assert a.slot != b.slot
    m = OrgModel([a, b])
    assert [f.value["reason"] for f, _ in m.freezes_at(_utc(2026, 11, 28), envs=["prod"])] \
        == ["Black Friday"]
    assert [f.value["reason"] for f, _ in m.freezes_at(_utc(2026, 12, 25), envs=["prod"])] \
        == ["Holidays"]
    assert m.freezes_at(_utc(2026, 12, 10), envs=["prod"]) == []


@pytest.mark.parametrize("at,inside", [
    (_utc(2026, 11, 27, 4, 59, 59), False),     # 23:59:59 in New York on the 26th
    (_utc(2026, 11, 27, 5, 0, 0), True),        # the start is inside
    (_utc(2026, 12, 1, 4, 59, 59), True),
    (_utc(2026, 12, 1, 5, 0, 0), False),        # the end is not
])
def test_the_window_is_compared_in_utc(at, inside):
    m = OrgModel([_freeze()])
    assert bool(m.freezes_at(at, envs=["prod"])) is inside
    # The same instants, asked in another zone, answer the same.
    tokyo = at.astimezone(datetime.fromisoformat("2026-01-01T00:00:00+09:00").tzinfo)
    assert bool(m.freezes_at(tokyo, envs=["prod"])) is inside


def test_a_window_across_a_dst_change_is_read_in_each_ends_offset():
    # 2026-11-01 is when New York leaves daylight time: 01:30 happens twice.
    f = _freeze(start="2026-10-31T12:00:00-04:00", end="2026-11-01T01:30:00-05:00")
    m = OrgModel([f])
    assert m.freezes_at(_utc(2026, 11, 1, 6, 29), envs=["prod"])       # 01:29 EST
    assert not m.freezes_at(_utc(2026, 11, 1, 6, 30), envs=["prod"])   # 01:30 EST


def test_which_scopes_a_freeze_covers():
    at = _utc(2026, 11, 28)
    m = OrgModel([_freeze("org:org", reason="everything"),
                  _freeze("team:payments", reason="team"),
                  _freeze("environment:prod", reason="env"),
                  _freeze("aws_account:123456789012", reason="acct")])

    def reasons(**kw):
        return sorted(f.value["reason"] for f, _ in m.freezes_at(at, **kw))
    assert reasons() == ["everything"]
    assert reasons(team="payments") == ["everything", "team"]
    assert reasons(team="search") == ["everything"]
    assert reasons(envs=["nonprod"]) == ["everything"]
    assert reasons(envs=["prod"]) == ["env", "everything"]
    assert reasons(accounts=["aws_account:123456789012"]) == ["acct", "everything"]
    assert reasons(accounts=["aws_account:999999999999"]) == ["everything"]


def test_a_team_freeze_is_read_through_confirmed_team_names():
    team = Fact.from_dict({"fact": "team", "subject": {"kind": "team", "id": "payments"},
                           "value": {"aliases": ["pay"]}, "source": "human",
                           "status": "confirmed"})
    guess = Fact.from_dict({"fact": "team", "subject": {"kind": "team", "id": "search"},
                            "value": {"aliases": ["srch"]}, "source": "codeowners:x"})
    m = OrgModel([team, guess, _freeze("team:pay", reason="t"), _freeze("team:srch")])
    assert [f.value["reason"] for f, _ in m.freezes_at(_utc(2026, 11, 28), team="payments")] \
        == ["t"]
    # A proposed alias does not route a freeze to a team.
    assert m.freezes_at(_utc(2026, 11, 28), team="search") == []


def test_proposed_and_rejected_freezes():
    at = _utc(2026, 11, 28)
    proposed = _freeze(status="proposed", reason="guess")
    rejected = _freeze(status="rejected", reason="no", start="2026-11-26T00:00:00Z")
    m = OrgModel([proposed, rejected])
    hits = m.freezes_at(at, envs=["prod"])
    assert [(f.value["reason"], sure) for f, sure in hits] == [("guess", False)]


def test_an_untrusted_confirmed_freeze_is_not_sure_and_hides_nothing():
    at = _utc(2026, 11, 28)
    mine = _freeze(mode="deny", reason="mine")
    mine.layer = 1
    theirs = _freeze(mode="ask", reason="theirs")       # same slot, repo layer, untrusted
    theirs.trusted, theirs.layer = False, 2
    m = OrgModel([mine, theirs])
    hits = m.freezes_at(at, envs=["prod"], strict=True)
    # The confirmed deny still answers first, sure; the repo's is a proposal.
    assert [(f.value["reason"], sure) for f, sure in hits] == [("mine", True), ("theirs", False)]
    assert m.freezes_at(at, envs=["prod"], strict=False)[0][1] is True


def test_only_confirmed_approval_chains_name_anybody():
    a = _approval(approvers=["github:alice"])
    b = _approval("environment:prod", classes=["*"], approvers=["team:platform"])
    c = _approval(classes=["delete_resource"], approvers=["github:mallory"], status="proposed")
    untrusted = _approval(classes=["rightsizing", "ticket"], approvers=["github:eve"])
    untrusted.trusted = False
    m = OrgModel([a, b, c, untrusted])
    assert [f.key for f in m.approvals_for("rightsizing", team="payments")] == [a.key]
    assert {f.key for f in m.approvals_for("rightsizing", team="payments", envs=["prod"])} \
        == {a.key, b.key}
    assert m.approvals_for("delete_resource", team="payments") == []
    assert [f.key for f in m.approvals_for("delete_resource", envs=["prod"])] == [b.key]
    assert {f.key for f in m.approvals_for("rightsizing", team="payments", strict=False)} \
        == {a.key, untrusted.key}


# ── the CLI ───────────────────────────────────────────────────────────────────

@pytest.fixture
def org_dir(tmp_path, monkeypatch):
    d = tmp_path / "org"
    monkeypatch.setenv("FINOPS_ORG_DIR", str(d))
    return d


def test_set_freeze_writes_a_confirmed_fact(org_dir, capsys):
    assert cli.main(["set", "freeze", "--scope", "environment:prod", "--start", NOV27,
                     "--end", DEC01, "--reason", "Black Friday", "--mode", "deny",
                     "--as", "maria"]) == 0
    f = org.load().by_kind("freeze")[0]
    assert f.confirmed and f.confirmed_by == "maria"
    assert f.value == {"start": NOV27, "end": DEC01, "reason": "Black Friday", "mode": "deny"}
    assert (org_dir / "freezes.yaml").is_file()


def test_set_freeze_reads_a_time_without_an_offset_in_tz(org_dir):
    assert cli.main(["set", "freeze", "--scope", "org:org", "--start", "2026-09-01T09:00",
                     "--end", "2027-01-15T09:00", "--tz", "America/New_York",
                     "--reason", "migration", "--as", "maria"]) == 0
    v = org.load().by_kind("freeze")[0].value
    # Daylight time at the start, standard time at the end.
    assert v["start"] == "2026-09-01T09:00:00-04:00"
    assert v["end"] == "2027-01-15T09:00:00-05:00"


def test_set_freeze_without_tz_uses_this_machines_zone(org_dir, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    import time
    time.tzset()
    try:
        assert cli.main(["set", "freeze", "--scope", "org:org", "--start", "2026-11-27T00:00",
                         "--end", "2026-11-28T00:00", "--reason", "r", "--as", "maria"]) == 0
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert org.load().by_kind("freeze")[0].value["start"] == "2026-11-27T00:00:00+09:00"


@pytest.mark.parametrize("argv,needle", [
    (["--start", NOV27, "--end", DEC01], "--reason"),
    (["--end", DEC01, "--reason", "r"], "--start"),
    (["--start", NOV27, "--end", DEC01, "--reason", "r", "--tz", "Mars/Olympus"], "Not set"),
    (["--start", DEC01, "--end", NOV27, "--reason", "r"], "after value.start"),
])
def test_set_freeze_refuses_what_it_cannot_store(org_dir, capsys, argv, needle):
    assert cli.main(["set", "freeze", "--scope", "org:org", *argv, "--as", "maria"]) != 0
    assert needle in capsys.readouterr().err
    assert not org.load().by_kind("freeze")


def test_set_is_a_human_decision(org_dir, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_is_tty", lambda: False)
    assert cli.main(["set", "freeze", "--scope", "org:org", "--start", NOV27, "--end", DEC01,
                     "--reason", "r"]) == 2
    assert cli.main(["set", "approval", "--scope", "team:payments", "--action-class",
                     "rightsizing", "--approver", "github:alice"]) == 2
    assert "human decision" in capsys.readouterr().err
    assert not org.load().facts


def test_set_approval(org_dir, capsys):
    assert cli.main(["set", "approval", "--scope", "team:payments", "--action-class",
                     "rightsizing", "--action-class", "delete_resource", "--approver",
                     "github:alice", "--approver", "team:platform", "--min", "2",
                     "--change-ticket", "--as", "maria"]) == 0
    f = org.load().by_kind("approval")[0]
    assert f.confirmed and f.value == {
        "action_classes": ["rightsizing", "delete_resource"],
        "approvers": ["github:alice", "team:platform"], "min": 2, "change_ticket": True}
    assert cli.main(["set", "approval", "--scope", "team:payments", "--action-class",
                     "rightsize", "--approver", "github:alice", "--as", "maria"]) == 1
    assert "not an action class nable knows" in capsys.readouterr().err


def test_status_lists_freezes_and_approvals(org_dir, capsys):
    now = datetime.now(UTC)
    current = (now - timedelta(days=1)).isoformat(), (now + timedelta(days=1)).isoformat()
    later = (now + timedelta(days=30)).isoformat(), (now + timedelta(days=31)).isoformat()
    over = (now - timedelta(days=9)).isoformat(), (now - timedelta(days=8)).isoformat()
    for (start, end), reason, mode in ((current, "now", "deny"), (later, "later", "ask"),
                                       (over, "over", "ask")):
        org.set_fact(org.make_fact("freeze", "environment:prod",
                                   {"start": start, "end": end, "reason": reason,
                                    "mode": mode}, source="human"), human("maria"))
    org.propose(org.make_fact("freeze", "org:org", {"start": current[0], "end": current[1],
                                                    "reason": "guess", "mode": "deny"},
                              source="agent:x"))
    org.set_fact(org.make_fact("approval", "team:payments",
                               {"action_classes": ["rightsizing"],
                                "approvers": ["github:alice"]}, source="human"), human("maria"))
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "freezes: 3" in out
    assert "in force, denies: now" in out
    assert "upcoming, asks: later" in out
    assert "over" not in out.split("freezes:")[1].split("approvals:")[0]
    assert "asks (proposed: a guess may only ask): guess" in out
    assert "approvals: 1" in out and "rightsizing: 1 of github:alice  (confirmed)" in out
    assert cli.main(["status", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert {r["value"]["reason"]: (r["state"], r["applies_as"]) for r in doc["freezes"]} == {
        "now": ("in force", "denies"), "later": ("upcoming", "asks"),
        "guess": ("in force", "asks (proposed: a guess may only ask)")}
    assert [r["value"]["approvers"] for r in doc["approvals"]] == [["github:alice"]]


def test_a_proposed_freeze_is_asked_about_on_its_own_and_defaults_yes(org_dir):
    now = datetime.now(UTC)
    org.propose(org.make_fact("freeze", "environment:prod",
                              {"start": now.isoformat(),
                               "end": (now + timedelta(days=1)).isoformat(),
                               "reason": "release week"}, source="agent:x"))
    org.propose(org.make_fact("approval", "team:payments",
                              {"action_classes": ["rightsizing"], "approvers": ["github:eve"]},
                              source="agent:x"))
    qs = {q.fact["fact"]: q for q in org.questions(10)}
    assert qs["freeze"].kind == "confirm" and qs["freeze"].default == "y"
    assert "is frozen from" in qs["freeze"].text
    # Naming who reviews is a person's call: never waved through with enter.
    assert qs["approval"].kind == "confirm" and qs["approval"].default == "n"


def test_the_org_files_are_created_for_the_new_kinds(tmp_path):
    from finops.org.store import ensure_dir
    made = {p.name for p in ensure_dir(tmp_path / "o")}
    assert {"freezes.yaml", "approvals.yaml"} <= made


def test_a_freeze_survives_the_guards_model_cache(tmp_path, monkeypatch):
    from finops import guard_org
    monkeypatch.setenv("FINOPS_ORG_DIR", str(tmp_path / "o"))
    org.set_fact(_freeze(), human("maria"))
    first = guard_org.load_model(str(tmp_path))
    again = guard_org.load_model(str(tmp_path))       # from the cache
    assert [f.value for f in again.by_kind("freeze")] == [f.value for f in first.by_kind("freeze")]
    assert again.freezes_at(_utc(2026, 11, 28), envs=["prod"])


def test_subject_kinds_stay_as_they_were():
    # The new kinds add no subject kinds: a freeze on an account uses the
    # account kinds the model already has.
    assert Subject("org", "org").kind in org.SUBJECT_KINDS
    assert "freeze" in org.FACT_KINDS and "approval" in org.FACT_KINDS
    assert os.path.basename(org.FILE_FOR_KIND["freeze"]) == "freezes.yaml"
