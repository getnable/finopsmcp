# SPDX-License-Identifier: Apache-2.0
"""A team budget that holds across machines (D107, D108).

`nable cloud status` reads the box's month to date for one person (POST
/api/free-tool/standing with exactly {"person": P}), when the box lists
"standing" among what it reads, and keeps it as cloud-standing.json beside the
ledger, 0600, without the key. P is --as, else the guard's own person
(FINOPS_GUARD_PERSON, else git's user.email).

The guard reads that file, and never the network: for each budget that
applies here, the figure compared is the larger of this machine's month to
date and the box's, never their sum. The box's figure counts only for this
UTC month, this person and the box this machine sends to, at any age, which
is said. Only the local budgets.yaml sets a threshold. Anything else (no file,
last month's, another person's or box's, a malformed one, one over 64 KiB) is
this machine's figure alone, and `nable guard doctor` says why.

No listener and no network: the box is a fake opener, and the guard's paths
run with socket connects and urllib's opener made to fail.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import socket
import stat
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from finops import ai_budget as ab
from finops import cli_cloud, cloud_standing, guard
from finops import cloud_connect as cc
from tests.test_cloud_connect import BOX, KEY, Box
from tests.test_guard_org_budget import AT, BUDGET, MONTH_START, TEAMS, _transcript
from tests.test_guard_org_budget import box as _box  # noqa: F401 - a fixture

OTHER_BOX = "https://other-box.example.com"
YESTERDAY = (AT - timedelta(days=1)).isoformat()
# The box's references to two workspaces on one pooled box, and B's key.
WS_A = "ws_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
WS_B = "ws_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
KEY_B = "nbl_other_workspace_key_B_0123456789ab"  # pragma: allowlist secret
# The box's host named as a whole word in a sentence (not as part of another
# name such as other-box.example.com).
_BOX_HOST_NAMED = re.compile(r"(?<![\w.-])box\.example\.com(?![\w-])")


def _names_box_host(text: str) -> bool:
    return _BOX_HOST_NAMED.search(text) is not None


def answer(*, person="alice@acme.example", team_usd="5420", person_usd="120.5",
           org_usd="9000", month="2026-10", as_of=YESTERDAY, workspace_ref=WS_A, **over):
    """The box's standing answer, in the shape the enterprise route gives."""
    doc = {"month": month, "as_of": as_of, "basis": "list-price equivalent",
           "workspace_ref": workspace_ref,
           "budgets_from": {"version": 3, "uploaded_at": "2026-10-02T09:00:00+00:00"},
           "person": {"id": person, "usd": person_usd, "days": 4, "last_day": "2026-10-04",
                      "budget": {"monthly_usd": None, "per_person_usd": "400",
                                 "state": "under"}},
           "teams": [{"team": "payments", "usd": team_usd, "people": 4, "people_sent": 3,
                      "latest_day": "2026-10-04", "oldest_last_day": "2026-10-01",
                      "budget": {"monthly_usd": "5000", "per_person_usd": None,
                                 "state": "over"}}],
           "org": {"usd": org_usd, "people_sent": 12, "latest_day": "2026-10-04",
                   "budget": None}}
    doc.update(over)
    return doc


class StandingBox(Box):
    """The fake box, with its status listing what it reads and a standing
    route that records exactly what it was sent."""

    def __init__(self, *, reads=("standing",), standing=None, refuse=None,
                 refuse_error="forbidden", workspace_ref=WS_A, **kw):
        super().__init__(**kw)
        self.reads = list(reads)
        self.answer = standing if standing is not None else answer()
        self.refuse = refuse
        self.refuse_error = refuse_error
        self.workspace_ref = workspace_ref

    def status(self) -> dict:
        return {**super().status(), "reads": self.reads,
                "accepts": ["org_model", "ledger_line", "agent_usage", "org_file_delete"],
                **({"workspace_ref": self.workspace_ref} if self.workspace_ref else {})}

    def __call__(self, req: urllib.request.Request, timeout: float):
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        if req.get_method() == "POST" and path == "api/free-tool/standing":
            self.requests.append({"method": "POST", "url": req.full_url,
                                  "headers": dict(req.header_items()), "body": req.data})
            if self.refuse:
                return self.refuse, json.dumps({"error": self.refuse_error,
                                                "message": "not this one"}).encode()
            return 200, json.dumps(self.answer).encode()
        return super().__call__(req, timeout)

    @property
    def standing_posts(self) -> list[bytes]:
        return [r["body"] for r in self.requests if r["url"].endswith("/standing")]


