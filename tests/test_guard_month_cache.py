# SPDX-License-Identifier: Apache-2.0
"""The guard's month to date, priced once (D109, open item 11).

The org model's AI budgets are checked at every shell and MCP call against
this machine's month to date at list price. The month cache keeps each
transcript's responses priced, keyed by its path, size, mtime and inode, the
UTC month and the llm_prices table, 0600 in the data dir:

  - a second call over unchanged transcripts prices no response;
  - appending N responses to one transcript prices exactly N;
  - a new UTC month, or another price table, prices everything again;
  - a transcript changed other than by an append is read again;
  - the figure is the one pricing every response gives, a response two
    transcripts hold counted once;
  - the cache is one of the guard's own files.

The pricing function is spied on (ai_budget.list_price_usd), so "prices no
response" is a count, not a timing.
"""
from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal

import pytest

from finops import ai_budget as ab
from finops import guard, llm_prices
from tests.test_guard_org_budget import AT, MONTH_START, _budget, _person, _transcript
from tests.test_guard_org_budget import box as _box  # noqa: F401 - a fixture


@pytest.fixture
def priced(monkeypatch):
    """How many responses ai_budget.list_price_usd priced since the last reset."""
    calls = {"n": 0}
    real = ab.list_price_usd

    def counting(r):
        calls["n"] += 1
        return real(r)
    monkeypatch.setattr(ab, "list_price_usd", counting)
    return calls


@pytest.fixture
def heavy(_box, monkeypatch):  # noqa: F811
    """Three transcripts this month, 40 responses between them, and a budget
    the gate reads them for."""
    for s in ("s-a", "s-b"):
        _transcript(_box["claude"], s, n=15)
    _budget(_box, "person", "alice@acme.example", "{monthly_usd: 100000}")
    _person(monkeypatch)
    return _box


def _uncached() -> str:
    """The month to date, every response priced, no cache: the old way."""
    rs, _notes = ab.agent_responses(ab._month_start_epoch())
    usd = sum((ab.list_price_usd(r) or Decimal(0) for r in rs), Decimal(0))
    return format(usd.quantize(Decimal("0.01"), rounding=ROUND_CEILING), "f")


def _append(claude, session: str, n: int, *, tag: str = "x") -> None:
    f = claude / "projects" / "-home-alice-work" / f"{session}.jsonl"
    with f.open("a") as fh:
        for i in range(n):
            fh.write(json.dumps({
                "timestamp": AT.replace(hour=13, minute=i % 60).isoformat().replace(
                    "+00:00", "Z"),
                "sessionId": session, "requestId": f"req-{tag}-{i}",
                "message": {"id": f"msg-{tag}-{i}", "model": "claude-sonnet-4-6",
                            "usage": {"input_tokens": 1_000, "output_tokens": 10,
                                      "cache_creation_input_tokens": 0,
                                      "cache_read_input_tokens": 0}}}) + "\n")


def test_a_second_gate_call_over_unchanged_transcripts_prices_nothing(heavy, priced):
    assert guard.gate_command("ls -la") is None
    assert priced["n"] == 31                  # s-1's one response, and s-a's and s-b's 15
    priced["n"] = 0
    assert guard.gate_command("ls -la") is None
    assert guard.gate_command("git status") is None
    assert priced["n"] == 0, "an unchanged month was priced again"
    assert ab.month_agent_usage()["usd_cents"] == _uncached()


@pytest.mark.parametrize("n", [1, 7])
def test_appending_n_responses_to_one_transcript_prices_exactly_n(heavy, priced, n):
    guard.gate_command("ls -la")
    before = ab.month_agent_usage()["usd_exact"]
    priced["n"] = 0
    _append(heavy["claude"], "s-a", n)
    guard.gate_command("ls -la")
    assert priced["n"] == n
    after = ab.month_agent_usage()
    each = llm_prices.price_for("claude-sonnet-4-6").cost_exact(input_tokens=1_000,
                                                                 output_tokens=10)
    assert Decimal(after["usd_exact"]) - Decimal(before) == each * n
    assert after["usd_cents"] == _uncached()


