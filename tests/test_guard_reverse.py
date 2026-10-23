# SPDX-License-Identifier: Apache-2.0
"""`nable guard reverse LINE_HASH [--why TEXT]` (D174).

A person, or an agent, says a call in a class that moves no money was
undone: the tag was wrong, the instance should not have stopped. It appends
an outcome line, `reversed`, naming that verdict line by its hash, through
guard_ledger.append, so the chain stays whole. It only narrows: once the
ledger is sent, the workspace demotes the class at once (D176). It refuses
a hash not in the ledger, a line in no class, and a line that never ran.
verify-log still verifies the chain, and ask_outcomes answers exactly as it
did before the line.
"""
from __future__ import annotations

import json

import finops.guard_ledger as gl
from finops import setup_wizard

TAG = "aws ec2 create-tags --resources i-0abc1234 --tags Key=team,Value=pay"


def _line(rec: dict) -> str:
    assert gl.append(rec)
    return gl._sha(gl.ledger_path().read_bytes().rstrip(b"\n").rsplit(b"\n", 1)[-1])


def _verdict(decision="allow", cls="tag", tuid="toolu_1") -> str:
    return _line({"harness": "claude-code", "session": "s-1", "tool": "Bash",
                  "command": gl.redact(TAG), "decision": decision,
                  "action_type": "authority_class" if cls else "infra_apply",
                  **({"authority_class": cls, "rung": "bounded"} if cls else {}),
                  "tool_use_id": tuid})


def _ran(verdict: str, cls="tag") -> str:
    return _line({"kind": "outcome", "outcome": "ran", "harness": "claude-code",
                  "session": "s-1", "tool": "Bash", "verdict": verdict,
                  "linked_by": "tool_use_id", **({"authority_class": cls} if cls else {})})


def cli(*args: str) -> int:
    try:
        setup_wizard.main(["guard", "reverse", *args])
    except SystemExit as e:
        return int(e.code or 0)
    return 0


def _records() -> list[dict]:
    return [json.loads(x) for x in gl.ledger_path().read_text().splitlines()]


def test_it_appends_a_reversed_outcome_for_a_class_line_that_ran(capsys):
    h = _verdict()
    _ran(h)
    assert cli(h, "--why", "wrong team") == 0
    last = _records()[-1]
    assert (last["kind"], last["outcome"], last["verdict"], last["authority_class"]) == (
        "outcome", "reversed", h, "tag")
    assert last["why"] == "wrong team"
    out = capsys.readouterr().out
    assert "demotes tag" in out and "nable cloud connect" in out


def test_a_short_unique_prefix_names_the_line():
    h = _verdict()
    _ran(h)
    assert cli(h[:12]) == 0
    assert _records()[-1]["verdict"] == h


def _refused(h: str, why: str, capsys) -> None:
    before = len(_records()) if gl.ledger_path().exists() else 0
    assert cli(h) == 1
    assert why in capsys.readouterr().err
    assert (len(_records()) if gl.ledger_path().exists() else 0) == before


def test_it_refuses_a_hash_not_in_the_ledger(capsys):
    _ran(_verdict())
    _refused("f" * 64, "not in this machine's ledger", capsys)


def test_it_refuses_a_line_in_no_class(capsys):
    h = _verdict("ask", cls=None)
    _ran(h, cls=None)
    _refused(h, "in no class", capsys)


def test_it_refuses_a_class_line_that_never_ran(capsys):
    _refused(_verdict(), "never ran", capsys)


def test_it_refuses_a_ran_that_is_not_the_line_itself():
    h = _verdict()
    ran = _ran(h)
    assert cli(ran) == 1


def test_a_line_reversed_once_is_not_reversed_again(capsys):
    h = _verdict()
    _ran(h)
    assert cli(h) == 0
    assert cli(h) == 1
    assert "already" in capsys.readouterr().err
    assert sum(1 for r in _records() if r.get("outcome") == "reversed") == 1


def test_verify_log_still_verifies_and_ask_outcomes_answer_as_before():
    ask = _verdict("ask", tuid="toolu_2")
    _ran(ask)
    h = _verdict()
    _ran(h)
    before = gl.ask_outcomes(gl.read(outcomes=True))
    assert cli(h, "--why", "x") == 0
    assert gl.verify()["ok"] and gl.check()["clean"]
    assert gl.ask_outcomes(gl.read(outcomes=True)) == before
    try:
        setup_wizard.main(["guard", "verify-log"])
    except SystemExit as e:
        assert not e.code


def test_why_is_redacted_and_bounded():
    h = _verdict()
    _ran(h)
    assert cli(h, "--why", "token=ghp_" + "a" * 36 + " " + "x" * 2000) == 0
    why = _records()[-1]["why"]
    assert "ghp_" not in why and len(why) <= 300


def test_with_no_hash_it_lists_class_lines_that_ran(capsys):
    h = _verdict()
    _ran(h)
    assert cli() == 0
    out = capsys.readouterr().out
    assert h[:12] in out and "tag" in out