@pytest.fixture
def laptop(tmp_path, monkeypatch):
    """A sandboxed HOME with git's config location variables cleared, the
    clock at the fixture's month, and git's user.email stubbed (nothing here
    depends on what is on PATH)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                "GIT_CONFIG_NOSYSTEM", "FINOPS_GUARD_PERSON", "FINOPS_AIRGAP", cc.KEY_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(ab, "_month_start_epoch", lambda: MONTH_START)
    monkeypatch.setattr(cloud_standing, "now_epoch", lambda: AT.timestamp())
    monkeypatch.setattr(ab, "_read_git_email", lambda cwd: "Alice@Acme.example")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _parse(*argv):
    p = argparse.ArgumentParser(prog="nable")
    sub = p.add_subparsers(dest="cmd")
    cli_cloud.add_parser(sub)
    return p.parse_args(["cloud", *argv])


def _status(box, *argv, environ=None):
    out, err = io.StringIO(), io.StringIO()
    code = cli_cloud.run(_parse("status", "--box", BOX, *argv), opener=box,
                         environ=environ if environ is not None else {cc.KEY_ENV: KEY},
                         prompt=None, stdout=out, stderr=err, terminal=True, now=AT)
    return code, out.getvalue(), err.getvalue()


# ── nable cloud status reads the standing ────────────────────────────────────

def test_status_posts_exactly_the_person_the_guard_holds_and_keeps_the_answer(laptop):
    box = StandingBox()
    code, out, err = _status(box)
    assert code == 0, err
    person, source = ab.developer(str(laptop))
    assert (person, source) == ("alice@acme.example", "git user.email")
    assert box.standing_posts == [json.dumps({"person": person},
                                             separators=(",", ":")).encode()]
    post = next(r for r in box.requests if r["url"].endswith("/standing"))
    assert post["headers"]["Authorization"] == f"Bearer {KEY}"
    assert post["headers"]["Content-type"] == "application/json"
    path = cloud_standing.path()
    assert path.name == "cloud-standing.json" and path.parent == ab_ledger_dir()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_text()
    assert KEY not in raw and KEY not in out + err
    doc = json.loads(raw)
    assert (doc["box"], doc["host"], doc["month"], doc["person"]) == (
        BOX, "box.example.com", "2026-10", person)
    assert doc["fetched_at"] == AT.isoformat(timespec="seconds")
    assert doc["figures"]["teams"][0]["usd"] == "5420"
    for shown in ("team payments", "$5,420.00", "3 of 4 people sent", "latest day 2026-10-04",
                  "oldest last day 2026-10-01", "$120.50", "budget $5,000.00 a month: over",
                  "$120.50 across 4 day(s), last day 2026-10-04", "never their sum", str(path)):
        assert shown in out, shown


def ab_ledger_dir():
    from finops import guard_ledger
    return guard_ledger.ledger_path().parent


def test_as_and_finops_guard_person_name_the_person_in_that_order(laptop, monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD_PERSON", "Bob@Acme.example")
    box = StandingBox(standing=answer(person="bob@acme.example"))
    assert _status(box)[0] == 0
    assert json.loads(box.standing_posts[-1]) == {"person": "bob@acme.example"}
    box = StandingBox(standing=answer(person="carol@acme.example"))
    assert _status(box, "--as", "Carol@Acme.example")[0] == 0
    assert json.loads(box.standing_posts[-1]) == {"person": "carol@acme.example"}


def test_a_box_that_does_not_read_standing_is_asked_nothing_and_says_so(laptop):
    box = StandingBox(reads=())
    code, out, _err = _status(box)
    assert code == 0 and box.standing_posts == []
    assert [r["method"] for r in box.requests] == ["GET"]
    assert "does not offer a person's standing to this workspace" in out
    assert not cloud_standing.path().exists()


def test_the_json_status_carries_the_standing(laptop):
    code, out, _err = _status(StandingBox(), "--json")
    doc = json.loads(out)
    assert code == 0 and doc["standing"]["read"] is True
    assert doc["standing"]["standing"]["figures"]["org"]["usd"] == "9000"
    code, out, _err = _status(StandingBox(reads=()), "--json")
    assert json.loads(out)["standing"]["read"] is False


def test_nobody_named_asks_nothing(laptop, monkeypatch):
    monkeypatch.setattr(ab, "_read_git_email", lambda cwd: None)
    box = StandingBox()
    code, out, _err = _status(box)
    assert code == 0 and box.standing_posts == [] and "nobody is named" in out


@pytest.mark.parametrize("bad, why", [
    (answer(person="mallory@acme.example"), "another person"),
    (answer(team_usd=5420), "decimal string"),
    (answer(team_usd="-1"), "decimal string"),
    (answer(month="2026-13"), "YYYY-MM"),
    ({"error": "nope"}, "month"),
])
def test_an_answer_this_version_does_not_read_is_not_kept(laptop, bad, why):
    code, out, _err = _status(StandingBox(standing=bad))
    assert code == cli_cloud.EXIT_REFUSED and why in out
    assert not cloud_standing.path().exists()


def test_a_refused_standing_exits_1_says_why_and_keeps_the_last_one(laptop):
    assert _status(StandingBox())[0] == 0
    before = cloud_standing.path().read_bytes()
    code, out, err = _status(StandingBox(refuse=403))
    assert code == cli_cloud.EXIT_REFUSED and "403" in out and "forbidden" in out
    assert "not read for alice@acme.example (git user.email)" in out
    assert KEY not in out + err
    assert cloud_standing.path().read_bytes() == before


def test_as_takes_one_line(laptop):
    code, _out, err = _status(StandingBox(), "--as", "x" * 400)
    assert code == cli_cloud.EXIT_USAGE and "x" * 400 not in err


# ── the guard: the larger figure, never the sum ──────────────────────────────

def _team_budget(box_fixture, monthly="5000"):
    (box_fixture["org"] / "budgets.yaml").write_text(BUDGET.format(
        kind="team", ident="payments", value=f"{{monthly_usd: {monthly}}}", status="confirmed"))
    (box_fixture["org"] / "teams.yaml").write_text(TEAMS)


def _machine(box_fixture, usd: int):
    """This machine's month: one response at $5 a million input tokens."""
    for f in (box_fixture["claude"] / "projects").rglob("*.jsonl"):
        f.unlink()
    _transcript(box_fixture["claude"], "s-opus", model="claude-opus-4-6",
                inp=usd * 200_000, out=0)


