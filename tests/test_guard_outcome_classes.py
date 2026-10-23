# SPDX-License-Identifier: Apache-2.0
"""The post hook records a `ran` for a class line, allow or ask (D175, D177).

The box counts executions and approval latency per class that moves no money
from the ledgers people send: a `ran` answering an ask in a class, and the
seconds between the two. A call let through within a bound, or at rung none,
is an allow, and the post hook used to record nothing for an allow. So a
verdict line that carries authority_class gets a `ran` whether it was an
allow or an ask, linked exactly as an ask is (tool_use_id, else the same
session and command within the link window), and the `ran` names the class.
Every other line behaves as before: an allow without the field gets none,
and a deny gets none.
"""
from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import pytest

import finops.guard_ledger as gl
import finops.guard_outcome as go
import finops.guard_plugin as gp

TAG = "aws ec2 create-tags --resources i-0abc1234 --tags Key=team,Value=pay"


@pytest.fixture(autouse=True)
def _machine(monkeypatch):
    monkeypatch.setattr(gp, "is_off", lambda: False)


def _verdict(decision: str, *, cls: str | None = "tag", tuid: str | None = "toolu_1",
             session: str = "s-1", command: str = TAG, harness: str = "claude-code") -> str:
    rec = {"harness": harness, "session": session, "tool": "Bash",
           "command": gl.redact(command), "decision": decision,
           "action_type": "authority_class" if cls else "stop_idle",
           **({"authority_class": cls, "rung": "bounded"} if cls else {}),
           **({"tool_use_id": tuid} if tuid else {})}
    assert gl.append(rec)
    return gl._sha(gl.ledger_path().read_bytes().rstrip(b"\n").rsplit(b"\n", 1)[-1])


def _post(*, tuid: str | None = "toolu_1", session: str = "s-1", command: str = TAG,
          mode: str | None = None) -> None:
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": session,
               "tool_input": {"command": command}, "tool_response": {"stdout": ""}}
    if tuid:
        payload["tool_use_id"] = tuid
    if mode:
        payload["permission_mode"] = mode
    assert go.run_post("claude", io.StringIO(json.dumps(payload)), io.StringIO()) == 0


def _rans() -> list[dict]:
    return [r for r in gl.read(outcomes=True) if gl.is_outcome(r)]


def test_an_allow_line_in_a_class_gets_a_ran_linked_by_tool_use_id():
    h = _verdict("allow")
    _post()
    (ran,) = _rans()
    assert (ran["outcome"], ran["verdict"], ran["linked_by"]) == ("ran", h, "tool_use_id")
    assert ran["authority_class"] == "tag"


def test_an_ask_line_in_a_class_gets_a_ran_naming_its_class():
    h = _verdict("ask")
    _post(mode="default")
    (ran,) = _rans()
    assert (ran["verdict"], ran["authority_class"]) == (h, "tag")
    assert gl.ask_outcomes(gl.read(outcomes=True))[h] == "approved"


def test_an_allow_line_without_the_field_still_gets_none():
    _verdict("allow", cls=None)
    _post()
    assert _rans() == []


def test_a_deny_in_a_class_gets_none():
    _verdict("deny")
    _post()
    assert _rans() == []


def test_without_tool_use_id_the_same_session_and_command_link_it():
    h = _verdict("allow", tuid=None)
    _post(tuid=None)
    (ran,) = _rans()
    assert (ran["verdict"], ran["linked_by"]) == (h, "command")


def test_an_allow_is_answered_once():
    _verdict("allow")
    _post()
    _post()
    assert len(_rans()) == 1


def test_a_ran_under_bypass_permissions_says_so():
    _verdict("ask")
    _post(mode="bypassPermissions")
    (ran,) = _rans()
    assert ran["permission_mode"] == "bypassPermissions"


def test_another_session_s_allow_is_not_linked():
    _verdict("allow", session="s-2", tuid=None)
    _post(tuid=None, session="s-1")
    assert _rans() == []


def test_the_newest_unanswered_line_wins_whatever_its_decision():
    _verdict("ask", tuid=None)
    newer = _verdict("allow", tuid=None)
    _post(tuid=None)
    (ran,) = _rans()
    assert ran["verdict"] == newer


def test_find_ask_reads_class_lines_old_and_new_alike():
    now = datetime.now(UTC)
    obs = {"harness": "claude-code", "tool_use_id": None, "_command": TAG,
           "_session": "s-1"}
    recs = [{"harness": "claude-code", "decision": "allow", "session": "s-1",
             "command": gl.redact(TAG), "ts": now.isoformat(), "_hash": "h1"}]
    assert go.find_ask(obs, recs, now=now) is None
    recs[0]["authority_class"] = "ticket"
    assert go.find_ask(dict(obs), recs, now=now) == (recs[0], "command")