def test_a_line_still_being_written_is_read_when_it_ends(heavy, priced):
    guard.gate_command("ls -la")
    f = heavy["claude"] / "projects" / "-home-alice-work" / "s-a.jsonl"
    whole = json.dumps({"timestamp": AT.isoformat().replace("+00:00", "Z"), "sessionId": "s-a",
                        "requestId": "req-half", "message": {
                            "id": "msg-half", "model": "claude-sonnet-4-6",
                            "usage": {"input_tokens": 5, "output_tokens": 5}}})
    with f.open("a") as fh:
        fh.write(whole[:20])
    priced["n"] = 0
    guard.gate_command("ls -la")
    assert priced["n"] == 0
    with f.open("a") as fh:
        fh.write(whole[20:] + "\n")
    guard.gate_command("ls -la")
    assert priced["n"] == 1 and ab.month_agent_usage()["usd_cents"] == _uncached()


def test_a_new_utc_month_prices_everything_again(heavy, priced, monkeypatch):
    guard.gate_command("ls -la")
    priced["n"] = 0
    nxt = datetime(2026, 11, 1, tzinfo=UTC).timestamp()
    monkeypatch.setattr(ab, "_month_start_epoch", lambda: nxt)
    for f in (heavy["claude"] / "projects").rglob("*.jsonl"):
        os.utime(f, (nxt + 60, nxt + 60))      # touched in the new month, nothing in it
    u = ab.month_agent_usage()
    assert u["usd_cents"] == "0.00" and priced["n"] == 0, "October's responses are not November's"
    doc = json.loads((heavy["tmp"] / "data" / ab.MONTH_CACHE).read_text())
    assert doc["month"] == "2026-11" and doc["since"] == nxt
    monkeypatch.setattr(ab, "_month_start_epoch", lambda: MONTH_START)
    ab.month_agent_usage()
    assert priced["n"] == 31, "going back a month made the cache again"


def test_another_price_table_prices_everything_again(heavy, priced, monkeypatch):
    guard.gate_command("ls -la")
    before = ab.month_agent_usage()["usd_cents"]
    priced["n"] = 0
    old = llm_prices.MODEL_PRICES["claude-sonnet-4-6"]
    import dataclasses
    monkeypatch.setitem(llm_prices.MODEL_PRICES, "claude-sonnet-4-6",
                        dataclasses.replace(old, input=old.input * 2))
    after = ab.month_agent_usage()["usd_cents"]
    assert priced["n"] == 31
    assert Decimal(after) > Decimal(before) and after == _uncached()
    priced["n"] = 0
    ab.month_agent_usage()
    assert priced["n"] == 0


def test_a_rewritten_transcript_is_read_again(heavy, priced):
    guard.gate_command("ls -la")
    f = heavy["claude"] / "projects" / "-home-alice-work" / "s-b.jsonl"
    lines = f.read_text().splitlines(keepends=True)
    f.write_text("".join(lines[:5]))           # shorter: not an append
    priced["n"] = 0
    u = ab.month_agent_usage()
    assert priced["n"] == 5 and u["usd_cents"] == _uncached()


def test_a_response_two_transcripts_hold_counts_once(heavy, priced):
    """A resumed session copies the conversation it resumes."""
    src = heavy["claude"] / "projects" / "-home-alice-work" / "s-a.jsonl"
    copy = heavy["claude"] / "projects" / "-home-alice-work" / "s-resumed.jsonl"
    copy.write_text(src.read_text())
    os.utime(copy, (AT.timestamp(), AT.timestamp()))
    u = ab.month_agent_usage()
    assert u["usd_cents"] == _uncached()
    assert Decimal(u["usd_exact"]) == Decimal("4.5") * 31
    _append(heavy["claude"], "s-resumed", 2)
    assert ab.month_agent_usage()["usd_cents"] == _uncached()


def test_a_removed_transcript_leaves_the_month(heavy, priced):
    ab.month_agent_usage()
    (heavy["claude"] / "projects" / "-home-alice-work" / "s-b.jsonl").unlink()
    u = ab.month_agent_usage()
    assert u["usd_cents"] == _uncached() == format(Decimal("4.5") * 16, ".2f")
    shards = list((heavy["tmp"] / "data" / ab.MONTH_SHARDS).iterdir())
    assert len(shards) == 2


