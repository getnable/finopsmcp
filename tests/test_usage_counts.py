# SPDX-License-Identifier: Apache-2.0
"""Two usage counts, under the consent telemetry already asks for.

What has to stay true (D57):
  - there are exactly two events, commitments_status_run and
    guard_commitment_asks, and each carries a closed allow-list of integers,
    booleans and enumerations, plus the runtime facts every event carries;
  - nothing a person or an account is named by reaches one: no account id,
    ARN, dollar figure, command, path or resource name, whatever the inputs
    held;
  - they are sent only under the consent that exists (a stored yes, or
    NABLE_TELEMETRY=1), never with NABLE_NO_TELEMETRY, in CI, in air-gap mode
    or without consent, and there is no new prompt;
  - asks are counted from the local guard ledger at most once a day, from a
    CLI or MCP start, never from inside the guard hook, which imports no
    telemetry at all.

Every send here is caught at telemetry._post_event, the last step before the
network, so what a test reads is the whole body that would have left.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from finops import cli_commitments as cli
from finops import guard_ledger
from tests.test_commitments_status import ACCOUNT, NOW, FakeSession, _full_account, _run

SRC = str(Path(__file__).resolve().parents[1] / "src")

# What a test plants in the inputs, and must never find in a body.
PLANTED = (ACCOUNT, "210987654321", "arn:aws:rds:us-east-1", "1234.56", "$1,234",
           "purchase-reserved-db-instances-offering", "/home/alice", "prod-orders-db",
           "ri-db-1", "sp-2")


def _tel():
    """finops.telemetry as it is now: test_airgap re-imports it."""
    return importlib.import_module("finops.telemetry")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Consent, install id and the count's own state in this test's
    directory; every opt-in and opt-out signal cleared."""
    tel = _tel()
    monkeypatch.setattr(tel, "_CONSENT_FILE", tmp_path / ".telemetry")
    monkeypatch.setattr(tel, "_ID_FILE", tmp_path / ".install_id")
    monkeypatch.setattr(tel, "_COUNTS_FILE", tmp_path / ".usage_counts")
    monkeypatch.setattr(tel, "_COUNTS_STARTED", False)
    for var in ("NABLE_TELEMETRY", "NABLE_NO_TELEMETRY", "DO_NOT_TRACK", "FINOPS_AIRGAP",
                *tel._CI_ENV_VARS):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(tel, "_POSTHOG_KEY", "phc_test_not_a_key")  # pragma: allowlist secret
    yield


@pytest.fixture
def sent(monkeypatch):
    """Every body that would have been POSTed."""
    bodies: list[dict] = []
    monkeypatch.setattr(_tel(), "_post_event", lambda body: bodies.append(body))
    return bodies


@pytest.fixture
def forbidden(monkeypatch):
    """A send fails the test."""
    def boom(*_a, **_k):
        raise AssertionError("telemetry was sent without consent")
    tel = _tel()
    monkeypatch.setattr(tel, "_post_event", boom)
    monkeypatch.setattr(tel, "_send_event", boom)


def _consent(monkeypatch, how: str = "stored"):
    tel = _tel()
    if how == "stored":
        tel._CONSENT_FILE.write_text("yes")
    else:
        monkeypatch.setenv("NABLE_TELEMETRY", "1")


def _events(bodies: list[dict], name: str) -> list[dict]:
    return [b for b in bodies if b["event"] == name]


# ── a ledger written the way append() writes one, at fixture times ──────────

def _write_ledger(path: Path, records: list[dict]) -> list[bytes]:
    prev = guard_ledger.GENESIS
    lines = []
    for rec in records:
        line = json.dumps({"v": 1, **rec, "prev": prev}, separators=(",", ":"),
                          sort_keys=True).encode()
        prev = hashlib.sha256(line).hexdigest()
        lines.append(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(x + b"\n" for x in lines))
    return lines


def _append(path: Path, records: list[dict]) -> None:
    raw = path.read_bytes()
    prev = hashlib.sha256(raw.rstrip(b"\n").rsplit(b"\n", 1)[-1]).hexdigest()
    with path.open("ab") as fh:
        for rec in records:
            line = json.dumps({"v": 1, **rec, "prev": prev}, separators=(",", ":"),
                              sort_keys=True).encode()
            prev = hashlib.sha256(line).hexdigest()
            fh.write(line + b"\n")