def _keep(doc, *, box=BOX):
    cloud_standing.write(cloud_standing.normalize(doc, box=box, person=doc["person"]["id"],
                                                  fetched_at=AT - timedelta(hours=2)))


@pytest.fixture
def team(_box, monkeypatch):  # noqa: F811
    _team_budget(_box)
    monkeypatch.setenv("FINOPS_GUARD_PERSON", "alice@acme.example")
    monkeypatch.setattr(cloud_standing, "now_epoch", lambda: AT.timestamp())
    return _box


def test_the_boxs_larger_team_figure_asks_and_names_both(team, monkeypatch):
    _machine(team, 300)
    _keep(answer(team_usd="5420"))
    v = guard.gate_command("ls -la")
    assert v["decision"] == "ask" and v["action_type"] == "ai_budget"
    r = v["reason"]
    assert "$5,420.00" in r and _names_box_host(r) and YESTERDAY in r
    assert "24 hours old" in r and "$300.00" in r
    assert "The larger, the box's $5,420.00, is the one compared" in r
    assert "$5,720" not in r and "5720" not in r
    assert v["budget_figure"] == {"compared": "box", "machine_usd": "300.00",
                                  "box_figure_read": True}
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    v = guard.gate_command("ls -la")
    assert v["decision"] == "deny" and "$5,420.00" in v["reason"]


def test_the_policys_on_budget_breach_deny_holds_on_the_boxs_figure(team, monkeypatch, tmp_path):
    pol = tmp_path / "policy.yaml"
    pol.write_text("on_budget_breach: deny\n")
    monkeypatch.setenv("FINOPS_POLICY_FILE", str(pol))
    _machine(team, 300)
    _keep(answer(team_usd="5420"))
    assert guard.gate_command("ls -la")["decision"] == "deny"


def test_this_machines_larger_figure_answers_the_same(team, monkeypatch):
    _machine(team, 5420)
    _keep(answer(team_usd="200"))
    v = guard.gate_command("ls -la")
    assert v["decision"] == "ask"
    r = v["reason"]
    assert "$5,420.00" in r and "$200.00" in r
    assert "The larger, this machine's $5,420.00, is the one compared" in r
    assert "5,620" not in r and v["budget_figure"]["compared"] == "machine"
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    assert guard.gate_command("ls -la")["decision"] == "deny"


def test_the_box_never_adds_to_this_machine(team):
    """$300 here and $4,800 on the box: the sum ($5,100) would be over a
    $5,000 budget, the larger ($4,800) is not."""
    _machine(team, 300)
    _keep(answer(team_usd="4800"))
    v = guard.gate_command("ls -la")
    assert v is not None and v["decision"] == "warn"      # 96% of $5,000: close, not over
    assert "$4,800.00" in v["reason"] and "$5,100" not in v["reason"]


def test_only_the_local_budget_sets_the_threshold(team):
    """The box says the team's budget is over; locally it is $10,000."""
    _team_budget(team, monthly="10000")
    _machine(team, 300)
    _keep(answer(team_usd="5420"))
    assert guard.gate_command("ls -la") is None


def test_the_boxs_person_and_org_figures_count_for_their_budgets(team):
    (team["org"] / "budgets.yaml").write_text(
        BUDGET.format(kind="person", ident="alice@acme.example", value="{monthly_usd: 100}",
                      status="confirmed")
        + BUDGET.format(kind="org", ident="org", value="{monthly_usd: 10000}",
                        status="confirmed").split("\n", 1)[1])
    _machine(team, 10)
    _keep(answer(person_usd="150", org_usd="9000", team_usd=None))
    v = guard.gate_command("ls -la")
    assert v["decision"] == "ask" and "$150.00" in v["reason"]
    assert "alice@acme.example" in v["reason"]