def test_the_cache_is_0600_in_the_sandboxed_data_dir(heavy):
    guard.gate_command("ls -la")
    data = heavy["tmp"] / "data"
    files = [data / ab.MONTH_CACHE, data / ab.MONTH_TOTAL,
             *(data / ab.MONTH_SHARDS).iterdir()]
    assert len(files) == 5
    for f in files:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f
    assert stat.S_IMODE((data / ab.MONTH_SHARDS).stat().st_mode) == 0o700
    home = os.path.expanduser("~")
    assert not str(data).startswith(os.path.join(home, ".nable"))


def test_a_cache_written_by_hand_is_not_trusted_past_its_stats(heavy, priced):
    """The total is used only while every transcript has the stats it was
    made from; a hand-edited total for other stats is made again."""
    ab.month_agent_usage()
    total = heavy["tmp"] / "data" / ab.MONTH_TOTAL
    doc = json.loads(total.read_text())
    doc["claude"]["usd"] = "0"
    doc["stamp"] = []
    total.write_text(json.dumps(doc))
    assert ab.month_agent_usage()["usd_cents"] == _uncached()


@pytest.mark.parametrize("name", [ab.MONTH_CACHE, ab.MONTH_TOTAL, f"{ab.MONTH_SHARDS}/x.json",
                                  "ai-budget-tally/x.json"])
def test_an_agent_writing_the_cache_is_asked(heavy, name):
    guard.gate_command("ls -la")
    path = heavy["tmp"] / "data" / name
    v = guard.gate_command(f"echo '{{}}' > {path}")
    assert v is not None and v["decision"] == "ask" and v["action_type"] == "protected_write"


def test_codex_responses_are_priced_once_each(heavy, priced, monkeypatch):
    from finops import harness_usage
    r = {"ts": AT.timestamp(), "model": "gpt-5", "input": 1_000_000, "output": 0,
         "cache_write": 0, "cache_write_1h": 0, "cache_read": 0, "fast": False,
         "us_only": False, "session": "c-1", "cwd": None, "harness": harness_usage.HARNESS_CODEX}
    monkeypatch.setattr(harness_usage, "codex_responses", lambda since, session_id=None: [r])
    u = ab.month_agent_usage()
    assert "codex" in u["agents"] and "openai" in u["providers"]
    priced["n"] = 0
    assert ab.month_agent_usage()["usd_cents"] == u["usd_cents"]
    assert priced["n"] == 0
    assert u["usd_cents"] == _uncached()


def test_an_unchanged_month_reads_the_small_total_alone(heavy, monkeypatch):
    """The call the guard makes most often: every transcript as the total was
    made from. It reads ai-budget-month.total.json and nothing else of the
    cache: no index, no transcript's shard, no transcript."""
    ab.month_agent_usage()
    read: list[str] = []
    real = ab._MonthTally._load

    def spy(self, path):
        read.append(path.name)
        return real(self, path)
    monkeypatch.setattr(ab._MonthTally, "_load", spy)
    opened: list[str] = []
    monkeypatch.setattr(ab._MonthTally, "_read_file",
                        lambda self, p, st, entry: opened.append(p))
    ab.month_agent_usage()
    assert read == [ab.MONTH_TOTAL] and opened == []


def test_a_grown_transcript_is_the_only_one_read_again(heavy, monkeypatch):
    """The call after an agent's turn: one transcript grew. The others are
    taken from the index by their stats, so neither their shards nor their
    files are read; only the grown one is, from where its last read stopped."""
    ab.month_agent_usage()
    _append(heavy["claude"], "s-a", 2)
    opened: list[str] = []
    real = ab._MonthTally._read_file

    def spy(self, p, st, entry):
        opened.append(os.path.basename(p))
        return real(self, p, st, entry)
    monkeypatch.setattr(ab._MonthTally, "_read_file", spy)
    u = ab.month_agent_usage()
    assert opened == ["s-a.jsonl"]
    assert u["usd_cents"] == _uncached()