def _ask(ts: datetime, action="purchase_commitment", decision="ask", **extra) -> dict:
    return {"ts": ts.isoformat(timespec="seconds"), "decision": decision,
            "action_type": action, "harness": "claude-code", "session": "s-1",
            "tool": "Bash",
            "command": ("aws rds purchase-reserved-db-instances-offering "
                        "--reserved-db-instance-id prod-orders-db --account 123456789012 "
                        "--profile /home/alice/.aws"),
            "monthly_usd": 1234.56, "reason": "would buy a commitment for $1,234 a month "
                                              "in arn:aws:rds:us-east-1:210987654321",
            **extra}


# ── C1: what each event carries ──────────────────────────────────────────────

def test_there_are_exactly_two_events_each_a_closed_allow_list():
    tel = _tel()
    assert set(tel.USAGE_EVENTS) == {"commitments_status_run", "guard_commitment_asks"}
    assert set(tel.USAGE_EVENTS["commitments_status_run"]) == {
        "exit_class", "commitments_read", "ending_within_90_days", "services_not_read", "json"}
    assert set(tel.USAGE_EVENTS["guard_commitment_asks"]) == {"asks", "window_days"}
    for event, props in tel.USAGE_EVENTS.items():
        for name, kind in props.items():
            assert kind in (int, bool) or (isinstance(kind, tuple) and kind), (event, name)


def test_a_key_off_the_list_or_of_the_wrong_kind_never_leaves():
    tel = _tel()
    got = tel.usage_props("commitments_status_run", {
        "exit_class": 1, "commitments_read": 8, "ending_within_90_days": 4,
        "services_not_read": 0, "json": True,
        "account": ACCOUNT, "arn": "arn:aws:rds:us-east-1:123456789012:ri:x",
        "usd": 1234.56, "command": "aws rds purchase-reserved-db-instances-offering",
        "path": "/home/alice/.finops", "resource": "prod-orders-db"})
    assert got == {"exit_class": 1, "commitments_read": 8, "ending_within_90_days": 4,
                   "services_not_read": 0, "json": True}
    # A string, a float, a bool where a count goes, an enumeration off its list.
    odd = tel.usage_props("commitments_status_run", {
        "exit_class": 7, "commitments_read": "8", "ending_within_90_days": 4.5,
        "services_not_read": True, "json": "yes"})
    assert odd == {}
    assert tel.usage_props("not_an_event", {"asks": 1}) is None


def test_a_count_is_clamped_so_an_account_id_cannot_ride_in_one():
    tel = _tel()
    got = tel.usage_props("guard_commitment_asks", {"asks": int(ACCOUNT), "window_days": -3})
    assert got["asks"] == tel.COUNT_MAX and ACCOUNT not in json.dumps(got)
    assert got["window_days"] == 0


def test_a_status_run_sends_counts_and_nothing_it_read(monkeypatch, sent):
    _consent(monkeypatch)
    with FakeSession() as sess:
        _full_account(sess)
        code, out, _err = _run(sess, "--json")
    assert code == cli.EXIT_OK
    for planted in ("ri-db-1", "sp-2", ACCOUNT):
        assert planted in out, "the fixture lost what it plants"
    (body,) = _events(sent, "commitments_status_run")
    props = body["properties"]
    assert {k: props[k] for k in _tel().USAGE_EVENTS["commitments_status_run"]} == {
        "exit_class": 0, "commitments_read": 8, "ending_within_90_days": 4,
        "services_not_read": 0, "json": True}
    text = json.dumps(body)
    for planted in (*PLANTED, "1000.0", "FixedPrice", "Partial Upfront", "us-east-1"):
        assert planted not in text, planted
    # Only the allow-list and the runtime facts every event carries.
    runtime = set(_tel()._runtime_props()) | {"$ip"}
    assert set(props) <= set(_tel().USAGE_EVENTS["commitments_status_run"]) | runtime