def test_the_boxs_figure_never_meets_an_earlier_identitys_budget(_box, monkeypatch):  # noqa: F811
    """git's user.email changed this month. The earlier identity's budgets
    still apply to this machine's month, but the box's standing is about the
    person named now, so it is compared with that person's budgets alone."""
    from decimal import Decimal

    from tests.test_guard_org_budget import _repo
    repo = _repo(_box)
    who = ["alice@acme.example"]
    monkeypatch.setattr(ab, "_read_git_email", lambda cwd: who[0])
    monkeypatch.setattr(cloud_standing, "now_epoch", lambda: AT.timestamp())
    (_box["org"] / "budgets.yaml").write_text(
        BUDGET.format(kind="person", ident="alice@acme.example", value="{monthly_usd: 100}",
                      status="confirmed")
        + BUDGET.format(kind="person", ident="carol@acme.example",
                        value="{monthly_usd: 10000}", status="confirmed").split("\n", 1)[1])
    assert guard.gate_command("ls -la", cwd=str(repo)) is None     # $4.50 of alice's $100
    who[0] = "carol@acme.example"
    (repo / ".git" / "config").write_text("[user]\n\temail = carol@acme.example\n")
    guard.gate_command("ls -la", cwd=str(repo))                    # the identity ask, once
    _keep(answer(person="carol@acme.example", person_usd="5000", teams=[]))
    st = ab.org_budget_status(str(repo))
    hits = {h["person"]: h for h in st["hits"]}
    assert set(hits) == {"alice@acme.example", "carol@acme.example"}
    assert hits["carol@acme.example"]["box_usd"] == Decimal(5000)
    assert hits["alice@acme.example"]["box_usd"] is None
    assert hits["alice@acme.example"]["compared"] == "machine"
    assert guard.gate_command("ls -la", cwd=str(repo)) is None     # 50% of carol's budget


def test_a_team_the_box_does_not_list_is_this_machines_figure(team):
    _machine(team, 5420)
    _keep(answer(teams=[]))
    v = guard.gate_command("ls -la")
    assert v["decision"] == "ask" and "this machine only" in v["reason"]


def test_no_records_received_on_the_box_is_this_machines_figure(team):
    _machine(team, 300)
    _keep(answer(team_usd=None))
    assert guard.gate_command("ls -la") is None


# ── a standing that does not count ───────────────────────────────────────────

def _too_big():
    doc = answer(team_usd="5420")
    pad = {"team": "padding", "usd": None, "people": 0, "people_sent": 0, "latest_day": None,
           "oldest_last_day": None, "budget": None}
    _keep(doc)
    path = cloud_standing.path()
    raw = json.loads(path.read_text())
    raw["figures"]["teams"] += [pad] * 2_000
    path.write_text(json.dumps(raw))
    assert path.stat().st_size > cloud_standing.MAX_BYTES


IGNORED = {
    "no file": (lambda: None, "there is no cloud-standing.json"),
    "last month": (lambda: _keep(answer(month="2026-09")), "is for 2026-09"),
    "another person": (lambda: _keep(answer(person="bob@acme.example")),
                       "is for bob@acme.example"),
    "another box": (lambda: (_keep(answer(), box=OTHER_BOX), cloud_standing.bind(BOX)),
                    "last sent its usage to box.example.com"),
    "malformed": (lambda: cloud_standing.path().write_text("{not json"), "not JSON"),
    "not this format": (lambda: cloud_standing.path().write_text('{"v": 2}'),
                        "not a standing this version reads"),
    "over 64 KiB": (_too_big, "over the 64 KiB"),
}


@pytest.mark.parametrize("case", list(IGNORED))
def test_a_standing_that_does_not_count_is_this_machines_figure_and_doctor_says_why(
        team, case):
    _machine(team, 300)
    local_only = guard.gate_command("ls -la")
    assert local_only is None
    _machine(team, 4900)                       # close on this machine alone: a note
    for f in (cloud_standing.path(), cloud_standing.box_path()):
        f.unlink(missing_ok=True)
    from finops import guard_ledger
    guard_ledger.ledger_path().with_name("guard-budget-note.json").unlink(missing_ok=True)
    expect = guard.gate_command("ls -la")
    make, why = IGNORED[case]
    cloud_standing.path().parent.mkdir(parents=True, exist_ok=True)
    make()
    guard_ledger.ledger_path().with_name("guard-budget-note.json").unlink(missing_ok=True)
    got = guard.gate_command("ls -la")
    assert got == expect and got["decision"] == "warn" and "$5,420" not in got["reason"]
    d = guard.doctor()["ai_budgets"]
    assert d["box_standing"]["used"] is False and why in d["box_standing"]["why"]
    assert any(why in gap for gap in guard.doctor()["not_covered"]), why


def test_a_standing_from_the_box_this_machine_sends_to_counts_at_any_age(team, monkeypatch):
    _machine(team, 300)
    _keep(answer(as_of=(AT - timedelta(days=20)).isoformat()))
    cloud_standing.bind(BOX, workspace=WS_A)
    v = guard.gate_command("ls -la")
    assert v["decision"] == "ask" and "20 days old" in v["reason"]
    d = guard.doctor()["ai_budgets"]
    assert d["box_standing"]["used"] is True and d["box_standing"]["host"] == "box.example.com"
    assert any("the larger is compared, never their sum" in c
               for c in guard.doctor()["covered"])


