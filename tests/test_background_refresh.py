"""The guard refreshes what it reads in a detached process, and never waits.

The hook answers every tool call from files on disk. The Cursor Admin API
cache went stale (or never existed) for anyone who did not run `nable
ai-budget`, and the cloud spend summary for anyone who did not run `nable
budget refresh`. Invariants under test:
  - a stale or missing Cursor cache starts one detached refresh; a fresh one,
    a failed read inside its backoff, or the kill switch starts none
  - parallel hooks start at most one (a lock file), and a dead one's lock is
    taken over
  - what the guard says keeps naming a stale or unread Cursor figure, and adds
    that a refresh is under way
  - the child does one refresh through harness_usage's own read and atomic
    write, records a failure, releases the lock and exits 0
  - the cloud budget refresh is off unless FINOPS_GUARD_AUTO_REFRESH_BUDGET=1,
    and does not restamp old spend as current
No test starts a real refresh except the one subprocess test, which points
the Admin API at a closed local port.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import finops.guard as g
from finops import ai_budget as ab
from finops import background_refresh as bg
from finops import harness_usage
from finops.budget import summary as bs

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("FINOPS_GUARD_STOP_ON_BUDGET", raising=False)
    # Background refresh is off under pytest; these tests turn it on.
    monkeypatch.setenv("FINOPS_GUARD_BACKGROUND_REFRESH", "1")

    def no_network(body):
        raise AssertionError("the Cursor Admin API was called")

    monkeypatch.setattr(harness_usage, "_cursor_post", no_network)
    # No test starts a process through maybe_start: each spawn is only counted.
    calls: list[str] = []
    monkeypatch.setattr(bg, "_spawn", lambda job, cwd: calls.append(job))
    return calls


@pytest.fixture
def spawned(_isolate):
    return _isolate


def _event(now, cents=1200.0, conversation="conv-1"):
    return {"timestamp": str(int((now - 120) * 1000)), "model": "claude-4.5-sonnet",
            "conversationId": conversation,
            "tokenUsage": {"inputTokens": 126, "outputTokens": 450, "cacheWriteTokens": 0,
                           "cacheReadTokens": 0, "totalCents": cents}}


def _cursor_cache(age_s: float, **extra):
    now = time.time()
    cache = {"email": "", "start_ms": 0, "fetched_at": now - age_s,
             "events": [harness_usage._cursor_event(_event(now))], **extra}
    harness_usage.write_json_atomic(harness_usage._cursor_cache_path(), cache)


@pytest.fixture
def cursor(monkeypatch):
    monkeypatch.setenv("CURSOR_ADMIN_API_KEY", "key_test")  # pragma: allowlist secret
    ab.set_budget(spend_cap=1000)


# ── when the guard starts one ─────────────────────────────────────────────────

def test_a_stale_cursor_cache_starts_one_refresh(cursor, spawned):
    _cursor_cache(2 * harness_usage._CURSOR_TTL)
    st = ab.status(for_gate=True)
    assert spawned == ["cursor"]
    src = st["month_to_date"]["sources"]["cursor"]
    assert src["stale"] is True and src["refreshing"] is True
    assert "Admin API read 2 hours old; refreshing in the background" in st["summary"]
    assert st["est_usd_mtd_list_price"] == 12.0          # the old read still counts


def test_no_cursor_cache_starts_one_and_says_so(cursor, spawned):
    st = ab.status(for_gate=True)
    assert spawned == ["cursor"]
    assert "Cursor usage was not read" in st["summary"]
    assert "refreshing in the background" in st["summary"]


def test_a_fresh_cursor_cache_starts_none(cursor, spawned):
    _cursor_cache(60)
    st = ab.status(for_gate=True)
    assert spawned == []
    assert "Cursor" not in st["summary"]


def test_no_key_starts_none(spawned, monkeypatch):
    ab.set_budget(spend_cap=1000)
    ab.status(for_gate=True)
    assert spawned == []


def test_outside_the_guard_the_read_is_synchronous_as_before(cursor, spawned, monkeypatch):
    now = time.time()
    monkeypatch.setattr(harness_usage, "_cursor_post",
                        lambda body: {"usageEvents": [_event(now)]})
    st = ab.status()
    assert spawned == [] and st["est_usd_mtd_list_price"] == 12.0


def test_the_kill_switch_starts_none(cursor, spawned, monkeypatch):
    _cursor_cache(2 * harness_usage._CURSOR_TTL)
    for off in ("0", "false", "off"):
        monkeypatch.setenv("FINOPS_GUARD_BACKGROUND_REFRESH", off)
        st = ab.status(for_gate=True)
        assert "refreshing" not in st["summary"]
        assert "Admin API read 2 hours old." in st["summary"]
    assert spawned == []


def test_it_is_off_under_pytest_unless_a_test_turns_it_on(monkeypatch):
    monkeypatch.delenv("FINOPS_GUARD_BACKGROUND_REFRESH")
    assert os.getenv("PYTEST_CURRENT_TEST") and bg.enabled() is False
    monkeypatch.setenv("FINOPS_GUARD_BACKGROUND_REFRESH", "1")
    assert bg.enabled() is True
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    monkeypatch.delenv("FINOPS_GUARD_BACKGROUND_REFRESH")
    assert bg.enabled() is True                   # on by default outside a test run


def test_a_failed_read_inside_its_backoff_starts_none(cursor, spawned):
    now = time.time()
    _cursor_cache(2 * harness_usage._CURSOR_TTL, failed_at=now - 60, error="HTTPStatusError: 401")
    st = ab.status(for_gate=True)
    assert spawned == []
    assert harness_usage.cursor_status()["retry_at"] == pytest.approx(
        now - 60 + harness_usage._CURSOR_RETRY_AFTER, abs=5)
    assert "refreshing" not in st["summary"]
    # Once the backoff has passed, the next call tries again.
    _cursor_cache(2 * harness_usage._CURSOR_TTL,
                  failed_at=now - harness_usage._CURSOR_RETRY_AFTER - 1, error="x")
    ab.status(for_gate=True)
    assert spawned == ["cursor"]


# ── single flight ─────────────────────────────────────────────────────────────

def test_parallel_hooks_start_one_refresh(cursor, spawned):
    _cursor_cache(2 * harness_usage._CURSOR_TTL)
    first = ab.status(for_gate=True)
    second = ab.status(for_gate=True)            # another hook, the child still running
    assert spawned == ["cursor"]
    assert "refreshing in the background" in first["summary"]
    assert "refreshing in the background" in second["summary"]
    assert bg.maybe_start(bg.CURSOR) == bg.RUNNING


def test_a_dead_refreshs_lock_is_taken_over(cursor, spawned):
    lock = bg._lock_path(bg.CURSOR)
    lock.write_text("{}")
    assert bg.maybe_start(bg.CURSOR) == bg.RUNNING and spawned == []
    old = time.time() - bg.LOCK_STALE_AFTER - 5
    os.utime(lock, (old, old))
    assert bg.maybe_start(bg.CURSOR) == bg.STARTED and spawned == ["cursor"]


def test_a_spawn_that_fails_leaves_no_lock(cursor, monkeypatch):
    def broken(job, cwd):
        raise OSError("no such interpreter")

    monkeypatch.setattr(bg, "_spawn", broken)
    assert bg.maybe_start(bg.CURSOR) is None
    assert not bg._lock_path(bg.CURSOR).exists()


def test_the_gate_does_not_wait_for_the_refresh(cursor, monkeypatch):
    """The spawn is stubbed with one that would take seconds if it were waited on."""
    _cursor_cache(2 * harness_usage._CURSOR_TTL)
    started = []
    monkeypatch.setattr(bg, "_spawn", lambda job, cwd: started.append(time.perf_counter()))
    t = time.perf_counter()
    for _ in range(5):
        ab.status(for_gate=True)
    per_call = (time.perf_counter() - t) / 5
    assert len(started) == 1
    assert per_call < 0.25, f"the gate took {per_call * 1000:.0f} ms a call"


def test_the_guard_stop_says_the_cursor_figure_is_old(cursor, spawned):
    ab.set_budget(session_cap=10, session_id="conv-1")
    _cursor_cache(3 * harness_usage._CURSOR_TTL)
    v = g.check_budget_gate("conv-1")
    assert v["decision"] == "ask"
    assert "~$12.00 estimated this session" in v["reason"]
    assert "Admin API read 3 hours old; refreshing in the background" in v["reason"]
    assert spawned == ["cursor"]


# ── the child ─────────────────────────────────────────────────────────────────

def test_the_child_reads_once_and_writes_the_cache_atomically(cursor, monkeypatch):
    now = time.time()
    _cursor_cache(2 * harness_usage._CURSOR_TTL)
    bodies, writes = [], []
    monkeypatch.setattr(harness_usage, "_cursor_post",
                        lambda body: bodies.append(body) or {"usageEvents": [_event(now, 500.0)]})
    real_write = harness_usage.write_json_atomic
    monkeypatch.setattr(harness_usage, "write_json_atomic",
                        lambda path, data: writes.append(path) or real_write(path, data))
    bg._lock_path(bg.CURSOR).write_text("{}")      # as maybe_start leaves it for the child
    assert bg.main(["cursor"]) == 0
    assert len(bodies) == 1 and writes == [harness_usage._cursor_cache_path()]
    month = ab._month_start_epoch()
    assert bodies[0]["startDate"] == int((month - 86400) * 1000)
    cache = json.loads(harness_usage._cursor_cache_path().read_text())
    assert cache["fetched_at"] >= now and cache["events"][0]["usd"] == 5.0
    assert not bg._lock_path(bg.CURSOR).exists()
    # What the guard reads next is that read, fresh.
    assert ab.status(for_gate=True)["est_usd_mtd_list_price"] == 5.0


def test_a_failed_child_read_is_backed_off_and_released(cursor, spawned, monkeypatch):
    def down(body):
        raise OSError("unreachable")

    monkeypatch.setattr(harness_usage, "_cursor_post", down)
    bg._lock_path(bg.CURSOR).write_text("{}")
    assert bg.run(bg.CURSOR) == 0
    cache = json.loads(harness_usage._cursor_cache_path().read_text())
    assert "unreachable" in cache["error"] and cache["failed_at"] <= time.time()
    assert not bg._lock_path(bg.CURSOR).exists()
    st = ab.status(for_gate=True)
    assert spawned == [] and "unreachable" in st["summary"]


def test_an_unknown_job_is_refused():
    assert bg.main([]) == 2 and bg.main(["everything"]) == 2


def test_the_child_process_runs_and_exits_without_the_network(tmp_path):
    """The real entry point, in its own process, against a closed local port:
    it records the failure and exits 0, quickly."""
    data = tmp_path / "child-data"
    env = {k: v for k, v in os.environ.items()
           if not k.lower().endswith("_proxy") and not k.startswith(("AWS_", "PYTEST_"))}
    env.update(FINOPS_DATA_DIR=str(data), HOME=str(tmp_path),
               PYTHONPATH=os.pathsep.join([str(SRC), env.get("PYTHONPATH", "")]),
               CURSOR_ADMIN_API_KEY="key_test",  # pragma: allowlist secret
               FINOPS_CURSOR_API_URL="http://127.0.0.1:9/teams/filtered-usage-events")
    data.mkdir()
    (data / ".refresh-cursor.lock").write_text("{}")
    t = time.perf_counter()
    done = subprocess.run([sys.executable, "-m", "finops.background_refresh", "cursor"],
                          env=env, cwd=str(data), capture_output=True, text=True, timeout=60,
                          check=False)
    assert done.returncode == 0, done.stderr
    assert time.perf_counter() - t < 30
    cache = json.loads((data / "cursor-usage.json").read_text())
    assert cache["error"] and cache["failed_at"]
    assert not (data / ".refresh-cursor.lock").exists()


def test_the_admin_api_address_may_only_move_to_https_or_this_machine(monkeypatch):
    assert harness_usage._cursor_url() == harness_usage.CURSOR_API
    for ok in ("https://cursor-proxy.example.com/x", "http://127.0.0.1:9/x",
               "http://localhost:8080/x", "http://[::1]:9/x"):
        monkeypatch.setenv("FINOPS_CURSOR_API_URL", ok)
        assert harness_usage._cursor_url() == ok
    for bad in ("http://example.com/x", "ftp://127.0.0.1/x", "http://127.0.0.1.example.com/x"):
        monkeypatch.setenv("FINOPS_CURSOR_API_URL", bad)
        with pytest.raises(ValueError):
            harness_usage._cursor_url()


# ── the doctor ────────────────────────────────────────────────────────────────

def test_the_doctor_reports_the_cursor_read_and_refreshes_an_old_one(cursor, spawned):
    _cursor_cache(3 * harness_usage._CURSOR_TTL)
    r = bg.status(start=True)
    assert spawned == ["cursor"]
    assert r["enabled"] is True and r["cursor"]["stale"] is True
    assert r["cursor"]["age_hours"] == pytest.approx(3.0, abs=0.05)
    assert r["cursor"]["refreshing"] is True
    assert r["budget"] == {"auto": False, "requested": False, "refreshing": False}


def test_the_doctor_says_when_background_refresh_is_off(cursor, spawned, monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD_BACKGROUND_REFRESH", "0")
    _cursor_cache(60)
    r = bg.status(start=True)
    assert r["enabled"] is False and r["switched_off"] is True
    assert r["cursor"]["stale"] is False and r["cursor"]["refreshing"] is False
    assert spawned == []


def test_the_doctor_lists_the_cursor_gap(cursor, spawned, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(g, "_settings_path", lambda global_scope: tmp_path / f"s{global_scope}.json")
    _cursor_cache(3 * harness_usage._CURSOR_TTL)
    d = g.doctor()
    assert d["background_refresh"]["cursor"]["refreshing"] is True
    assert any("Admin API read 3 hours old" in n and "refresh is running" in n
               for n in d["not_covered"])
    _cursor_cache(60)
    d = g.doctor()
    assert any(c.startswith("Cursor usage in the AI budget (Admin API read")
               for c in d["covered"])


def test_the_cli_prints_the_background_refresh_section(cursor, spawned, tmp_path, monkeypatch):
    import argparse
    import contextlib
    import io

    from finops import setup_wizard
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(g, "_settings_path", lambda global_scope: tmp_path / f"s{global_scope}.json")
    _cursor_cache(60)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        setup_wizard._run_guard(argparse.Namespace(guard_action="doctor", guard_global=False,
                                                   guard_json=False))
    flat = " ".join(out.getvalue().split())
    assert "Background refresh on" in flat
    assert "Cursor usage: read 1 minute ago" in flat
    assert "Cloud spend figure: refreshed by `nable budget refresh` only" in flat


# ── the cloud budget summary (opt-in) ─────────────────────────────────────────

M5_2XL = "aws ec2 run-instances --instance-type m5.2xlarge"
P4D = "aws ec2 run-instances --instance-type p4d.24xlarge --count 8"


def _budget(spent=100.0, limit=1_000_000.0):
    today = datetime.now().astimezone().date()
    start = today.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return {"name": "Cloud total", "scope_type": "total", "scope_value": "*",
            "period": "monthly", "period_start": start.isoformat(),
            "period_end": end.isoformat(), "spent": spent, "limit": limit,
            "pct_used": 0.0, "status": "ok"}


def _summary(*budgets, age_hours=72.0, through=None):
    through = through or datetime.now().astimezone().date().replace(day=1).isoformat()
    bs.write_summary(list(budgets), spend_through=through,
                     now=datetime.now(UTC) - timedelta(hours=age_hours))


@pytest.fixture
def budget_lens_only(monkeypatch):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD", "FINOPS_POLICY_FILE",
                "FINOPS_GUARD_BUDGET_MAX_AGE_HOURS", "FINOPS_POLICY_ON_BUDGET_BREACH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ab, "status", lambda **_: {"verdict": ab.BUDGET_OK})


def test_the_budget_refresh_is_off_by_default(budget_lens_only, spawned):
    _summary(_budget())
    v = g.gate_command(P4D)
    assert "3 days old" in v["reason"] and "`nable budget refresh` updates it" in v["reason"]
    assert spawned == []


def test_a_stale_summary_is_refreshed_in_the_background_when_asked(
        budget_lens_only, spawned, monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD_AUTO_REFRESH_BUDGET", "1")
    _summary(_budget())
    v = g.gate_command(P4D)
    assert "3 days old" in v["reason"]
    assert "it is being refreshed in the background" in v["reason"]
    g.gate_command(P4D)                              # the child is still running
    assert spawned == ["budget"]


def test_a_missing_summary_is_computed_in_the_background_when_asked(
        budget_lens_only, spawned, monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD_AUTO_REFRESH_BUDGET", "1")
    v = g.gate_command(P4D)
    assert "one is being computed in the background" in v["reason"]
    assert spawned == ["budget"]


def test_no_budget_means_no_budget_refresh(budget_lens_only, spawned, monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD_AUTO_REFRESH_BUDGET", "1")
    _summary()
    g.gate_command(P4D)
    assert spawned == []


def test_the_budget_refresh_obeys_the_kill_switch_and_its_backoff(
        budget_lens_only, spawned, monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD_AUTO_REFRESH_BUDGET", "1")
    monkeypatch.setenv("FINOPS_GUARD_BACKGROUND_REFRESH", "0")
    _summary(_budget())
    g.gate_command(P4D)
    assert spawned == []
    monkeypatch.setenv("FINOPS_GUARD_BACKGROUND_REFRESH", "1")
    harness_usage.write_json_atomic(bg._state_path(bg.BUDGET),
                                    {"error": "x", "at": time.time(),
                                     "retry_at": time.time() + 60})
    g.gate_command(P4D)
    assert spawned == []


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Engine:
    def connect(self):
        return _Conn()


@pytest.fixture
def budget_child(monkeypatch):
    from finops.budget import enforcer
    from finops.storage import db
    refreshed = []
    monkeypatch.setattr(db, "get_engine", lambda *a, **k: _Engine())
    monkeypatch.setattr(enforcer, "refresh_summary",
                        lambda: refreshed.append(1) or {"ok": True, "budgets": 1})
    return refreshed


def test_the_budget_child_does_not_restamp_old_spend(budget_child, monkeypatch):
    from finops.budget import enforcer
    through = datetime.now().astimezone().date().replace(day=1).isoformat()
    _summary(_budget(), through=through)
    monkeypatch.setattr(enforcer, "_spend_through", lambda conn, since=None: through)
    bg._lock_path(bg.BUDGET).write_text("{}")
    assert bg.run(bg.BUDGET) == 0
    assert budget_child == []
    state = bg.read_state(bg.BUDGET)
    assert f"no cost data newer than {through}" in state["note"]
    assert state["retry_at"] == pytest.approx(time.time() + bg.NOTHING_NEW_WAIT, abs=5)
    assert not bg._lock_path(bg.BUDGET).exists()
    assert bs.freshness(bs.read_summary())["state"] == "stale"


def test_the_budget_child_refreshes_when_there_is_newer_spend(budget_child, monkeypatch):
    from finops.billing_access import in_unattended_context
    from finops.budget import enforcer
    _summary(_budget(), through="2020-01-01")
    unattended = []
    monkeypatch.setattr(enforcer, "_spend_through",
                        lambda conn, since=None: unattended.append(in_unattended_context())
                        or "2099-01-01")
    assert bg.run(bg.BUDGET) == 0
    assert budget_child == [1] and unattended == [True]
    assert bg.read_state(bg.BUDGET) == {}


def test_a_failed_budget_child_is_backed_off(budget_child, monkeypatch):
    from finops.budget import enforcer
    monkeypatch.setattr(enforcer, "refresh_summary",
                        lambda: {"ok": False, "error": "database is locked"})
    assert bg.run(bg.BUDGET) == 0                       # no summary: straight to refresh
    state = bg.read_state(bg.BUDGET)
    assert state["error"] == "database is locked"
    assert state["retry_at"] == pytest.approx(time.time() + bg.RETRY_AFTER, abs=5)
