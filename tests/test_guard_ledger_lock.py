"""Nothing that holds the ledger file can hold up a verdict.

`flock -x ~/.finops/guard-ledger.jsonl sleep 999 &` used to hang every
recorded verdict until the harness timed the hook out, and a timed-out hook
fails open. The agent can run that command itself.

Invariants under test:
  - append() waits on another process's lock for a bounded time, then gives
    up on the record and counts it where doctor can see it
  - a FIFO in the ledger's place cannot block the hook either
  - the verdict is answered either way
"""
from __future__ import annotations

import fcntl
import io
import json
import os
import threading
import time

import pytest

import finops.guard as g
import finops.guard_ledger as gl
from finops import ai_budget


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})


@pytest.fixture
def held_lock():
    p = gl.ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    fd = os.open(p, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)           # as `flock -x <ledger> sleep 999` would
    yield p
    os.close(fd)


def _within(seconds: float, fn, *a):
    """Run fn in a thread; fail (without hanging the suite) if it blocks."""
    box: dict = {}
    t = threading.Thread(target=lambda: box.setdefault("out", fn(*a)), daemon=True)
    start = time.perf_counter()
    t.start()
    t.join(seconds)
    assert not t.is_alive(), f"blocked for more than {seconds}s"
    return box.get("out"), time.perf_counter() - start


def test_a_held_lock_costs_the_record_not_the_verdict(held_lock):
    v, took = _within(3, g.gate_command, "terraform destroy")
    assert v["decision"] == "ask"
    assert took < 1.0
    assert held_lock.read_text() == "", "nothing written under someone else's lock"
    lost = gl.unrecorded()
    assert lost["count"] == 1 and lost["why"] == {"locked": 1}


def test_the_hook_answers_while_the_ledger_is_locked(held_lock):
    out = io.StringIO()
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "terraform destroy"}})
    code, took = _within(3, g.run_hook, io.StringIO(payload), out)
    assert code == 0 and took < 1.0
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_a_released_lock_is_waited_for():
    p = gl.ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    fd = os.open(p, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    threading.Timer(0.05, os.close, (fd,)).start()   # another writer, finishing up
    assert gl.append({"decision": "ask"}) is True
    assert gl.unrecorded()["count"] == 0


def test_a_fifo_in_the_ledgers_place_cannot_block():
    p = gl.ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(p)
    ok, took = _within(3, gl.append, {"decision": "ask"})
    assert ok is False and took < 1.0
    assert gl.unrecorded()["why"] == {"not_a_file": 1}


def test_doctor_reports_unrecorded_verdicts(held_lock):
    g.gate_command("terraform destroy")
    d = g.doctor()
    assert d["ledger"]["unrecorded"]["count"] == 1
    assert any("answered but not recorded" in gap for gap in d["not_covered"])
    assert any("lsof" in fix for fix in d["recommendations"])


def test_the_unrecorded_note_carries_no_command(held_lock):
    g.gate_command("PASSWORD=hunter2 terraform destroy")
    blob = gl.ledger_path().with_name(gl.UNRECORDED_NAME).read_text()
    assert "hunter2" not in blob and "terraform" not in blob