def test_a_send_records_the_box_this_machine_sends_to(team, monkeypatch, tmp_path):
    from tests.test_cloud_connect import records, write_ledger
    write_ledger(records(2))
    monkeypatch.setenv("FINOPS_ORG_DIR", str(team["org"]))
    cc.send(cc.build(agent_usage=False), OTHER_BOX, KEY, opener=StandingBox())
    assert json.loads(cloud_standing.box_path().read_text())["box"] == OTHER_BOX
    assert stat.S_IMODE(cloud_standing.box_path().stat().st_mode) == 0o600
    _machine(team, 300)
    _keep(answer())                            # from box.example.com
    v = guard.gate_command("ls -la")
    assert v is None, "a standing from another box than the one sent to was used"


# ── the workspace a standing is from ─────────────────────────────────────────

class PooledBox:
    """One pooled box: every workspace behind one URL, and the key alone
    decides which answers. Each workspace is a StandingBox with its own
    reference and answer."""

    def __init__(self, workspaces: dict):
        self.ws = {key: StandingBox(standing=doc, workspace_ref=ref)
                   for key, (ref, doc) in workspaces.items()}

    def __call__(self, req: urllib.request.Request, timeout: float):
        key = dict(req.header_items())["Authorization"].split(" ", 1)[1]
        return self.ws[key](req, timeout)


def _send_to(box_opener, key, team, monkeypatch):
    from tests.test_cloud_connect import records, write_ledger
    write_ledger(records(2))
    monkeypatch.setenv("FINOPS_ORG_DIR", str(team["org"]))
    cc.send(cc.build(agent_usage=False), BOX, key, opener=box_opener)


def _pooled():
    return PooledBox({KEY: (WS_A, answer(team_usd="200")),
                      KEY_B: (WS_B, answer(team_usd="9000", workspace_ref=WS_B))})


def test_another_workspaces_standing_on_the_same_pooled_box_never_reaches_the_guard(
        team, monkeypatch):
    """This machine sends to workspace A. `nable cloud status` with
    workspace B's key on the same box URL reads B's $9,000, which neither
    replaces the guard's file nor meets A's $5,000 budget."""
    pooled = _pooled()
    _send_to(pooled, KEY, team, monkeypatch)
    assert json.loads(cloud_standing.box_path().read_text())["workspace"] == WS_A
    _machine(team, 300)
    code, out, _err = _status(pooled, environ={cc.KEY_ENV: KEY_B})
    assert code == 0 and "$9,000.00" in out
    assert "Not kept for the guard" in out and "another workspace" in out
    assert not cloud_standing.path().exists()
    assert guard.gate_command("ls -la") is None
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    assert guard.gate_command("ls -la") is None


def test_the_workspace_this_machine_sends_to_keeps_its_standing_against_another(
        team, monkeypatch):
    """A's own standing ($5,420) asks; reading B's ($200) with B's key on the
    same box leaves A's in place, so the ask stands."""
    pooled = PooledBox({KEY: (WS_A, answer(team_usd="5420")),
                        KEY_B: (WS_B, answer(team_usd="200", workspace_ref=WS_B))})
    _send_to(pooled, KEY, team, monkeypatch)
    _machine(team, 300)
    assert _status(pooled)[0] == 0
    kept = cloud_standing.path().read_bytes()
    assert json.loads(kept)["workspace"] == WS_A
    assert guard.gate_command("ls -la")["decision"] == "ask"
    assert _status(pooled, environ={cc.KEY_ENV: KEY_B})[0] == 0
    assert cloud_standing.path().read_bytes() == kept
    assert guard.gate_command("ls -la")["decision"] == "ask"


def test_a_standing_file_from_another_workspace_is_not_used_and_doctor_says_why(team):
    """Written by hand, or by a version before this one: the box matches,
    the workspace does not."""
    cloud_standing.bind(BOX, workspace=WS_A)
    _machine(team, 300)
    _keep(answer(team_usd="9000", workspace_ref=WS_B))
    assert guard.gate_command("ls -la") is None
    d = guard.doctor()["ai_budgets"]["box_standing"]
    assert d["used"] is False and "another workspace" in d["why"]


def test_a_send_to_a_box_that_names_no_workspace_binds_none_a_standing_matches(
        team, monkeypatch):
    _send_to(StandingBox(workspace_ref=None), KEY, team, monkeypatch)
    assert json.loads(cloud_standing.box_path().read_text())["workspace"] is None
    _machine(team, 300)
    _keep(answer(team_usd="9000"))
    assert guard.gate_command("ls -la") is None
    why = guard.doctor()["ai_budgets"]["box_standing"]["why"]
    assert "named no workspace" in why and "nable cloud connect" in why


def test_an_answer_that_names_no_workspace_is_not_kept(laptop):
    doc = answer()
    del doc["workspace_ref"]
    code, out, _err = _status(StandingBox(standing=doc))
    assert code == cli_cloud.EXIT_REFUSED and "names no workspace" in out
    assert not cloud_standing.path().exists()
    for bad in ("", "short", "ws_" + "x" * 200, "ws_a/../b", 12345678):
        assert cloud_standing.workspace_ref(bad) is None


