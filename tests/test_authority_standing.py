"""The workspace's standing on the authority ladder, kept on the laptop for
the guard (authority_standing, D176).

`nable cloud status` reads the box's status answer, whose `authority` block
says, per class that moves no money, the rung in force on the box and
whether a reversal demoted it. The laptop keeps it (authority-standing.json,
0600, beside the guard ledger) only from the box and the workspace it last
sent to, and the guard reads it on the hook path through
guard_paths.read_regular, never waiting on a FIFO. The box can only narrow:
nothing in the block ever widens what authority.yaml says.
"""
from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime

import pytest

from finops import authority_standing as st
from finops import cloud_standing, guard_ledger, guard_paths

BOX = "https://box.nable.example"
OTHER_BOX = "https://other.nable.example"
WS = "ws_" + "a" * 32
OTHER_WS = "ws_" + "b" * 32
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
LINE = "c" * 64


def _block(ws=WS, **classes):
    out = {c: {"rung_in_force": None, "demoted": False, "since": None, "line": None}
           for c in ("tag", "ticket", "offhours_schedule")}
    for c, over in classes.items():
        out[c].update(over)
    return {"v": 1, "read": True, "workspace_ref": ws, "as_of": "2026-10-22T11:00:00+00:00",
            "classes": out}


@pytest.fixture
def bound():
    cloud_standing.bind(BOX, workspace=WS, at=NOW)
    return st.path()


def test_a_block_from_the_box_and_workspace_this_machine_sends_to_is_kept_0600(bound):
    got = st.keep(_block(tag={"demoted": True, "since": "2026-10-20", "line": LINE,
                               "rung_in_force": "approve_each"}),
                  box=BOX, workspace_ref=WS, fetched_at=NOW)
    assert got["kept"] and got["why"] is None and got["file"] == str(bound)
    assert stat.S_IMODE(os.stat(bound).st_mode) == 0o600
    doc = st.for_guard()
    assert doc["read"] and doc["classes"]["tag"]["demoted"]
    assert doc["classes"]["tag"]["since"] == "2026-10-20"
    assert "tag" in st.text(got, _block(tag={"demoted": True, "since": "2026-10-20",
                                              "line": LINE, "rung_in_force": "approve_each"}))


def test_the_file_sits_beside_the_ledger():
    assert st.path() == guard_ledger.ledger_path().parent / "authority-standing.json"


@pytest.mark.parametrize("box,ws", [(OTHER_BOX, WS), (BOX, OTHER_WS)])
def test_a_block_for_another_box_or_workspace_is_not_kept_with_why(bound, box, ws):
    got = st.keep(_block(ws=ws), box=box, workspace_ref=ws, fetched_at=NOW)
    assert not got["kept"] and got["why"]
    assert not bound.exists()


def test_a_block_naming_another_workspace_than_its_status_is_not_kept(bound):
    got = st.keep(_block(ws=OTHER_WS), box=BOX, workspace_ref=WS, fetched_at=NOW)
    assert not got["kept"] and "another workspace" in got["why"]
    assert not bound.exists()


def test_a_machine_that_sent_to_no_box_keeps_nothing():
    got = st.keep(_block(), box=BOX, workspace_ref=WS, fetched_at=NOW)
    assert not got["kept"] and "sent nothing" in got["why"]


def test_no_block_from_an_older_box_keeps_nothing_and_says_so(bound):
    got = st.keep(None, box=BOX, workspace_ref=WS, fetched_at=NOW)
    assert not got["kept"] and "older box" in got["why"]
    assert st.for_guard() is None


def test_a_block_that_says_not_read_is_kept_as_not_read(bound):
    block = {"v": 1, "read": False, "reason": "the ladder pass failed on the last upload",
             "workspace_ref": WS, "as_of": "2026-10-22T11:00:00+00:00"}
    got = st.keep(block, box=BOX, workspace_ref=WS, fetched_at=NOW)
    assert got["kept"] and got["read"] is False
    doc = st.for_guard()
    assert doc["read"] is False and "ladder pass failed" in doc["why"]