def test_a_transcript_that_could_not_be_read_is_never_kept_as_zero(heavy, monkeypatch):
    """A read that fails (a permission error, a sharing violation while
    another process holds the file) is said as not read, nothing about the
    file is kept, and the next call reads it: a failed read then a good one
    gives the full figure, and never $0 for that transcript after."""
    # Transcripts are opened without waiting (guard_paths.open_regular, D171),
    # through os.open: the failure is made there.
    real_open = os.open
    fail = {"left": 1}

    def flaky(file, *a, **k):
        if str(file).endswith("s-b.jsonl") and fail["left"]:
            fail["left"] -= 1
            raise PermissionError(13, "Permission denied")
        return real_open(file, *a, **k)
    monkeypatch.setattr(os, "open", flaky)
    u = ab.month_agent_usage()
    full = Decimal(_uncached())
    assert Decimal(u["usd_exact"]) == full - Decimal("4.5") * 15
    assert any("1 Claude Code transcript was not read" in n for n in u["notes"])
    data = heavy["tmp"] / "data"
    assert not (data / ab.MONTH_TOTAL).exists() or json.loads(
        (data / ab.MONTH_TOTAL).read_text()).get("stamp") is None
    assert all("s-b.jsonl" not in p for p in json.loads(
        (data / ab.MONTH_CACHE).read_text())["files"])
    monkeypatch.setattr(os, "open", real_open)
    u = ab.month_agent_usage()
    assert u["usd_cents"] == _uncached() and Decimal(u["usd_exact"]) == full
    assert not any("not read" in n for n in u["notes"])
    assert ab.month_agent_usage()["usd_cents"] == _uncached()


def test_a_transcript_read_cut_short_is_not_kept_either(heavy, monkeypatch):
    """The read fails after some lines: nothing of that transcript is kept."""
    ab.month_agent_usage()
    _append(heavy["claude"], "s-b", 3)
    real = ab._parse_usage_line
    seen = {"n": 0}

    def cut(line, path, lineno):
        if path.name == "s-b.jsonl":
            seen["n"] += 1
            if seen["n"] == 2:
                raise OSError(5, "Input/output error")
        return real(line, path, lineno)
    monkeypatch.setattr(ab, "_parse_usage_line", cut)
    u = ab.month_agent_usage()
    assert any("not read" in n for n in u["notes"])
    monkeypatch.setattr(ab, "_parse_usage_line", real)
    assert ab.month_agent_usage()["usd_cents"] == _uncached()


@pytest.mark.parametrize("lowered", ["s-a", "s-resumed"])
def test_two_copies_that_differ_count_the_larger_whatever_their_paths(heavy, lowered):
    """A response two transcripts hold counts once: the larger copy, so a
    copy rewritten lower in either file does not lower the month."""
    projects = heavy["claude"] / "projects" / "-home-alice-work"
    src = projects / "s-a.jsonl"
    (projects / "s-resumed.jsonl").write_text(src.read_text())
    target = projects / f"{lowered}.jsonl"
    rows = [json.loads(x) for x in target.read_text().splitlines()]
    for r in rows:
        if r.get("sessionId") == "s-a":
            r["message"]["usage"]["input_tokens"] = 1
            r["message"]["usage"]["output_tokens"] = 0
    target.write_text("".join(json.dumps(r) + "\n" for r in rows))
    for f in projects.glob("*.jsonl"):
        os.utime(f, (AT.timestamp(), AT.timestamp()))
    u = ab.month_agent_usage()
    assert Decimal(u["usd_exact"]) == Decimal("4.5") * 31
    assert ab.month_agent_usage()["usd_exact"] == u["usd_exact"]


def test_a_data_dir_written_with_a_tilde_is_the_one_the_guard_protects(heavy, monkeypatch):
    """FINOPS_DATA_DIR=~/nd, as a settings.json env entry gives it: the month
    cache goes under the home directory, where the guard's own files are,
    never under a literal ~ in the agent's working directory."""
    from finops import guard_paths, harness_usage
    home = heavy["tmp"] / "home"
    monkeypatch.setenv("FINOPS_DATA_DIR", "~/nd")
    work = heavy["tmp"] / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    ab.month_agent_usage()
    assert (home / "nd" / ab.MONTH_TOTAL).is_file()
    assert not (work / "~").exists()
    assert harness_usage._cursor_cache_path() == home / "nd" / "cursor-usage.json"
    assert ab._budget_path() == home / "nd" / "ai-budget.json"
    shown = {e.path for e in guard_paths.protected()}
    assert os.path.realpath(home / "nd" / ab.MONTH_TOTAL) in shown


def test_doctor_says_the_transcripts_are_not_the_guards_own_files(heavy):
    """The month to date is read from the agents' own logs, which the guard
    does not protect: doctor says so wherever an org model budget applies."""
    gaps = guard.doctor()["not_covered"]
    assert any("transcripts" in g and "not among the guard's own files" in g for g in gaps), gaps