@pytest.mark.parametrize("code, klass", [
    (cli.EXIT_OK, 0), (cli.EXIT_ENDING, 1), (cli.EXIT_CANNOT_CHECK, 2),
    (cli.EXIT_EXPIRED, 2), (cli.EXIT_DENIED, 2), (cli.EXIT_PARTIAL, 2), (cli.EXIT_NO_CREDS, 2)])
def test_the_exit_class(code, klass):
    assert cli.exit_class(code) == klass


def test_a_run_that_read_nothing_says_every_service_was_not_read(monkeypatch, sent):
    _consent(monkeypatch, "env")
    with FakeSession(creds=False) as sess:
        code, _out, _err = _run(sess, "--fail-within", "30")
    assert code == cli.EXIT_CANNOT_CHECK
    (body,) = _events(sent, "commitments_status_run")
    p = body["properties"]
    assert (p["exit_class"], p["commitments_read"], p["services_not_read"], p["json"]) == (
        2, 0, 7, False)


def test_a_dry_run_or_a_usage_error_is_not_a_run(monkeypatch, sent):
    _consent(monkeypatch)
    with FakeSession() as sess:
        assert _run(sess, "--dry-run")[0] == 0
        assert _run(sess, "--regions", "not a region")[0] == 2
    assert _events(sent, "commitments_status_run") == []


def test_the_asks_event_carries_a_count_and_a_window_and_nothing_else(monkeypatch, sent):
    _consent(monkeypatch)
    _write_ledger(guard_ledger.ledger_path(), [
        _ask(NOW - timedelta(days=6)),
        _ask(NOW - timedelta(days=5), decision="deny"),
        _ask(NOW - timedelta(days=4), action="one_way"),
        {"ts": (NOW - timedelta(days=3)).isoformat(), "kind": "outcome", "outcome": "ran",
         "decision": "ask", "action_type": "purchase_commitment", "harness": "claude-code"},
        _ask(NOW - timedelta(days=2)),
    ])
    assert _tel().count_guard_asks(now=NOW) is True
    (body,) = _events(sent, "guard_commitment_asks")
    p = body["properties"]
    assert (p["asks"], p["window_days"]) == (2, 6)
    text = json.dumps(body)
    for planted in PLANTED:
        assert planted not in text, planted


# ── C2: when they send ───────────────────────────────────────────────────────

@pytest.mark.parametrize("how", ["stored", "env"])
def test_each_existing_consent_sends(monkeypatch, sent, how):
    _consent(monkeypatch, how)
    with FakeSession(creds=False) as sess:
        _run(sess)
    assert len(_events(sent, "commitments_status_run")) == 1


@pytest.mark.parametrize("setup", ["none", "stored_no", "no_telemetry", "ci", "airgap",
                                   "do_not_track", "env_no"])
def test_nothing_sends_without_consent(monkeypatch, forbidden, setup):
    tel = _tel()
    if setup == "stored_no":
        tel._CONSENT_FILE.write_text("no")
    elif setup == "no_telemetry":
        _consent(monkeypatch)
        monkeypatch.setenv("NABLE_TELEMETRY", "1")
        monkeypatch.setenv("NABLE_NO_TELEMETRY", "1")
    elif setup == "ci":
        _consent(monkeypatch)
        monkeypatch.setenv("CI", "1")
    elif setup == "airgap":
        _consent(monkeypatch, "env")
        monkeypatch.setenv("FINOPS_AIRGAP", "1")
    elif setup == "do_not_track":
        _consent(monkeypatch)
        monkeypatch.setenv("DO_NOT_TRACK", "1")
    elif setup == "env_no":
        _consent(monkeypatch)
        monkeypatch.setenv("NABLE_TELEMETRY", "0")
    _write_ledger(guard_ledger.ledger_path(), [_ask(NOW - timedelta(days=1))])
    with FakeSession(creds=False) as sess:
        _run(sess)
    with FakeSession() as sess:
        _full_account(sess)
        _run(sess, "--json")
    assert tel.count_guard_asks(now=NOW) is False
    assert tel.send_usage("guard_commitment_asks", {"asks": 1, "window_days": 1}) is False
    assert not tel._COUNTS_FILE.exists(), "the count kept state for a person who said no"


