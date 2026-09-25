"""`nable guard export`: the verified ledger, for a SIEM.

Invariants under test:
  - every exported record carries its chain hash, and the export can be
    re-verified on the receiving side from those hashes alone
  - a ledger that does not verify is refused, or with --force exported with
    each record saying whether its link held
  - CEF lines are well formed and escaped
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import stat
from datetime import UTC, datetime, timedelta

import pytest

import finops.guard as g
import finops.guard_ledger as gl
from finops import __version__, ai_budget


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_STOP_ON_BUDGET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})


def _ago(**kw) -> str:
    return (datetime.now(UTC) - timedelta(**kw)).isoformat(timespec="seconds")


def _activity():
    gl.append({"ts": _ago(days=10), "decision": "allow", "action_type": "infra_apply",
               "command": "terraform apply", "harness": "claude-code"})
    g.gate_command("terraform destroy -auto-approve", session_id="s1")
    g.gate_command("aws ec2 run-instances --instance-type p4d.24xlarge --count 8",
                   session_id="s1")


def _export(**kw):
    from finops import setup_wizard
    kw.setdefault("guard_format", "jsonl")
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        setup_wizard._run_guard(argparse.Namespace(guard_action="export", guard_global=False,
                                                   **kw))
    return out.getvalue(), err.getvalue()


def test_every_record_carries_its_chain_hash():
    _activity()
    out, _ = _export()
    recs = [json.loads(line) for line in out.splitlines()]
    raw = gl.ledger_path().read_bytes().splitlines()
    assert len(recs) == len(raw) == 3
    for rec, line in zip(recs, raw, strict=True):
        assert rec["chain"]["hash"] == hashlib.sha256(line).hexdigest()
        assert rec["chain"]["ok"] is True
    assert recs[-1]["chain"]["hash"] == gl.verify()["head"]
    assert recs[1]["session"] == "s1" and recs[1]["decision"] == "ask"


def test_the_receiver_can_re_verify_the_chain_from_the_export():
    _activity()
    out, _ = _export()
    recs = [json.loads(line) for line in out.splitlines()]
    prev = gl.GENESIS
    for rec in recs:
        assert rec["prev"] == prev
        prev = rec["chain"]["hash"]


@pytest.mark.parametrize("since,count", [("24h", 2), ("30d", 3), ("2100-01-01", 0)])
def test_since_filters_by_time_but_keeps_each_records_hash(since, count):
    _activity()
    out, _ = _export(guard_since=since)
    recs = [json.loads(line) for line in out.splitlines()]
    assert len(recs) == count
    if count == 2:
        assert recs[0]["chain"]["line"] == 2, "line numbers are the ledger's, not the export's"


def test_since_takes_an_iso_date():
    _activity()
    day = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()
    out, _ = _export(guard_since=day)
    assert len(out.splitlines()) == 2


def test_a_bad_since_is_a_usage_error():
    with pytest.raises(SystemExit) as e:
        _export(guard_since="yesterday-ish")
    assert e.value.code == 2


def test_a_broken_chain_is_refused():
    _activity()
    p = gl.ledger_path()
    p.write_bytes(p.read_bytes().replace(b'"allow"', b'"deny"', 1))
    with pytest.raises(SystemExit) as e:
        _export()
    assert e.value.code == 1


def test_a_broken_chain_exports_with_force_and_says_where():
    _activity()
    p = gl.ledger_path()
    p.write_bytes(p.read_bytes().replace(b'"allow"', b'"deny"', 1))
    out, err = _export(guard_force=True)
    assert "exporting anyway" in err and "breaks at line 2" in err
    recs = [json.loads(line) for line in out.splitlines()]
    assert [r["chain"]["ok"] for r in recs] == [True, False, True]


def test_records_cut_since_the_last_check_are_refused_too():
    _activity()
    gl.save_anchor(gl.verify())
    p = gl.ledger_path()
    p.write_bytes(b"".join(p.read_bytes().splitlines(keepends=True)[:1]))
    with pytest.raises(SystemExit):
        _export()


def test_an_unparseable_line_is_exported_as_a_visible_gap():
    _activity()
    with gl.ledger_path().open("ab") as fh:
        fh.write(b"not json\n")
    out, _ = _export(guard_force=True)
    last = json.loads(out.splitlines()[-1])
    assert last["unparseable"] is True and last["chain"]["ok"] is False


def test_out_writes_an_owner_only_file(tmp_path):
    _activity()
    target = tmp_path / "ledger.jsonl"
    out, err = _export(guard_out=str(target))
    assert out == "" and "3 record(s) written" in err
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert len(target.read_text().splitlines()) == 3


# ── CEF ───────────────────────────────────────────────────────────────────────

_CEF = re.compile(r"^CEF:0\|nable\|guard\|[^|]+\|[^|]+\|[^|]+\|\d+\|(.*)$")


def test_cef_lines_are_well_formed():
    _activity()
    out, _ = _export(guard_format="cef")
    lines = out.splitlines()
    assert len(lines) == 3
    heads = [line.split("|")[:7] for line in lines]
    assert heads[1] == ["CEF:0", "nable", "guard", __version__, "ask",
                        "guard ask delete_resource", "5"]
    for line in lines:
        assert _CEF.match(line), line
    ext = lines[1]
    assert "cs4Label=chainHash cs4=" in ext and "cs3Label=session cs3=s1" in ext
    assert "act=ask" in ext and "rt=" in ext
    head = gl.verify()["head"]
    assert f"cs4={head}" in lines[-1]


def test_cef_escapes_what_it_must():
    rec = {"ts": _ago(minutes=1), "decision": "deny", "action_type": "delete_resource",
           "command": "a=b | c\\d", "reason": "line one\nline two",
           "chain": {"line": 1, "hash": "h", "prev": "p", "ok": True}}
    line = gl.to_cef(rec, "1.0|x")
    assert "|1.0\\|x|" in line
    assert "cs1=a\\=b | c\\\\d" in line
    assert "msg=line one\\nline two" in line and "\n" not in line


def test_parse_since_forms():
    now = datetime(2026, 9, 24, 12, tzinfo=UTC)
    assert gl.parse_since("24h", now=now) == now - timedelta(hours=24)
    assert gl.parse_since("2w", now=now) == now - timedelta(weeks=2)
    assert gl.parse_since("2026-09-01") == datetime(2026, 9, 1, tzinfo=UTC)
    assert gl.parse_since(None) is None
    with pytest.raises(ValueError):
        gl.parse_since("soon")


def test_export_arguments_parse():
    from finops import setup_wizard
    seen = {}
    real = setup_wizard._run_guard
    setup_wizard._run_guard = lambda parsed: seen.update(vars(parsed))
    try:
        setup_wizard.main(["guard", "export", "--since", "7d", "--format", "cef",
                           "--out", "x.cef", "--force"])
    finally:
        setup_wizard._run_guard = real
    assert (seen["guard_action"], seen["guard_since"], seen["guard_format"],
            seen["guard_out"], seen["guard_force"]) == ("export", "7d", "cef", "x.cef", True)