def test_another_persons_standing_is_shown_and_never_replaces_the_guards(laptop):
    """`--as bob` reads bob's standing for a team lead; the guard's own file
    for alice stays, so her team's breach still asks."""
    assert _status(StandingBox())[0] == 0
    kept = cloud_standing.path().read_bytes()
    code, out, _err = _status(StandingBox(standing=answer(person="bob@acme.example")),
                              "--as", "bob@acme.example")
    assert code == 0 and "Standing for bob@acme.example (--as)" in out
    assert "Not kept for the guard" in out and "alice@acme.example" in out
    assert cloud_standing.path().read_bytes() == kept


def test_the_ledger_and_the_payload_never_carry_the_boxs_figure(team, monkeypatch):
    """The ledger goes to whichever workspace this machine sends it to: the
    box's figure, host and times stay out of its lines, reason included."""
    from finops import guard_ledger
    _machine(team, 300)
    _keep(answer(team_usd="5420"))
    v = guard.gate_command("ls -la", record=True)
    assert "$5,420.00" in v["reason"] and _names_box_host(v["reason"])
    raw = guard_ledger.ledger_path().read_text()
    line = json.loads(raw.splitlines()[-1])
    assert (line["decision"], line["action_type"]) == ("ask", "ai_budget")
    assert "the month to date of the team payments was read by" in line["reason"]
    for leak in ("5420", "5,420", "box.example.com", YESTERDAY, "2026-10-04", "2026-10-01"):
        assert leak not in raw, leak
    monkeypatch.setenv("FINOPS_ORG_DIR", str(team["org"]))
    payload = cc.build(agent_usage=False)
    assert b"5420" not in payload.data and b"5,420" not in payload.data
    assert b"box.example.com" not in payload.data


def test_a_figure_is_dated_by_its_own_records_not_the_workspaces_newest(team):
    """as_of is the newest receipt from anyone in the workspace; the reason
    also says how far the team's own records run."""
    _machine(team, 300)
    _keep(answer(team_usd="5420"))
    r = guard.gate_command("ls -la")["reason"]
    assert "the newest it received from anyone in the workspace" in r
    assert "3 of the team's 4 people sent" in r
    assert "their records running to 2026-10-04" in r
    assert "the oldest of their last days 2026-10-01" in r


def test_a_standing_the_plan_does_not_include_is_said_and_exits_0(laptop):
    for error in ("not_on_plan", "plan_required"):
        code, out, err = _status(StandingBox(refuse=403, refuse_error=error))
        assert code == 0, (error, out, err)
        assert "plan does not include a person's standing" in out and error in out
        assert "analyst key" not in out
        assert "not read for alice@acme.example (git user.email)" in out
    code, out, _err = _status(StandingBox(refuse=403, refuse_error="not_on_plan"), "--json")
    got = json.loads(out)["standing"]
    assert code == 0 and got["read"] is False and got["asked"] is True and got["plan"] is True


@pytest.mark.parametrize("person", ["x" * 3000 + "@acme.example", "nobody-in-particular",
                                    "alice@acme.example\nX-Injected: 1"],
                         ids=["3000 characters", "not an email", "two lines"])
def test_a_person_the_box_would_not_take_is_never_sent(laptop, monkeypatch, person):
    monkeypatch.setenv("FINOPS_GUARD_PERSON", person)
    box = StandingBox()
    code, out, err = _status(box)
    assert code == 0 and box.standing_posts == []
    assert "FINOPS_GUARD_PERSON is not an email or github:LOGIN" in out
    assert person not in out + err and "x" * 100 not in out + err


# ── the guard's own file, and no network ─────────────────────────────────────

@pytest.mark.parametrize("cmd", ["rm {p}", "rm -f {p}", "echo '{{}}' > {p}",
                                 "cp /dev/null {p}", "mv /tmp/x {p}"])
def test_an_agent_writing_or_removing_the_standing_is_asked(team, cmd):
    _keep(answer())
    v = guard.gate_command(cmd.format(p=cloud_standing.path()))
    assert v is not None and v["decision"] == "ask"
    assert "month to date the AI budget gate compares" in v["reason"]
    v = guard.gate_command(cmd.format(p=cloud_standing.box_path()))
    assert v is not None and v["decision"] == "ask"


@pytest.fixture
def offline(monkeypatch):
    calls: list[str] = []

    def no_connect(self, *a, **k):
        calls.append("socket")
        raise AssertionError("the budget gate opened a connection")

    def no_open(self, *a, **k):
        calls.append("urllib")
        raise AssertionError("the budget gate opened a URL")
    monkeypatch.setattr(socket.socket, "connect", no_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", no_connect)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", no_open)
    return calls