def test_without_consent_the_ledger_is_not_even_read(monkeypatch, forbidden):
    def boom(*_a, **_k):
        raise AssertionError("the ledger was read for telemetry without consent")
    monkeypatch.setattr(guard_ledger, "count_asks", boom)
    _write_ledger(guard_ledger.ledger_path(), [_ask(NOW)])
    assert _tel().count_guard_asks(now=NOW) is False


def test_there_is_no_new_prompt(monkeypatch):
    """The only question is the one consent prompt that already exists."""
    import inspect
    tel = _tel()
    for fn in (tel.count_guard_asks, tel.send_usage, tel.start_usage_counts,
               cli.run):
        assert "input(" not in inspect.getsource(fn), fn.__name__


def test_asks_are_counted_at_most_once_a_day_and_only_the_new_ones(monkeypatch, sent):
    _consent(monkeypatch)
    path = guard_ledger.ledger_path()
    _write_ledger(path, [_ask(NOW - timedelta(days=3)), _ask(NOW - timedelta(hours=2))])
    assert _tel().count_guard_asks(now=NOW) is True
    assert _tel().count_guard_asks(now=NOW + timedelta(hours=5)) is False, "twice in a day"
    _append(path, [_ask(NOW + timedelta(hours=6)), _ask(NOW + timedelta(hours=7),
                                                         decision="allow")])
    assert _tel().count_guard_asks(now=NOW + timedelta(days=1, hours=1)) is True
    first, second = (e["properties"] for e in _events(sent, "guard_commitment_asks"))
    assert (first["asks"], first["window_days"]) == (2, 3)
    assert (second["asks"], second["window_days"]) == (1, 2)
    # A day with no new asks still says so: zero is a count that was read.
    assert _tel().count_guard_asks(now=NOW + timedelta(days=2, hours=1)) is True
    assert _events(sent, "guard_commitment_asks")[-1]["properties"]["asks"] == 0


def test_a_rewritten_ledger_is_counted_by_time_not_by_position(monkeypatch, sent):
    _consent(monkeypatch)
    path = guard_ledger.ledger_path()
    _write_ledger(path, [_ask(NOW - timedelta(days=2)), _ask(NOW - timedelta(days=1))])
    assert _tel().count_guard_asks(now=NOW)
    # The file is replaced: the line the last count ended on is gone.
    _write_ledger(path, [_ask(NOW - timedelta(days=9)), _ask(NOW + timedelta(hours=3))])
    assert _tel().count_guard_asks(now=NOW + timedelta(days=1))
    assert _events(sent, "guard_commitment_asks")[-1]["properties"]["asks"] == 1


def test_no_ledger_or_an_unreadable_one_sends_nothing_never_a_zero(monkeypatch, sent):
    _consent(monkeypatch)
    assert _tel().count_guard_asks(now=NOW) is False
    path = guard_ledger.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(path.parent / "elsewhere.jsonl")
    assert _tel().count_guard_asks(now=NOW) is False
    assert _events(sent, "guard_commitment_asks") == []
    assert not _tel()._COUNTS_FILE.exists(), "a failed read was recorded as counted"


def test_the_counts_state_is_private(monkeypatch, sent):
    _consent(monkeypatch)
    _write_ledger(guard_ledger.ledger_path(), [_ask(NOW)])
    assert _tel().count_guard_asks(now=NOW)
    state = _tel()._COUNTS_FILE
    assert state.stat().st_mode & 0o077 == 0
    for planted in PLANTED:
        assert planted not in state.read_text(), planted


# ── where the asks are counted from ──────────────────────────────────────────

