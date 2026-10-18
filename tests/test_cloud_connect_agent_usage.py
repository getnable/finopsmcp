# SPDX-License-Identifier: Apache-2.0
"""`nable cloud connect` carries coding-agent usage, one record a day (D88).

One record per (source, UTC day, developer, agent, model, project), for closed
UTC days in the last 35, each a payload line of exactly these keys, sorted, no
spaces:

    {"agent", "basis": "list_price", "day", "developer", "kind": "agent_usage",
     "model", "price_source", "project", "source", "tokens": {"cache_read",
     "cache_write", "input", "output"}, "unpriced_tokens", "usd", "v": 1}

What has to hold: USD only for tokens priced at a cited llm_prices row, rounded
up to the millionth, with unpriced tokens counted and carrying no dollars; the
project is the repo's identity only when the org model holds a repo_path fact
for that repo, else "unmapped"; the source is a random 128-bit id made once per
install; the developer is the sender (--as, else on a terminal git's
user.email, then $USER); no prompt, file content, path, host name or machine
id is in the bytes. The dry run shows it all, and the confirm code covers it:
change a record and the code changes. A send still needs a terminal or --as,
and a box that does not take the record kind is told so before anything is
sent.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from finops import ai_budget, cli_cloud, cloud_connect as cc, guard_ledger, llm_prices

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
BOX = "https://box.example.com"
KEY = "nbl_test_workspace_key_0123456789abcdef"  # pragma: allowlist secret
PROMPT = "PLEASE-REFACTOR-THE-SECRET-BILLING-MODULE"
CONTENTS = "FILE-CONTENTS-MARKER-api_key=hunter2"  # pragma: allowlist secret
HOST = "alice-laptop.corp.example"
RECORD_KEYS = ["agent", "basis", "day", "developer", "kind", "model", "price_source",
               "project", "source", "tokens", "unpriced_tokens", "usd", "v"]


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _claude(claude: Path, session: str, when: datetime, *, model="claude-sonnet-4-6",
            inp=1_000_000, out=100_000, cw=0, cr=0, cwd: str, n=1):
    f = claude / "projects" / "-home-alice-work" / f"{session}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        # A user turn with the prompt and a tool result with a file's contents:
        # what the payload must never carry.
        fh.write(json.dumps({"timestamp": _iso(when), "sessionId": session, "cwd": cwd,
                             "type": "user", "message": {"role": "user", "content": PROMPT}})
                 + "\n")
        fh.write(json.dumps({"timestamp": _iso(when), "sessionId": session, "cwd": cwd,
                             "type": "user", "hostname": HOST,
                             "message": {"role": "user", "content": [
                                 {"type": "tool_result", "content": CONTENTS}]}}) + "\n")
        for i in range(n):
            fh.write(json.dumps({
                "timestamp": _iso(when + timedelta(seconds=i)), "sessionId": session,
                "cwd": cwd, "requestId": f"req-{session}-{when:%m%d}-{i}",
                "message": {"id": f"msg-{session}-{model}-{when:%m%d%H}-{i}", "model": model,
                            "content": [{"type": "text", "text": PROMPT}],
                            "usage": {"input_tokens": inp, "output_tokens": out,
                                      "cache_creation_input_tokens": cw,
                                      "cache_read_input_tokens": cr}}}) + "\n")
    os.utime(f, (NOW.timestamp(), NOW.timestamp()))


def _codex(codex: Path, when: datetime, cwd: str):
    f = codex / "sessions" / when.strftime("%Y/%m/%d") / (
        f"rollout-{when:%Y-%m-%dT%H-%M-%S}-thread-1.jsonl")
    f.parent.mkdir(parents=True, exist_ok=True)
    usage = {"input_tokens": 3000, "cached_input_tokens": 1000, "cache_write_input_tokens": 0,
             "output_tokens": 500, "reasoning_output_tokens": 0, "total_tokens": 3500}
    lines = [
        {"timestamp": _iso(when), "type": "session_meta",
         "payload": {"id": "thread-1", "session_id": "thread-1", "timestamp": _iso(when),
                     "cwd": cwd}},
        {"timestamp": _iso(when), "type": "turn_context",
         "payload": {"turn_id": "t1", "cwd": cwd, "model": "gpt-5-codex"}},
        {"timestamp": _iso(when), "type": "token_usage_record",
         "payload": {"thread_id": "thread-1", "turn_id": "t1", "session_id": "thread-1",
                     "response_id": "resp-1", "usage": usage}},
    ]
    f.write_text("".join(json.dumps(x) + "\n" for x in lines))
    os.utime(f, (NOW.timestamp(), NOW.timestamp()))


def _repo(path: Path, remote: str | None) -> Path:
    (path / ".git").mkdir(parents=True)
    if remote:
        (path / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {remote}\n')
    return path


ORG = ("# nable org model v1\n- fact: owner\n"
       "  subject: {kind: repo_path, id: 'github.com/acme/payments//.'}\n"
       "  value: {team: payments}\n  source: human\n  status: confirmed\n")


@pytest.fixture
def laptop(tmp_path, monkeypatch):
    home = tmp_path / "home" / "alice"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    claude = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    codex = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    org = tmp_path / "org"
    org.mkdir()
    (org / "owners.yaml").write_text(ORG)
    monkeypatch.setenv("FINOPS_ORG_DIR", str(org))
    monkeypatch.setattr(socket, "gethostname", lambda: HOST)
    pay = _repo(home / "work" / "payments", "git@github.com:acme/payments.git")
    scratch = _repo(home / "work" / "scratch-notes", "https://github.com/alice/scratch.git")
    (pay / "src").mkdir()
    (pay / "src" / "billing.py").write_text(CONTENTS)
    _claude(claude, "s-1", NOW - timedelta(days=1), cwd=str(pay / "src"), n=2)       # 10-17
    _claude(claude, "s-2", NOW - timedelta(days=1), cwd=str(scratch))                # unmapped
    _claude(claude, "s-3", NOW - timedelta(days=3), model="claude-haiku-5-5", inp=9_000_000,
            out=0, cwd=str(pay))                                                     # unpriced
    _claude(claude, "s-4", NOW - timedelta(hours=1), cwd=str(pay))                   # today
    _claude(claude, "s-5", datetime(2026, 9, 13, 8, 0, tzinfo=UTC), cwd=str(pay))   # 35 days back
    _claude(claude, "s-6", datetime(2026, 9, 12, 23, 0, tzinfo=UTC), cwd=str(pay))  # 36: out
    _claude(claude, "s-7", NOW - timedelta(days=2), cwd=str(tmp_path / "gone"))     # no repo
    _codex(codex, NOW - timedelta(days=2), cwd=str(pay))
    return {"home": home, "pay": pay, "scratch": scratch, "claude": claude, "tmp": tmp_path}


def _build(**kw):
    kw.setdefault("developer", "alice@acme.example")
    return cc.build(now=NOW, **kw)


def _records(payload) -> list[dict]:
    return [json.loads(payload.data[a:b]) for kind, a, b in payload.lines
            if kind == "agent_usage"]


# ── the record format, the seam ──────────────────────────────────────────────

def test_each_line_is_the_record_format_exactly(laptop):
    p = _build()
    lines = [p.data[a:b] for kind, a, b in p.lines if kind == "agent_usage"]
    assert lines
    for raw in lines:
        doc = json.loads(raw)
        assert list(doc) == RECORD_KEYS
        assert raw == json.dumps(doc, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False).encode() + b"\n"
        assert doc["kind"] == "agent_usage" and doc["v"] == 1 and doc["basis"] == "list_price"
        assert doc["agent"] in ("claude_code", "codex", "cursor")
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", doc["day"])
        assert re.fullmatch(r"[0-9a-f]{32}", doc["source"])
        assert list(doc["tokens"]) == ["cache_read", "cache_write", "input", "output"]
        assert all(isinstance(v, int) and v >= 0 for v in doc["tokens"].values())
        assert isinstance(doc["unpriced_tokens"], int)
        assert re.fullmatch(r"\d+(\.\d{1,6})?", doc["usd"])
        assert doc["developer"] == "alice@acme.example"


def test_one_record_per_day_developer_agent_model_and_project(laptop):
    got = {(r["day"], r["agent"], r["model"], r["project"]): r for r in _records(_build())}
    assert set(got) == {
        ("2026-10-17", "claude_code", "claude-sonnet-4-6", "github.com/acme/payments"),
        ("2026-10-17", "claude_code", "claude-sonnet-4-6", "unmapped"),
        ("2026-10-15", "claude_code", "claude-haiku-5-5", "github.com/acme/payments"),
        ("2026-10-16", "claude_code", "claude-sonnet-4-6", "unmapped"),
        ("2026-10-16", "codex", "gpt-5-codex", "github.com/acme/payments"),
        ("2026-09-13", "claude_code", "claude-sonnet-4-6", "github.com/acme/payments"),
    }
    two = got[("2026-10-17", "claude_code", "claude-sonnet-4-6", "github.com/acme/payments")]
    assert two["tokens"] == {"cache_read": 0, "cache_write": 0, "input": 2_000_000,
                             "output": 200_000}
    assert two["usd"] == "9"                       # 2 x ($3.00 + $1.50) at $3/$15 a million


def test_closed_utc_days_in_the_last_35_only(laptop):
    days = {r["day"] for r in _records(_build())}
    assert "2026-10-18" not in days, "today is not closed"
    assert "2026-09-12" not in days, "36 days back is outside the window"
    assert "2026-09-13" in days


def test_dollars_only_at_a_cited_rate_and_unpriced_tokens_carry_none(laptop):
    recs = {(r["model"], r["day"]): r for r in _records(_build())}
    haiku = recs[("claude-haiku-5-5", "2026-10-15")]
    assert (haiku["usd"], haiku["unpriced_tokens"]) == ("0", 9_000_000)
    assert "no llm_prices row" in haiku["price_source"]
    sonnet = recs[("claude-sonnet-4-6", "2026-09-13")]
    assert sonnet["unpriced_tokens"] == 0
    assert "claude-sonnet-4-6" in sonnet["price_source"]
    assert llm_prices.ANTHROPIC_SOURCE in sonnet["price_source"]
    assert llm_prices.AS_OF_BY_PROVIDER["anthropic"] in sonnet["price_source"]
    codex = recs[("gpt-5-codex", "2026-10-16")]
    # fresh input 2,000 at $1.25, cached 1,000 at $0.125, output 500 at $10 a million
    assert Decimal(codex["usd"]) == Decimal("0.007625")
    assert codex["tokens"] == {"cache_read": 1000, "cache_write": 0, "input": 2000,
                               "output": 500}


def test_dollars_round_up_to_the_millionth(laptop, monkeypatch):
    _claude(laptop["claude"], "s-9", NOW - timedelta(days=4), inp=1, out=0,
            cwd=str(laptop["pay"]))                  # 1 token at $3 a million: $0.000003
    _claude(laptop["claude"], "s-9", NOW - timedelta(days=4), model="claude-haiku-4-5", inp=1,
            out=0, cwd=str(laptop["pay"]))           # $0.000001
    _claude(laptop["claude"], "s-9", NOW - timedelta(days=4), model="claude-3-5-haiku",
            inp=1, out=0, cwd=str(laptop["pay"]))    # $0.0000008, rounded up
    _claude(laptop["claude"], "s-9", NOW - timedelta(days=4), model="claude-opus-4-1",
            inp=0, out=0, cr=1, cwd=str(laptop["pay"]))   # one cached read: $0.0000015
    _claude(laptop["claude"], "s-9", NOW - timedelta(days=4), model="claude-sonnet-4-5",
            inp=0, out=0, cr=1, cwd=str(laptop["pay"]))   # $0.0000003: up, never to 0
    recs = {r["model"]: r for r in _records(_build()) if r["day"] == "2026-10-14"}
    assert recs["claude-sonnet-4-6"]["usd"] == "0.000003"
    assert recs["claude-haiku-4-5"]["usd"] == "0.000001"
    assert recs["claude-3-5-haiku"]["usd"] == "0.000001"
    assert recs["claude-opus-4-1"]["usd"] == "0.000002"
    assert recs["claude-sonnet-4-5"]["usd"] == "0.000001"


def test_the_exact_rate_matches_the_float_one():
    for model, price in llm_prices.MODEL_PRICES.items():
        kw = {"input_tokens": 1_234_567, "output_tokens": 89_012, "cache_write_5m_tokens": 3_456,
              "cache_write_1h_tokens": 789, "cache_read_tokens": 4_321_000}
        for fast in (False, True):
            for us in (False, True):
                exact = price.cost_exact(**kw, fast=fast, us_only=us)
                assert isinstance(exact, Decimal)
                assert abs(float(exact) - price.cost(**kw, fast=fast, us_only=us)) < 1e-9, model


# ── what is in the bytes, and what is not ────────────────────────────────────

def test_no_cwd_home_prompt_contents_or_host_name_is_in_the_payload(laptop):
    p = _build()
    blob = p.data
    for said in (str(laptop["pay"]), str(laptop["home"]), "/home/alice", "scratch-notes",
                 PROMPT, CONTENTS, "hunter2", HOST, "alice-laptop", "s-1", "req-s-1",
                 str(laptop["tmp"])):
        assert said.encode() not in blob, said
    assert b"github.com/acme/payments" in blob    # the one repo the org model names


def test_a_repo_the_org_model_does_not_name_is_unmapped(laptop):
    projects = {r["project"] for r in _records(_build())}
    assert projects == {"github.com/acme/payments", "unmapped"}
    assert not any("alice/scratch" in r["project"] for r in _records(_build()))


def test_the_source_is_made_once_per_install(laptop):
    a = {r["source"] for r in _records(_build())}
    b = {r["source"] for r in _records(_build())}
    assert a == b and len(a) == 1
    path = guard_ledger.ledger_path().parent / cc.SOURCE_NAME
    assert path.read_text().strip() == a.pop()
    assert (path.stat().st_mode & 0o077) == 0
    path.unlink()
    assert {r["source"] for r in _records(_build())} != b


def test_a_path_a_model_was_logged_as_never_leaves(laptop):
    _claude(laptop["claude"], "s-8", NOW - timedelta(days=5),
            model="/home/alice/models/llama.gguf", cwd=str(laptop["pay"]))
    p = _build()
    assert b"/home/alice" not in p.data
    assert any(r["model"] == "unknown" for r in _records(p))


# ── who the developer is ─────────────────────────────────────────────────────

def _args(*argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    cli_cloud.add_parser(parser.add_subparsers(dest="cmd"))
    return parser.parse_args(["cloud", "connect", "--box", BOX, *argv])


def _dry(*argv, terminal=False, monkeypatch=None):
    out, err = io.StringIO(), io.StringIO()
    code = cli_cloud.run(_args(*argv, "--json"), stdout=out, stderr=err, prompt=None,
                         terminal=terminal, now=NOW)
    return code, json.loads(out.getvalue()), err.getvalue()


def test_the_developer_is_the_sender_named_with_as(laptop, monkeypatch):
    monkeypatch.setattr(cli_cloud, "_git_email", lambda: "git@acme.example")
    code, doc, _ = _dry("--as", "Alice@Acme.example")
    assert code == 0
    assert doc["agent_usage"]["developer"] == "Alice@Acme.example"
    assert doc["agent_usage"]["developer_from"] == "--as"


def test_on_a_terminal_it_is_git_user_email_then_user(laptop, monkeypatch):
    monkeypatch.setattr(cli_cloud, "_git_email", lambda: "alice@acme.example")
    code, doc, _ = _dry(terminal=True)
    assert doc["agent_usage"]["developer"] == "alice@acme.example"
    monkeypatch.setattr(cli_cloud, "_git_email", lambda: None)
    monkeypatch.setenv("USER", "alice")
    code, doc, _ = _dry(terminal=True)
    assert (doc["agent_usage"]["developer"], doc["agent_usage"]["developer_from"]) == (
        "alice", "$USER")


def test_without_a_terminal_or_as_no_record_is_built(laptop, monkeypatch):
    monkeypatch.setattr(cli_cloud, "_git_email", lambda: "alice@acme.example")
    code, doc, _ = _dry(terminal=False)
    assert doc["agent_usage"]["records"] == 0
    assert "--as" in doc["agent_usage"]["left_out"]
    assert not any(k == "agent_usage" for k, *_ in cc.build(now=NOW).lines)


def test_no_agent_usage_leaves_it_out(laptop, monkeypatch):
    code, doc, _ = _dry("--as", "alice@acme.example", "--no-agent-usage")
    assert doc["agent_usage"]["records"] == 0 and "--no-agent-usage" in doc["agent_usage"][
        "left_out"]


# ── the dry run shows it, and the confirm code covers it ─────────────────────

def test_the_dry_run_shows_counts_days_developer_agents_models_projects_tokens_and_usd(
        laptop, monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    assert cli_cloud.run(_args("--as", "alice@acme.example"), stdout=out, stderr=err,
                         prompt=None, terminal=False, now=NOW) == 0
    text = out.getvalue()
    for said in ("Coding agents", "6 records", "2026-09-13 to 2026-10-17",
                 "alice@acme.example", "Claude Code", "Codex", "claude-sonnet-4-6",
                 "gpt-5-codex", "claude-haiku-5-5", "github.com/acme/payments", "unmapped",
                 "input", "output", "cache read", "list-price equivalent",
                 "not an invoice", "llm_prices", "9,000,000 tokens", "not priced"):
        assert said in text, said
    code, doc, _ = _dry("--as", "alice@acme.example")
    u = doc["agent_usage"]
    assert u["records"] == 6 and u["days"] == {"first": "2026-09-13", "last": "2026-10-17",
                                               "count": 4}
    assert u["agents"] == ["claude_code", "codex"] and u["basis"] == "list_price"
    assert u["projects"] == ["github.com/acme/payments"] and u["unmapped_records"] == 2
    assert u["unpriced_tokens"] == 9_000_000
    assert Decimal(u["usd"]) == sum(Decimal(r["usd"]) for r in _records(_build()))
    assert u["price_sources"]


def test_the_confirm_code_changes_when_any_record_changes(laptop):
    a = _build()
    code_a = cc.confirm_code(a, BOX)
    _claude(laptop["claude"], "s-1", NOW - timedelta(days=1), out=1, cwd=str(laptop["pay"]))
    b = _build()
    assert cc.confirm_code(b, BOX) != code_a
    assert cc.confirm_code(_build(developer="bob@acme.example"), BOX) != cc.confirm_code(b, BOX)


def test_a_send_with_the_old_code_after_a_record_changed_is_refused(laptop, monkeypatch):
    code0, doc, _ = _dry("--as", "alice@acme.example")
    _claude(laptop["claude"], "s-1", NOW - timedelta(days=1), out=1, cwd=str(laptop["pay"]))
    sent = []
    out, err = io.StringIO(), io.StringIO()
    code = cli_cloud.run(_args("--send", "--confirm", doc["confirm_sha256"], "--as",
                               "alice@acme.example"),
                         opener=lambda req, t: sent.append(req) or (200, b"{}"),
                         environ={cc.KEY_ENV: KEY}, stdout=out, stderr=err, prompt=None,
                         terminal=False, now=NOW)
    assert code == cli_cloud.EXIT_REFUSED and sent == []


def test_a_send_still_needs_a_terminal_or_as(laptop):
    p = _build()
    out, err = io.StringIO(), io.StringIO()
    code = cli_cloud.run(_args("--send", "--confirm", cc.confirm_code(p, BOX)),
                         opener=lambda req, t: (200, b"{}"), environ={cc.KEY_ENV: KEY},
                         stdout=out, stderr=err, prompt=None, terminal=False, now=NOW)
    assert code == cli_cloud.EXIT_USAGE and "--as" in err.getvalue()


class _Box:
    def __init__(self, accepts=("org_model", "ledger_line", "agent_usage")):
        self.accepts = list(accepts)
        self.posts: list[bytes] = []

    def __call__(self, req, timeout):
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        if req.get_method() == "GET" and path == "api/free-tool/status":
            return 200, json.dumps({"org_model": None, "chains": [],
                                    "accepts": self.accepts}).encode()
        self.posts.append(req.data)
        return 200, json.dumps({"accepted": {}}).encode()


def test_what_is_sent_is_exactly_what_was_shown_and_agent_usage_is_resent_whole(laptop):
    p = _build()
    box = _Box()
    cc.send(p, BOX, KEY, opener=box)
    assert b"".join(box.posts) == p.data
    again = _Box()
    cc.send(p, BOX, KEY, opener=again)
    assert b"".join(again.posts) == p.data, "a resend replaces records, so all go again"


def test_a_box_that_does_not_take_agent_usage_is_told_before_anything_is_sent(laptop):
    p = _build()
    box = _Box(accepts=("org_model", "ledger_line"))
    with pytest.raises(cc.ConnectError, match="--no-agent-usage"):
        cc.send(p, BOX, KEY, opener=box)
    assert box.posts == []


def test_a_box_that_says_nothing_of_what_it_takes_is_one_that_does_not(laptop):
    p = _build()

    def old(req, timeout):
        if req.get_method() == "GET":
            return 200, b'{"org_model": null, "chains": []}'
        raise AssertionError("posted to a box that never said it takes agent usage")
    with pytest.raises(cc.ConnectError):
        cc.send(p, BOX, KEY, opener=old)


def test_unreadable_usage_is_not_read_never_zero(laptop, monkeypatch):
    def broken(since, **kw):
        raise OSError("a transcript could not be read")
    monkeypatch.setattr(ai_budget, "agent_responses", broken)
    p = _build()
    assert p.usage["records"] == 0 and "not read" in p.usage["left_out"]
    assert "usd" not in p.usage or p.usage["usd"] is None


def test_guard_py_imports_neither_cloud_connect_nor_telemetry():
    src = Path(__file__).resolve().parents[1] / "src" / "finops" / "guard.py"
    text = src.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(?:from|import)\s+(?:\.|finops\.)?\s*(?:cloud_connect|telemetry)\b",
                         text, re.M)
    assert "from . import cloud_connect" not in text and "from .telemetry" not in text
    assert hashlib.sha256(text.encode()).hexdigest()   # read, not imported


def test_the_source_file_is_one_of_the_guards_own_files(laptop):
    from finops import guard, guard_paths
    _build()
    path = guard_ledger.ledger_path().parent / cc.SOURCE_NAME
    assert guard_paths.match_file(str(path)) is not None
    v = guard.gate_command(f"echo 0123 > {path}", record=False)
    assert v is not None and v["decision"] == "ask"