SCENARIOS = {
    "allow": (300, None, None, None),
    "allow, box under": (300, "1000", None, None),
    "warn from the box": (300, "4500", None, "warn"),
    "ask from the box": (300, "5420", None, "ask"),
    "deny from the box": (300, "5420", "1", "deny"),
    "ask from this machine": (5420, "200", None, "ask"),
    "deny from this machine": (5420, "200", "1", "deny"),
    "ignored standing": (5420, "bad", None, "ask"),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_no_path_of_the_budget_gate_opens_a_connection(team, monkeypatch, offline, name):
    usd, theirs, stop, want = SCENARIOS[name]
    _machine(team, usd)
    if theirs == "bad":
        cloud_standing.path().parent.mkdir(parents=True, exist_ok=True)
        cloud_standing.path().write_text("{")
    elif theirs is not None:
        _keep(answer(team_usd=theirs))
    if stop:
        monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", stop)
    v = guard.check_budget_gate("s-1", str(team["tmp"]))
    assert (v["decision"] if v else None) == want
    v = guard.gate_command("ls -la", cwd=str(team["tmp"]))
    assert (v["decision"] if v else None) == want
    assert offline == []


def test_the_ai_budget_json_gate_opens_no_connection_either(team, monkeypatch, offline):
    (team["org"] / "budgets.yaml").unlink()
    _machine(team, 300)
    ab.set_budget(spend_cap=1)
    assert guard.gate_command("ls -la")["decision"] == "ask"
    monkeypatch.setenv("FINOPS_GUARD_STOP_ON_BUDGET", "1")
    assert guard.gate_command("ls -la")["decision"] == "deny"
    assert offline == []


def test_the_guard_module_names_no_network_client():
    """The gate's modules import no HTTP client: cloud_standing reads a file."""
    import ast
    import inspect
    for mod in (cloud_standing,):
        tree = ast.parse(inspect.getsource(mod))
        names = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                 for a in n.names} | {n.module for n in ast.walk(tree)
                                      if isinstance(n, ast.ImportFrom) and n.module}
        assert not names & {"urllib", "urllib.request", "http", "http.client", "socket",
                            "cloud_connect", "requests", "httpx"}, names


def test_the_ledger_line_keeps_decision_and_action_type_and_adds_the_figures(team):
    from finops import guard_ledger
    _machine(team, 300)
    _keep(answer(team_usd="5420"))
    guard.gate_command("ls -la", record=True)
    line = [json.loads(x) for x in guard_ledger.ledger_path().read_text().splitlines()][-1]
    assert (line["decision"], line["action_type"]) == ("ask", "ai_budget")
    assert line["budget_figure"] == {"compared": "box", "machine_usd": "300.00",
                                     "box_figure_read": True}


def test_the_standing_file_keeps_no_key_and_no_more_than_it_checked(laptop):
    box = StandingBox(standing={**answer(), "secret": KEY, "extra": {"key": KEY}})
    assert _status(box)[0] == 0
    raw = cloud_standing.path().read_text()
    assert KEY not in raw and "secret" not in raw and "extra" not in raw


def test_now_is_injectable_and_the_age_is_from_as_of(laptop):
    doc = cloud_standing.normalize(answer(), box=BOX, person="alice@acme.example",
                                   fetched_at=AT)
    assert cloud_standing.age_hours(doc, now=AT.timestamp()) == pytest.approx(24)
    doc["as_of"] = None
    assert cloud_standing.age_hours(doc, now=(AT + timedelta(hours=3)).timestamp()) == \
        pytest.approx(3)


def test_the_file_lives_beside_the_ledger_and_is_a_guard_file(laptop):
    from finops import guard_paths
    names = {os.path.basename(e.path) for e in guard_paths.protected()}
    assert {"cloud-standing.json", "cloud-box.json"} <= names
    assert datetime.fromisoformat(YESTERDAY).tzinfo == UTC


# The enterprise route's answer for its own fixture (nable-enterprise
# tests/agent_usage/test_standing.py, two workspaces, the person in both),
# as its assertions pin it, "status" on each part included: this version
# reads past keys it does not keep.
RECORDED = {
    "month": "2026-10", "as_of": "2026-10-17T12:00:00+00:00",
    "workspace_ref": "ws_0c5f2b0f8f7e4c1a9d3b6e2f1a0b9c8d",
    "person": {"id": "alice@acme.example", "usd": "5.75", "days": 2, "last_day": "2026-10-16",
               "budget": {"monthly_usd": None, "per_person_usd": "4", "state": "over"},
               "status": "ok"},
    "teams": [{"team": "payments", "usd": "8.75", "people": 3, "people_sent": 2,
               "latest_day": "2026-10-16", "oldest_last_day": "2026-10-15",
               "budget": {"monthly_usd": "9", "per_person_usd": "4", "state": "close"},
               "status": "ok"}],
    "org": {"usd": "11.5", "people_sent": 3, "latest_day": "2026-10-16",
            "budget": {"monthly_usd": "100", "per_person_usd": None, "state": "under"},
            "status": "ok"},
    "budgets_from": {"version": 1, "uploaded_at": "2026-10-17T12:00:00+00:00"},
    "basis": "list-price equivalent"}
RECORDED_NONE = {
    "month": "2026-10", "as_of": None,
    "workspace_ref": "ws_0c5f2b0f8f7e4c1a9d3b6e2f1a0b9c8d",
    "person": {"id": "dana@acme.example", "usd": None, "days": 0, "last_day": None,
               "budget": None, "status": "no records received"},
    "teams": [], "org": {"usd": None, "people_sent": 0, "latest_day": None, "budget": None,
                         "status": "no records received"},
    "budgets_from": None, "basis": "list-price equivalent"}