def test_the_cli_start_and_the_mcp_start_count_and_nothing_else_does(monkeypatch):
    import ast
    import inspect

    from finops import setup_wizard

    tel = _tel()
    fn = next(n for n in ast.walk(ast.parse(inspect.getsource(setup_wizard)))
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and "start_usage_counts" in (getattr(n.func, "id", None),
                                          getattr(n.func, "attr", None))]
    assert len(calls) == 1, "the CLI start does not count asks"
    assert "start_usage_counts" in inspect.getsource(tel.ping_startup), (
        "the MCP start does not count asks")
    # And nowhere on the hook's path: none of its modules imports telemetry.
    root = Path(SRC) / "finops"
    for name in ("guard.py", "guard_plugin.py", "guard_outcome.py", "guard_ledger.py",
                 "guard_paths.py", "guard_adapters.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                named = [node.module or ""] + [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                named = [a.name for a in node.names]
            else:
                continue
            assert not any(n == "telemetry" or n.endswith(".telemetry") for n in named), name


def test_start_usage_counts_runs_in_the_background_and_only_with_consent(monkeypatch):
    tel = _tel()
    started: list = []
    monkeypatch.setattr(tel, "in_background", lambda fn, *a, **k: started.append(fn))
    tel.start_usage_counts()
    assert started == [], "a thread was started for a person who never agreed"
    _consent(monkeypatch)
    tel.start_usage_counts()
    tel.start_usage_counts()
    assert started == [tel.count_guard_asks], "counted twice from one process"


def test_two_processes_at_once_count_once(monkeypatch, sent):
    """A start while another process holds the count's lock counts nothing."""
    import fcntl
    import os

    _consent(monkeypatch)
    _write_ledger(guard_ledger.ledger_path(), [_ask(NOW)])
    tel = _tel()
    tel._COUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = tel._COUNTS_FILE.with_name(tel._COUNTS_FILE.name + ".lock")
    other = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert tel.count_guard_asks(now=NOW) is False
    finally:
        os.close(other)
    assert tel.count_guard_asks(now=NOW) is True
    assert len(_events(sent, "guard_commitment_asks")) == 1


_PROBE = ("import sys, json;"
          "sys.argv = ['finops', 'guard', 'hook'] + sys.argv[1:];"
          "from finops.setup_wizard import main\n"
          "try:\n    main()\nexcept SystemExit as e:\n    assert e.code == 0\n"
          "sys.stderr.write(json.dumps(sorted(m for m in sys.modules "
          "if m.startswith('finops.'))))")


@pytest.mark.parametrize("via", [["--via", "plugin"], []])
def test_the_guard_hook_imports_no_telemetry(tmp_path, via):
    """Even consented, with a commitment purchase to judge: the hook never
    loads the module that sends, so it cannot count or send from inside."""
    import os
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                          "session_id": "s-1", "cwd": str(tmp_path),
                          "tool_input": {"command": "aws rds purchase-reserved-db-instances-"
                                                    "offering --reserved-db-instances-"
                                                    "offering-id x"}})
    home = tmp_path / "home"
    (home / ".config" / "finops").mkdir(parents=True)
    (home / ".config" / "finops" / ".telemetry").write_text("yes")
    env = {k: v for k, v in os.environ.items()
           if k not in ("CI", "NABLE_NO_TELEMETRY", "FINOPS_ORG_DIR", "FINOPS_PROFILE")}
    env.update(HOME=str(home), FINOPS_DATA_DIR=str(tmp_path / "data"), PYTHONPATH=SRC,
               NABLE_TELEMETRY="1", NABLE_POSTHOG_KEY="")
    r = subprocess.run([sys.executable, "-c", _PROBE, *via], input=payload,
                       capture_output=True, text=True, env=env, timeout=60, check=False,
                       cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    modules = json.loads(r.stderr.strip().splitlines()[-1])
    assert "finops.telemetry" not in modules, modules
    assert '"ask"' in r.stdout, "the hook did not judge the purchase"


# ── C2: the docstring and the docs say exactly this ──────────────────────────

def test_telemetry_docstring_and_security_md_say_what_the_counts_are():
    doc = " ".join((_tel().__doc__ or "").split())
    root = Path(__file__).resolve().parents[1]
    sec = " ".join((root / "SECURITY.md").read_text(encoding="utf-8").split())
    for text in (doc, sec):
        for phrase in ("commitments_status_run", "guard_commitment_asks",
                       "at most once a day", "never from inside the guard hook",
                       "no new prompt"):
            assert phrase in text, phrase
        for never in ("account id", "dollar", "command", "resource name"):
            assert never in text.lower(), never