@pytest.mark.parametrize("bad", [
    {"rung_in_force": "unbounded"},
    {"demoted": "yes"},
    {"demoted": True, "since": None},
    {"since": "yesterday"},
    {"line": "not a hash"},
    {"extra": 1},
])
def test_a_block_out_of_shape_is_kept_as_not_read_never_as_wider(bound, bad):
    block = _block()
    block["classes"]["tag"].update(bad)
    got = st.keep(block, box=BOX, workspace_ref=WS, fetched_at=NOW)
    assert got["kept"] and got["read"] is False and "refused" in got["why"]
    assert st.for_guard()["read"] is False


def test_a_block_over_its_size_is_refused(bound):
    block = _block()
    block["classes"].update({f"future_{i}": {"rung_in_force": None, "demoted": False,
                                             "since": None, "line": LINE} for i in range(60)})
    got = st.keep(block, box=BOX, workspace_ref=WS, fetched_at=NOW)
    assert got["read"] is False


def test_a_class_this_version_does_not_know_is_left_out(bound):
    block = _block()
    block["classes"]["pager"] = {"rung_in_force": "bounded", "demoted": False, "since": None,
                                 "line": None}
    got = st.keep(block, box=BOX, workspace_ref=WS, fetched_at=NOW)
    assert got["kept"] and got["read"]
    assert set(st.for_guard()["classes"]) == {"tag", "ticket", "offhours_schedule"}


def test_a_standing_kept_from_a_box_this_machine_no_longer_sends_to_is_not_read(bound):
    st.keep(_block(), box=BOX, workspace_ref=WS, fetched_at=NOW)
    cloud_standing.bind(OTHER_BOX, workspace=OTHER_WS, at=NOW)
    doc = st.for_guard()
    assert doc["read"] is False and "last sent to" in doc["why"]


def test_moving_to_an_older_box_removes_the_old_workspace_standing(bound):
    st.keep(_block(tag={"demoted": True, "since": "2026-10-20", "line": LINE}), box=BOX,
            workspace_ref=WS, fetched_at=NOW)
    cloud_standing.bind(OTHER_BOX, workspace=OTHER_WS, at=NOW)
    st.keep(None, box=OTHER_BOX, workspace_ref=OTHER_WS, fetched_at=NOW)
    assert st.for_guard() is None and not bound.exists()


@pytest.mark.parametrize("body", [b"{not json", b"[]", json.dumps({"v": 2}).encode(),
                                  b"x" * (st.MAX_BYTES + 10)])
def test_a_file_that_cannot_be_read_is_not_read_never_absent(bound, body):
    bound.parent.mkdir(parents=True, exist_ok=True)
    bound.write_bytes(body)
    doc = st.for_guard()
    assert doc is not None and doc["read"] is False and doc["why"]


def test_the_hook_reads_it_only_through_guard_paths(bound, monkeypatch):
    st.keep(_block(), box=BOX, workspace_ref=WS, fetched_at=NOW)
    seen = []
    real = guard_paths.read_regular

    def spy(p, most):
        seen.append(str(p))
        return real(p, most)
    monkeypatch.setattr(guard_paths, "read_regular", spy)
    assert st.for_guard()["read"]
    assert str(bound) in seen


def test_a_fifo_in_its_place_is_not_read_and_does_not_wait(bound):
    bound.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(bound)
    doc = st.for_guard()
    assert doc["read"] is False and "not a regular file" in doc["why"]


def test_keep_never_raises_on_anything_the_box_sends(bound):
    for junk in (7, "x", [], {"v": 1}, {"v": 1, "read": True}, {"read": None}):
        got = st.keep(junk, box=BOX, workspace_ref=WS, fetched_at=NOW)
        assert set(got) >= {"kept", "why", "file"}


# ── one of the guard's own files ─────────────────────────────────────────────

def test_an_agent_write_or_delete_of_the_standing_asks(bound):
    from finops import guard
    p = st.path()
    assert guard_paths.match(str(p)) is not None
    assert "authority" in guard_paths.match(str(p)).what
    assert "authority-standing.json" in guard_paths.DISTINCTIVE_NAMES
    for command in (f"rm -f {p}", f"echo '{{}}' > {p}", f"mv /tmp/x.json {p}",
                    f"truncate -s 0 {p}"):
        v = guard.gate_command(command, record=False, cwd=str(p.parent))
        assert v is not None and v["decision"] == "ask", command
    v = guard.gate_editor("Write", {"file_path": str(p), "content": "{}"}, cwd=str(p.parent))
    assert v is not None and v["decision"] == "ask"