def test_the_enterprise_answer_reads_on_the_laptop():
    from decimal import Decimal
    doc = cloud_standing.normalize(RECORDED, box=BOX, person="alice@acme.example",
                                   fetched_at=AT)
    assert cloud_standing.figure(doc, "team", "team:payments") == Decimal("8.75")
    assert cloud_standing.figure(doc, "per_person", "team:payments") == Decimal("5.75")
    assert cloud_standing.figure(doc, "org", "org:org") == Decimal("11.5")
    assert cloud_standing.figure(doc, "team", "team:search") is None
    none = cloud_standing.normalize(RECORDED_NONE, box=BOX, person="dana@acme.example",
                                    fetched_at=AT)
    assert cloud_standing.figure(none, "person", "person:dana@acme.example") is None
    assert cloud_standing.figure(none, "org", "org:org") is None
    assert "no records received" in cli_cloud._standing_text(
        {"read": True, "standing": none, "file": "f", "person_from": "--as"})


# ── each figure's own as_of (the iteration 7 carry-over) ────────────────────
# The box sends an as_of on each figure (the person's, each team's, the
# org's): the latest receipt among the records in that figure. The guard's
# reason, its age and doctor date the compared figure by it, and fall back
# to the answer's top-level as_of only for a box that sends none.

TEAM_AS_OF = (AT - timedelta(days=6)).isoformat()
PERSON_AS_OF = (AT - timedelta(hours=30)).isoformat()


def _own(**over):
    doc = answer(**over)
    doc["person"]["as_of"] = PERSON_AS_OF
    doc["teams"][0]["as_of"] = TEAM_AS_OF
    doc["org"]["as_of"] = None
    return doc


def test_each_figures_own_as_of_is_kept_and_read_back(laptop):
    _keep(_own())
    doc, why = cloud_standing.read()
    assert why is None
    assert doc["figures"]["person"]["as_of"] == PERSON_AS_OF
    assert doc["figures"]["teams"][0]["as_of"] == TEAM_AS_OF
    assert doc["figures"]["org"]["as_of"] is None and "as_of" in doc["figures"]["org"]
    assert cloud_standing.figure_as_of(doc, "team", "team:payments") == (TEAM_AS_OF, True)
    assert cloud_standing.figure_as_of(doc, "person", "person:x") == (PERSON_AS_OF, True)


def test_a_box_that_sends_no_figure_as_of_keeps_none(laptop):
    _keep(answer())
    doc, _why = cloud_standing.read()
    assert "as_of" not in doc["figures"]["person"]
    assert all("as_of" not in t for t in doc["figures"]["teams"])
    assert "as_of" not in doc["figures"]["org"]
    assert cloud_standing.figure_as_of(doc, "team", "team:payments") == (YESTERDAY, False)


def test_a_figure_as_of_that_is_not_a_time_refuses_the_answer(laptop):
    bad = _own()
    bad["teams"][0]["as_of"] = "last tuesday"
    with pytest.raises(cloud_standing.StandingError, match="as_of"):
        cloud_standing.normalize(bad, box=BOX, person="alice@acme.example", fetched_at=AT)


def test_the_reason_dates_the_compared_figure_by_its_own_as_of(team):
    _machine(team, 300)
    _keep(_own(team_usd="5420"))
    r = guard.gate_command("ls -la")["reason"]
    assert TEAM_AS_OF in r and "6 days old" in r, r
    assert YESTERDAY not in r and "24 hours old" not in r
    assert "the newest of the team's records it received" in r
    assert "from anyone in the workspace" not in r


def test_the_age_falls_back_to_the_answers_as_of_only_for_a_box_that_sends_none(team):
    _machine(team, 300)
    _keep(answer(team_usd="5420"))
    r = guard.gate_command("ls -la")["reason"]
    assert YESTERDAY in r and "24 hours old" in r
    assert "the newest it received from anyone in the workspace" in r


def test_doctor_dates_each_budgets_figure_by_its_own_as_of(team):
    _machine(team, 300)
    _keep(_own(team_usd="5420"))
    cloud_standing.bind(BOX, workspace=WS_A)
    covered = " ".join(guard.doctor()["covered"])
    assert f"the team payments as of {TEAM_AS_OF}" in covered and "6 days old" in covered
    assert YESTERDAY not in covered


def test_the_figure_age_is_from_its_own_as_of(laptop):
    doc = cloud_standing.normalize(_own(), box=BOX, person="alice@acme.example",
                                   fetched_at=AT)
    assert cloud_standing.figure_age_hours(doc, "team", "team:payments",
                                           now=AT.timestamp()) == pytest.approx(144)
    assert cloud_standing.figure_age_hours(doc, "person", "person:a",
                                           now=AT.timestamp()) == pytest.approx(30)
    old = cloud_standing.normalize(answer(), box=BOX, person="alice@acme.example",
                                   fetched_at=AT)
    assert cloud_standing.figure_age_hours(old, "team", "team:payments",
                                           now=AT.timestamp()) == pytest.approx(24)
