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


def answer(*, person="alice@acme.example", team_usd="5420", person_usd="120.5",
           org_usd="9000", month="2026-10", as_of=YESTERDAY, **over):
    """The box's standing answer, in the shape the enterprise route gives."""
    doc = {"month": month, "as_of": as_of, "basis": "list-price equivalent",
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

    def __init__(self, *, reads=("standing",), standing=None, refuse=None, **kw):
        super().__init__(**kw)
        self.reads = list(reads)
        self.answer = standing if standing is not None else answer()
        self.refuse = refuse

    def status(self) -> dict:
        return {**super().status(), "reads": self.reads,
                "accepts": ["org_model", "ledger_line", "agent_usage", "org_file_delete"]}

    def __call__(self, req: urllib.request.Request, timeout: float):
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        if req.get_method() == "POST" and path == "api/free-tool/standing":
            self.requests.append({"method": "POST", "url": req.full_url,
                                  "headers": dict(req.header_items()), "body": req.data})
            if self.refuse:
                return self.refuse, json.dumps({"error": "plan_required",
                                                "message": "not active"}).encode()
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
                  "never their sum", str(path)):
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
    assert "does not offer a person's standing yet" in out
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
    assert code == cli_cloud.EXIT_REFUSED and "403" in out and "plan_required" in out
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
    assert "$5,420.00" in r and "box.example.com" in r and YESTERDAY in r
    assert "24 hours old" in r and "$300.00" in r
    assert "The larger, the box's $5,420.00, is the one compared" in r
    assert "$5,720" not in r and "5720" not in r
    assert v["budget_figure"] == {"compared": "box", "machine_usd": "300.00",
                                  "box_usd": "5420", "box_host": "box.example.com",
                                  "box_as_of": YESTERDAY}
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
    cloud_standing.bind(BOX)
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
    assert line["budget_figure"]["compared"] == "box"
    assert line["budget_figure"]["box_usd"] == "5420"


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
