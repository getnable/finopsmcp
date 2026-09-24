"""The guard's memory: verdicts that depend on what it already let through.

Invariants under test:
  - the velocity cap sums the priced monthly run-rate the guard ALLOWED (or
    warned on) in a rolling window, and asks when this action would take the
    window over the cap, naming the total, the cap and the actions behind it
  - asks and denies do not count toward it: the ledger cannot see a human's
    answer, and a declined launch was never spent
  - history can only tighten: an allow or warn may become an ask, nothing else
  - reading the ledger is a bounded read from the end of the file
  - any failure in a history check fails open, and is recorded as one
"""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta

import pytest

import finops.guard as g
import finops.guard_ledger as gl
from finops import ai_budget
from finops.aws_prices import EC2_HOURLY

M5_2XL = "aws ec2 run-instances --instance-type m5.2xlarge"
M5_2XL_MONTHLY = EC2_HOURLY["m5.2xlarge"] * 730


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_VELOCITY_CAP_USD",
                "FINOPS_POLICY_VELOCITY_WINDOW_MIN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})


def _records():
    p = gl.ledger_path()
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def _ago(minutes: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _seed(decision: str, monthly: float, minutes_ago: float, command: str = M5_2XL) -> None:
    gl.append({"ts": _ago(minutes_ago), "decision": decision, "monthly_usd": monthly,
               "action_type": "infra_apply", "command": command})


# ── velocity cap ──────────────────────────────────────────────────────────────

def test_launches_each_under_the_threshold_ask_once_the_window_is_over_the_cap(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_VELOCITY_CAP_USD", "1000")
    for _ in range(3):
        assert g.gate_command(M5_2XL) is None          # 3 x ~$280/mo, silent
    v = g.gate_command(M5_2XL)                          # the fourth crosses $1,000
    assert v is not None and v["decision"] == "ask"
    assert v["history"] == "velocity"
    total = 3 * M5_2XL_MONTHLY
    assert f"~${total:,.0f}/mo already let through in the last 60 minutes (3 actions" in v["reason"]
    assert f"~${total + M5_2XL_MONTHLY:,.0f}/mo in 60 minutes" in v["reason"]
    assert "over your $1,000/mo velocity cap" in v["reason"]
    assert M5_2XL in v["reason"], "the actions that make up the total are named"
    last = _records()[-1]
    assert (last["decision"], last["history"]) == ("ask", "velocity")


def test_the_default_cap_catches_the_incident_shape():
    # Each launch sits under the $500/mo per-action threshold, which is how
    # thousands of dollars of oversized infrastructure got through one at a time.
    decisions = [(g.gate_command(M5_2XL) or {}).get("decision", "allow") for _ in range(9)]
    assert decisions[:7] == ["allow"] * 7                # 7 x ~$280 = ~$1,962
    assert decisions[7] == "ask"                         # the 8th makes ~$2,243 > $2,000


def test_the_default_cap_moves_with_the_per_action_threshold(monkeypatch):
    from finops.policy import load_policy, velocity_cap
    assert velocity_cap(load_policy()) == 2000.0
    monkeypatch.setenv("FINOPS_POLICY_MAX_AUTO_USD", "300")
    assert velocity_cap(load_policy()) == 1200.0
    decisions = [(g.gate_command(M5_2XL) or {}).get("decision") for _ in range(5)]
    assert decisions == ["warn"] * 4 + ["ask"]           # ~$280 is 93% of $300
    monkeypatch.setenv("FINOPS_POLICY_VELOCITY_CAP_USD", "0")
    assert velocity_cap(load_policy()) == 0.0


def test_only_what_the_guard_let_through_counts(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_VELOCITY_CAP_USD", "1000")
    _seed("ask", 50_000, 5)            # the human may have said no
    _seed("deny", 50_000, 5)           # never ran
    _seed("fail_open", 50_000, 5)
    _seed("warn", 450, 5)
    assert g.gate_command(M5_2XL) is None                # $450 + $280 is under $1,000
    _seed("allow", 300, 2)
    v = g.gate_command(M5_2XL)
    assert v["decision"] == "ask"
    assert "(3 actions" in v["reason"]                   # the warn, the allow, the silent launch


def test_records_older_than_the_window_do_not_count(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_VELOCITY_CAP_USD", "1000")
    _seed("allow", 5_000, 61)
    assert g.gate_command(M5_2XL) is None
    monkeypatch.setenv("FINOPS_POLICY_VELOCITY_WINDOW_MIN", "120")
    v = g.gate_command(M5_2XL)
    assert v["decision"] == "ask" and "in the last 120 minutes" in v["reason"]


def test_a_cap_of_zero_turns_it_off(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_VELOCITY_CAP_USD", "0")
    _seed("allow", 1_000_000, 1)
    assert g.gate_command(M5_2XL) is None


def test_one_action_over_a_low_cap_says_so(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_VELOCITY_CAP_USD", "100")
    v = g.gate_command(M5_2XL)
    assert v["decision"] == "ask"
    assert "on its own, over your $100/mo velocity cap" in v["reason"]


def test_history_only_tightens(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_VELOCITY_CAP_USD", "1")
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "ticket")
    v = g.gate_command(M5_2XL)
    assert v["decision"] == "deny" and "history" not in v, "a deny stays a deny"
    monkeypatch.delenv("FINOPS_POLICY_ALLOWED_ACTIONS")
    assert g.gate_command("aws ec2 run-instances") is None, "no price, nothing to sum"


def test_a_warn_is_upgraded_too(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_VELOCITY_CAP_USD", "600")
    _seed("allow", 200, 3)
    v = g.gate_command("aws ec2 run-instances --instance-type c5.4xlarge")
    assert v["decision"] == "ask" and v["history"] == "velocity"


def test_mcp_calls_are_capped_the_same_way(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_VELOCITY_CAP_USD", "500")
    _seed("allow", 400, 3)
    v = g.gate_mcp_call("mcp__aws-api__call_aws", {"cli_command": M5_2XL})
    assert v["decision"] == "ask"
    assert v["reason"].startswith("nable guard: mcp__aws-api__call_aws")
    assert "velocity cap" in v["reason"]


def test_a_ledger_that_cannot_be_read_fails_open_and_is_recorded(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_VELOCITY_CAP_USD", "1")

    def boom(*_a, **_k):
        raise OSError("disk gone")
    monkeypatch.setattr(gl, "recent", boom)
    assert g.gate_command(M5_2XL) is None, "the policy verdict (allow) stands"
    fail, verdict = _records()
    assert (fail["decision"], fail["error"], fail["check"]) == ("fail_open", "OSError", "history")
    assert verdict["decision"] == "allow"


def test_a_ledger_that_cannot_be_read_never_breaks_an_mcp_call(monkeypatch):
    monkeypatch.setenv("FINOPS_POLICY_VELOCITY_CAP_USD", "1")
    monkeypatch.setattr(gl, "recent", lambda *a, **k: 1 / 0)
    assert g.gate_mcp_call("mcp__aws-api__call_aws", {"cli_command": M5_2XL}) is None
    assert [r["decision"] for r in _records()] == ["fail_open", "allow"]


# ── reading the recent end of the ledger ──────────────────────────────────────

def test_recent_returns_the_window_oldest_first():
    for m in (90, 50, 20, 5):
        _seed("allow", m, m)
    got = gl.recent(60)
    assert [r["monthly_usd"] for r in got] == [50, 20, 5]
    assert gl.recent(1) == []


def test_recent_on_a_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(gl, "_path_override", tmp_path / "none.jsonl")
    assert gl.recent(60) == []


def test_recent_reads_lines_that_straddle_chunks(monkeypatch):
    monkeypatch.setattr(gl, "_TAIL_CHUNK", 37)        # far smaller than a record
    for i in range(12):
        _seed("allow", i, 30 - i)
    assert [r["monthly_usd"] for r in gl.recent(60)] == list(range(12))


def test_recent_stops_at_the_first_old_record_without_reading_the_rest(monkeypatch):
    p = gl.ledger_path()
    old = json.dumps({"ts": _ago(600), "decision": "allow", "monthly_usd": 1})
    p.write_text((old + "\n") * 20_000)                # ~1.3 MB of history
    _seed("allow", 7, 1)
    reads = []
    real_open = type(p).open

    def spy(self, *a, **k):
        fh = real_open(self, *a, **k)
        orig = fh.read

        def read(n=-1):
            data = orig(n)
            reads.append(len(data))
            return data
        fh.read = read
        return fh
    monkeypatch.setattr(type(p), "open", spy)
    assert [r["monthly_usd"] for r in gl.recent(60)] == [7]
    assert sum(reads) <= gl._TAIL_CHUNK, "one chunk from the end, not the whole file"


def test_recent_bounds_what_it_reads():
    for i in range(50):
        _seed("allow", i, 1)
    got = gl.recent(60, max_bytes=1024)
    assert 0 < len(got) < 50
    assert got[-1]["monthly_usd"] == 49, "the newest records are the ones kept"


def test_recent_skips_torn_and_naive_lines():
    _seed("allow", 1, 2)
    with gl.ledger_path().open("a") as fh:
        fh.write('{"ts": "2026-01-01T00:00:00", "decision": "allow"}\n')   # no zone
        fh.write("not json\n")
    _seed("allow", 2, 1)
    assert [r["monthly_usd"] for r in gl.recent(60)] == [1, 2]


def test_the_history_path_does_not_import_sqlalchemy():
    code = ("import sys; import finops.guard as g; "
            f"g.gate_command({M5_2XL!r}); "
            "print('sqlalchemy' in sys.modules)")
    import subprocess
    env = {**os.environ, "HOME": str(gl.ledger_path().parent), "NABLE_NO_TELEMETRY": "1",
           "FINOPS_DATA_DIR": str(gl.ledger_path().parent)}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=env, timeout=60, check=False)
    assert r.stdout.strip() == "False", r.stderr
