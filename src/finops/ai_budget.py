"""
Local budget for your AI coding agent.

nable's cost tools point at your cloud. This one points at the agent itself: the
tokens Claude Code / Cursor burn against your Claude or Cursor plan, and the dollars
a metered API key spends. It answers one question before you (or the agent) kick off
a big task: "am I about to blow my budget?"

Two honest data sources, no guessing:

  1. Local usage meter (subscription plans). Claude Code writes every message's real
     token usage to ~/.claude/projects/**/*.jsonl. We tally it over a rolling window
     and month-to-date. This is exact token counts, read locally, nothing leaves the
     machine. What we deliberately do NOT do: claim a percentage of Anthropic's Max
     rate-limit. That number is not exposed by any API, so we report real burn rate
     against YOUR budget instead of a fabricated "% of plan left".

  2. Metered API spend (pay-per-token keys). get_all_llm_costs gives real provider
     dollars month-to-date. Precise budgeting for OpenAI/Anthropic/Bedrock API keys.

The gate, `check(...)`, mirrors policy.py: ok / warn / over, advice only. It never
stops the agent; it tells you where you stand so you decide.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import harness_usage, llm_prices, token_budget

# ── Verdicts (mirror policy.py's vocabulary) ─────────────────────────────────
BUDGET_OK = "ok"        # comfortably under budget
BUDGET_WARN = "warn"    # crossed the warn threshold (default 80%)
BUDGET_OVER = "over"    # at or past the budget

_WARN_AT = float(os.getenv("FINOPS_AI_BUDGET_WARN_PCT", "0.80"))

# A rolling window for "right now" usage. Claude's heaviest plan gate is a ~5h
# window, so 5h is a sensible default to show burn against. Configurable.
_WINDOW_HOURS = float(os.getenv("FINOPS_AI_WINDOW_HOURS", "5"))

# API-equivalent dollars so a subscription user sees a figure they can reason about
# ("this session would be ~$18 on the API"). Not what the plan charges (that is
# flat); it is the metered-equivalent, priced per response at the list rate of the
# model that produced it (llm_prices). A model that table does not know falls back
# to this blended rate, Sonnet 4.6's by default, and is reported as unpriced rather
# than silently folded in. Configurable for the model you run.
_BLEND = llm_prices.MODEL_PRICES["claude-sonnet-4-6"]

# Cache rates as multiples of the input rate: a 5-minute write 1.25x, a 1-hour
# write 2x, a read 0.1x, the ratios llm_prices carries for Anthropic's models.
# Derived from FINOPS_AI_USD_PER_MTOK_IN, so setting the input rate you pay
# moves them with it, unless each is set on its own.
_CACHE_RATES = (("FINOPS_AI_USD_PER_MTOK_CACHE_WRITE", 1.25),
                ("FINOPS_AI_USD_PER_MTOK_CACHE_WRITE_1H", 2.0),
                ("FINOPS_AI_USD_PER_MTOK_CACHE_READ", 0.1))


def _env_rate(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _fallback() -> llm_prices.ModelPrice:
    """The rate an unpriced model is charged at, read from the environment now."""
    inp = _env_rate("FINOPS_AI_USD_PER_MTOK_IN", _BLEND.input)
    (w5, m5), (w1, m1), (rd, mr) = _CACHE_RATES
    return llm_prices.ModelPrice(
        model="fallback", provider="fallback",
        input=inp, output=_env_rate("FINOPS_AI_USD_PER_MTOK_OUT", _BLEND.output),
        cache_write_5m=_env_rate(w5, inp * m5), cache_write_1h=_env_rate(w1, inp * m1),
        cache_read=_env_rate(rd, inp * mr))


def _data_dir() -> Path:
    d = Path(os.getenv("FINOPS_DATA_DIR") or (Path.home() / ".nable"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _budget_path() -> Path:
    return _data_dir() / "ai-budget.json"


_SESSION_CAPS_KEPT = 100


# ── Budget config ────────────────────────────────────────────────────────────

def get_budget() -> dict[str, Any]:
    """The user's AI budget. Empty/zero fields mean 'not set'.

    mode: 'flat' (a subscription, budget = plan_cost + optional usage cap) or
    'metered' (pay-per-token, budget = spend_cap). monthly_tokens is a usage cap
    that works in either mode ("warn before I burn N tokens").

    session_cap is a per-task cap in list-price dollars, measured against one
    Claude Code session (its subagents included) rather than the month:
    "this task may spend at most $40". It applies to every session;
    session_caps overrides it for named sessions."""
    # on_breach: what the guard does when usage crosses the budget.
    #   "notify" (default) -> the agent asks the human before continuing
    #   "stop"             -> the agent is denied outright
    # Asked once at `nable guard install`. Default is notify because a tool that
    # silently halts your agent gets uninstalled before anyone finds the setting
    # that caused it; stopping has to be a thing you chose.
    default = {"mode": "", "plan_cost": 0.0, "spend_cap": 0.0,
               "monthly_tokens": 0, "plan_label": "", "set_at": 0.0,
               "on_breach": "notify", "session_cap": 0.0, "session_caps": {}}
    try:
        data = json.loads(_budget_path().read_text())
        if isinstance(data, dict):
            default.update({k: data[k] for k in default if k in data})
    except (OSError, ValueError):
        pass
    caps = default["session_caps"]
    default["session_caps"] = ({str(k): float(v) for k, v in caps.items()
                                if isinstance(v, (int, float)) and v > 0}
                               if isinstance(caps, dict) else {})
    return default


def set_budget(mode: str | None = None, plan_cost: float | None = None,
               spend_cap: float | None = None, monthly_tokens: int | None = None,
               plan_label: str | None = None,
               on_breach: str | None = None,
               session_cap: float | None = None,
               session_id: str | None = None) -> dict[str, Any]:
    """Set the AI budget. `mode` is 'flat' (subscription: pass plan_cost) or
    'metered' (pay-per-token: pass spend_cap). Passing plan_cost/spend_cap infers
    the mode. monthly_tokens is an optional usage cap for either. Any subset.

    session_cap with a session_id caps that one session; without one it is the
    cap for every session. 0 clears it. A negative value is an error (it used
    to clear the cap silently), and nothing is saved."""
    for name, value in (("plan_cost", plan_cost), ("spend_cap", spend_cap),
                        ("monthly_tokens", monthly_tokens), ("session_cap", session_cap)):
        if value is not None and value < 0:
            raise ValueError(f"{name} cannot be negative ({value:g}); pass 0 to clear it, "
                             f"nothing was saved.")
    b = get_budget()
    if mode in ("flat", "metered"):
        b["mode"] = mode
    if plan_cost is not None:
        b["plan_cost"] = max(0.0, float(plan_cost))
        if not b["mode"]:
            b["mode"] = "flat"
    if spend_cap is not None:
        b["spend_cap"] = max(0.0, float(spend_cap))
        if not b["mode"]:
            b["mode"] = "metered"
    if monthly_tokens is not None:
        b["monthly_tokens"] = max(0, int(monthly_tokens))
    if plan_label is not None:
        b["plan_label"] = plan_label
    if on_breach in ("stop", "notify"):
        b["on_breach"] = on_breach
    if session_cap is not None:
        cap = max(0.0, float(session_cap))
        if session_id:
            caps = b["session_caps"]
            caps.pop(session_id, None)          # re-insert so the newest is last
            if cap > 0:
                caps[session_id] = cap
            # One entry per task someone capped; keep the recent ones.
            b["session_caps"] = dict(list(caps.items())[-_SESSION_CAPS_KEPT:])
        else:
            b["session_cap"] = cap
    b["set_at"] = time.time()
    fd = os.open(_budget_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(b, fh)
    return b


def reset_budget() -> None:
    """Forget the saved budget (so the next run re-asks)."""
    try:
        _budget_path().unlink()
    except OSError:
        pass


# ── Local usage meter (Claude Code session logs) ─────────────────────────────

def _claude_projects_dir() -> Path:
    base = os.getenv("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def read_agent_usage(since_epoch: float, *, allow_network: bool = True) -> dict[str, Any]:
    """Tally agent token usage across all local sessions since `since_epoch`.

    Every harness the guard hooks: Claude Code transcripts and Codex CLI
    rollouts, read locally, plus Cursor's Admin API when CURSOR_ADMIN_API_KEY
    is set (harness_usage). Exact counts. Skips log files whose mtime predates
    the window so a long history stays cheap. Returns totals, a per-model and
    per-harness split of tokens and of list-price dollars, the models priced at
    the fallback rate, the costliest sessions, and first/last activity.

    allow_network=False (the guard) reads Cursor only from its cache.
    """
    proj = _claude_projects_dir()
    claude = proj.is_dir()
    responses = _responses(proj, since_epoch) if claude else []
    return _tally(responses + _other_harnesses(since_epoch, allow_network=allow_network),
                  source_present=claude, since_epoch=since_epoch)


def read_session_usage(session_id: str, *, allow_network: bool = True) -> dict[str, Any]:
    """Everything one session has used, from its first response.

    A Claude Code session is the sessionId it stamps on every transcript line,
    and a Codex session is the root thread id its rollouts carry, so in both the
    subagents it spawned count toward it: they are part of the same task. A
    Cursor session is a conversation, when its Admin API is connected.
    """
    if not session_id:
        return _tally([], source_present=False)
    proj = _claude_projects_dir()
    claude = proj.is_dir()
    responses = _responses(proj, 0, session_id=session_id) if claude else []
    return _tally(responses + _other_harnesses(0, session_id=session_id,
                                               allow_network=allow_network),
                  source_present=claude)


def _other_harnesses(since_epoch: float, session_id: str | None = None, *,
                     allow_network: bool = True) -> list[dict[str, Any]]:
    """Codex and Cursor responses. A reader that fails counts nothing rather
    than taking the Claude Code numbers down with it. allow_network=False
    keeps Cursor to its cache (see harness_usage.cursor_responses)."""
    out: list[dict[str, Any]] = []
    if session_id is None or _SAFE_SESSION_ID.match(session_id):
        with contextlib.suppress(*_READER_ERRORS):
            out.extend(harness_usage.codex_responses(since_epoch, session_id=session_id))
    with contextlib.suppress(*_READER_ERRORS):
        out.extend(harness_usage.cursor_responses(since_epoch, session_id=session_id,
                                                  month_start=_month_start_epoch(),
                                                  allow_network=allow_network))
    return out


# What reading a malformed log can raise. Anything else is a bug worth seeing.
_READER_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError)


# Claude Code names a session's transcript <project>/<sessionId>.jsonl and puts its
# subagents under <project>/<sessionId>/subagents/. A session is found by those
# names and nothing else: the guard reads it on every tool call, and falling back
# to a scan of the whole history would put seconds on each one. An id that is not
# safe to put in a glob matches nothing.
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _transcripts(proj: Path, session_id: str | None) -> list[Path]:
    if not session_id:
        return list(proj.rglob("*.jsonl"))
    if not _SAFE_SESSION_ID.match(session_id):
        return []
    return [*proj.glob(f"*/{session_id}.jsonl"), *proj.glob(f"*/{session_id}/**/*.jsonl")]


def _path_session(path: Path) -> str:
    return path.parent.parent.name if path.parent.name == "subagents" else path.stem


def _responses(proj: Path, since_epoch: float,
               session_id: str | None = None) -> list[dict[str, Any]]:
    """One entry per API response in the window, however many lines logged it.

    Claude Code writes a transcript line per content block of a response
    (thinking, text, each tool_use), and every one of those lines carries the
    whole response's usage. Summing lines counted each response's input and
    cache tokens once per block: about 2x on real transcripts, with output 1.3x
    because output_tokens grows as the blocks stream. Keyed on the response, the
    last line wins, and it holds the final output count. A line with no message
    id (older logs) is its own response.

    The same response can sit in more than one transcript (a resumed session
    copies the conversation it resumes), so the key is shared across files and
    the last file read wins, as it did before each file's parse was cached.
    """
    seen: dict[Any, dict[str, Any]] = {}
    paths = _transcripts(proj, session_id)
    cache = _TallyCache.open()
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_mtime < since_epoch - 1:
            continue  # whole file is older than the window
        for key, r in _file_responses(path, st, cache).items():
            if r["ts"] < since_epoch or (session_id and r["session"] != session_id):
                continue
            seen[key] = r
    if cache is not None and not session_id:
        cache.prune(paths)
    return list(seen.values())


def _parse_usage_line(line: bytes, path: Path, lineno: int) -> tuple[Any, dict] | None:
    """(response key, record) for one transcript line, or None when the line
    carries no usage. Nothing here depends on the window or the session asked
    for, so a file's parse can be kept and reused (see _TallyCache)."""
    if b'"usage"' not in line:
        return None
    try:
        rec = json.loads(line.decode("utf-8", errors="ignore"))
    except ValueError:
        return None
    if not isinstance(rec, dict):
        return None
    ts = _rec_epoch(rec.get("timestamp"))
    if ts is None:
        return None
    msg = rec.get("message") or {}
    usage = msg.get("usage") or {} if isinstance(msg, dict) else {}
    if not usage or not isinstance(usage, dict):
        return None
    ti = int(usage.get("input_tokens", 0) or 0)
    to = int(usage.get("output_tokens", 0) or 0)
    cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
    cr = int(usage.get("cache_read_input_tokens", 0) or 0)
    if ti == to == cw == cr == 0:
        return None
    session = str(rec.get("sessionId") or _path_session(path))
    # Claude Code writes its main-thread cache with the 1-hour TTL,
    # billed at 2x input against 1.25x for the 5-minute one. The
    # split is in usage.cache_creation; anything it does not
    # account for is priced as a 5-minute write.
    split = usage.get("cache_creation")
    cw_1h = 0
    if isinstance(split, dict):
        cw_1h = min(cw, int(split.get("ephemeral_1h_input_tokens", 0) or 0))
    key = ((msg.get("id"), rec.get("requestId")) if msg.get("id")
           else (str(path), lineno))
    return key, {
        "ts": ts, "model": str(msg.get("model", "") or "unknown"),
        "input": ti, "output": to, "cache_write": cw, "cache_read": cr,
        "cache_write_1h": cw_1h,
        "fast": usage.get("speed") == "fast",
        "us_only": usage.get("inference_geo") == "us",
        "session": session, "cwd": rec.get("cwd"),
        "harness": harness_usage.HARNESS_CLAUDE,
    }


def _file_responses(path: Path, st: os.stat_result,
                    cache: _TallyCache | None) -> dict[Any, dict[str, Any]]:
    """Every response one transcript holds, key -> its last line's record.

    From the cache when the file is unchanged; when it has only grown (Claude
    Code appends), from the cached parse plus the new lines; otherwise, or
    without a cache, read whole."""
    entry = cache.get(path) if cache is not None else None
    out: dict[Any, dict[str, Any]] = {}
    offset = lines = 0
    if entry is not None:
        if entry.unchanged(path, st):
            return entry.responses
        if entry.grew(path, st):
            out, offset, lines = dict(entry.responses), entry.offset, entry.lines
    tail = b""
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            for line in fh:
                if not line.endswith(b"\n"):
                    break          # a line still being written: read it next time
                hit = _parse_usage_line(line, path, lines)
                if hit is not None:
                    out[hit[0]] = hit[1]
                lines += 1
                offset += len(line)
                tail = line
    except OSError:
        return out
    if cache is not None:
        cache.put(path, st, out, offset, lines, tail)
    return out


class _TallyEntry:
    __slots__ = ("ino", "lines", "mtime_ns", "offset", "responses", "size", "tail")

    def __init__(self, data: dict[str, Any]):
        self.size = int(data["size"])
        self.mtime_ns = int(data["mtime_ns"])
        self.ino = int(data["ino"])
        self.offset = int(data["offset"])
        self.lines = int(data["lines"])
        self.tail = bytes.fromhex(data["tail"])
        self.responses = {_TallyCache.key_in(k): r for k, r in data["responses"]}

    def _tail_in_place(self, path: Path) -> bool:
        """The last line the cached parse read still sits where it ended. A
        cheap guard against a file rewritten in place to the same size within
        the filesystem's timestamp resolution."""
        if not self.tail:
            return True
        try:
            with path.open("rb") as fh:
                fh.seek(self.offset - len(self.tail))
                return fh.read(len(self.tail)) == self.tail
        except OSError:
            return False

    def unchanged(self, path: Path, st: os.stat_result) -> bool:
        return ((st.st_size, st.st_mtime_ns, st.st_ino) == (self.size, self.mtime_ns, self.ino)
                and self._tail_in_place(path))

    def grew(self, path: Path, st: os.stat_result) -> bool:
        """The same file with lines appended (Claude Code only appends to a
        transcript): same inode, larger, and the parse's last line in place."""
        return (st.st_ino == self.ino and st.st_size > self.offset and bool(self.tail)
                and self._tail_in_place(path))


class _TallyCache:
    """Each transcript's parsed responses, kept in the data dir so the guard
    does not re-read a month of transcripts on every tool call (1.6 s on
    386 MB of them) when a monthly cap is set.

    One file per transcript under ai-budget-tally/, named by a hash of its
    path and written atomically, so a growing transcript rewrites only its own
    entry. An entry is used as-is while the transcript's size, mtime and inode
    are unchanged, and extended from where it stopped when the file has only
    grown. Keys stay per response, not per-file totals: the same response can
    be in two transcripts, and _responses dedupes across files."""

    VERSION = 1

    def __init__(self, root: Path):
        self.root = root

    @classmethod
    def open(cls) -> _TallyCache | None:
        try:
            root = _data_dir() / "ai-budget-tally"
            root.mkdir(exist_ok=True)
            return cls(root)
        except OSError:
            return None

    @staticmethod
    def _name(path: Path) -> str:
        import hashlib
        return hashlib.sha256(str(path).encode("utf-8", "surrogateescape")).hexdigest()[:40]

    @staticmethod
    def key_in(k: list) -> Any:
        return tuple(k)

    def get(self, path: Path) -> _TallyEntry | None:
        try:
            data = json.loads((self.root / f"{self._name(path)}.json").read_text())
            if data.get("v") != self.VERSION or data.get("path") != str(path):
                return None
            return _TallyEntry(data)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    def put(self, path: Path, st: os.stat_result, responses: dict[Any, dict[str, Any]],
            offset: int, lines: int, tail: bytes) -> None:
        if not tail:
            entry = self.get(path)
            # Nothing new was read: keep the line the old parse ended on.
            tail = entry.tail if entry is not None and entry.offset == offset else b""
        data = {"v": self.VERSION, "path": str(path), "size": st.st_size,
                "mtime_ns": st.st_mtime_ns, "ino": st.st_ino, "offset": offset,
                "lines": lines, "tail": tail[-256:].hex(),
                "responses": [[list(k), r] for k, r in responses.items()]}
        try:
            harness_usage.write_json_atomic(self.root / f"{self._name(path)}.json", data)
        except (OSError, TypeError, ValueError):
            pass

    def prune(self, paths: list[Path]) -> None:
        """Drop the entries of transcripts that are gone."""
        keep = {f"{self._name(p)}.json" for p in paths}
        try:
            for f in self.root.iterdir():
                if f.suffix == ".json" and f.name not in keep:
                    with contextlib.suppress(OSError):
                        f.unlink()
        except OSError:
            pass


_SESSIONS_LISTED = 20


def _response_usd(r: dict[str, Any],
                  fallback: llm_prices.ModelPrice | None = None) -> tuple[float, bool]:
    """(list-price USD, priced) for one response. Unpriced means the fallback rate.

    A record that carries its own `usd` (Cursor reports what each request cost
    at the model's rate) is taken at that figure."""
    if isinstance(r.get("usd"), (int, float)):
        return float(r["usd"]), True
    price = llm_prices.price_for(r["model"])
    rate = price or fallback or _fallback()
    if price is None and _openai_billed(r):
        # OpenAI bills no cache-write premium: a write is ordinary input.
        rate = dataclasses.replace(rate, cache_write_5m=None, cache_write_1h=None)
    usd = rate.cost(
        input_tokens=r["input"], output_tokens=r["output"],
        cache_write_5m_tokens=r["cache_write"] - r["cache_write_1h"],
        cache_write_1h_tokens=r["cache_write_1h"], cache_read_tokens=r["cache_read"],
        fast=r["fast"], us_only=r["us_only"])
    return usd, price is not None


def _openai_billed(r: dict[str, Any]) -> bool:
    """Whether an unpriced response is an OpenAI model's: by its name, or a
    Codex response whose model name says nothing either way."""
    provider = llm_prices.provider_of(r["model"])
    if provider is not None:
        return provider == "openai"
    return r.get("harness") == harness_usage.HARNESS_CODEX


def _tally(responses: list[dict[str, Any]], source_present: bool,
           since_epoch: float | None = None) -> dict[str, Any]:
    tin = tout = cwrite = cread = 0
    usd_total = 0.0
    by_model: dict[str, int] = {}
    usd_by_model: dict[str, float] = {}
    usd_by_harness: dict[str, float] = {}
    tokens_by_harness: dict[str, int] = {}
    unpriced_usd = 0.0
    fallback = _fallback()
    unpriced: dict[str, int] = {}
    sessions: dict[str, dict[str, Any]] = {}
    first_ts: float | None = None
    last_ts: float | None = None
    for r in responses:
        ti, to, cw, cr = r["input"], r["output"], r["cache_write"], r["cache_read"]
        tin += ti; tout += to; cwrite += cw; cread += cr
        model = r["model"]
        by_model[model] = by_model.get(model, 0) + ti + to + cw + cr
        usd, priced = _response_usd(r, fallback)
        usd_total += usd
        usd_by_model[model] = usd_by_model.get(model, 0.0) + usd
        harness = r.get("harness") or harness_usage.HARNESS_CLAUDE
        usd_by_harness[harness] = usd_by_harness.get(harness, 0.0) + usd
        tokens_by_harness[harness] = tokens_by_harness.get(harness, 0) + ti + to + cw
        if not priced:
            unpriced[model] = unpriced.get(model, 0) + ti + to + cw
            unpriced_usd += usd
        ts = r["ts"]
        first_ts = ts if first_ts is None else min(first_ts, ts)
        last_ts = ts if last_ts is None else max(last_ts, ts)
        sess = sessions.setdefault(r["session"], {
            "usd_equivalent": 0.0, "billable_tokens": 0, "messages": 0,
            "first_activity": ts, "last_activity": ts, "project": None,
            "harness": harness})
        sess["usd_equivalent"] += usd
        sess["billable_tokens"] += ti + to + cw
        sess["messages"] += 1
        sess["last_activity"] = max(sess["last_activity"], ts)
        # Named for where the session started: a subagent's worktree later on
        # is still the same task.
        if r.get("cwd") and (sess["project"] is None or ts < sess["first_activity"]):
            sess["project"] = Path(r["cwd"]).name
        sess["first_activity"] = min(sess["first_activity"], ts)

    # "Billable" = the tokens that represent real new work and cost: input, output,
    # and cache creation. cache_read is Claude Code re-reading its own cached context
    # every turn; it is cheap and would otherwise dwarf every other number, so it is
    # reported separately and NOT the headline the budget measures against.
    billable = tin + tout + cwrite
    out = {
        "input_tokens": tin, "output_tokens": tout,
        "cache_creation_tokens": cwrite, "cache_read_tokens": cread,
        "billable_tokens": billable, "total_tokens": billable + cread,
        "messages": len(responses),
        "usd_equivalent": round(usd_total, 2),
        "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1])),
        "cost_by_model": {m: round(v, 2) for m, v in
                          sorted(usd_by_model.items(), key=lambda kv: -kv[1])},
        # Billable tokens from models llm_prices has no confirmed rate for. Their
        # dollars are in usd_equivalent at the fallback rate, and named here so a
        # figure built on a guessed rate is never presented as a list price.
        "unpriced_models": dict(sorted(unpriced.items(), key=lambda kv: -kv[1])),
        "unpriced_usd": round(unpriced_usd, 2),
        # The costliest sessions, each one task's worth of agent work. Capped so a
        # month of sessions cannot swamp an MCP response; session_count is all.
        "by_session": {sid: {**v, "usd_equivalent": round(v["usd_equivalent"], 2)}
                       for sid, v in sorted(sessions.items(),
                                            key=lambda kv: -kv[1]["usd_equivalent"])
                       [:_SESSIONS_LISTED]},
        "session_count": len(sessions),
        # Which agent the dollars went to. Harnesses are keyed as
        # harness_usage names them: claude-code, codex, cursor.
        "cost_by_harness": {h: round(v, 2) for h, v in
                            sorted(usd_by_harness.items(), key=lambda kv: -kv[1])},
        "billable_tokens_by_harness": dict(sorted(tokens_by_harness.items(),
                                                  key=lambda kv: -kv[1])),
        "prices_as_of": llm_prices.AS_OF,
        "first_activity": first_ts, "last_activity": last_ts,
        # True when any harness's usage source exists on this machine (Claude
        # Code transcripts, Codex rollouts) or is connected (Cursor).
        "source_present": (source_present or harness_usage.codex_present()
                           or harness_usage.cursor_enabled()),
        "sources": _sources(source_present),
    }
    notes = _source_notes(out["sources"])
    if notes:
        # Said wherever the figure is: a source that was not read or was cut
        # short makes the total a lower bound, not the whole of it.
        out["source_notes"] = notes
    if since_epoch is not None:
        skipped = harness_usage.codex_compressed_skipped(since_epoch)
        if skipped:
            out["codex_compressed_rollouts_skipped"] = skipped
    if unpriced:
        fb = fallback
        out["unpriced_note"] = (
            f"{', '.join(unpriced)} priced at the fallback ${fb.input:g}/${fb.output:g} "
            f"per 1M in/out, ${fb.cache_write_5m:g}/${fb.cache_write_1h:g} per 1M cache "
            f"writes (5m/1h; an OpenAI model's cache writes at the input rate, as "
            f"OpenAI bills them) and ${fb.cache_read:g} per 1M cache reads; set "
            f"FINOPS_AI_USD_PER_MTOK_IN/OUT to the rate you pay. The cache rates follow "
            f"the input rate unless FINOPS_AI_USD_PER_MTOK_CACHE_WRITE, "
            f"FINOPS_AI_USD_PER_MTOK_CACHE_WRITE_1H or FINOPS_AI_USD_PER_MTOK_CACHE_READ "
            f"is set.")
    return out


def _sources(claude: bool) -> dict[str, Any]:
    cursor: Any = False
    if harness_usage.cursor_enabled():
        cursor = {"scope": harness_usage.cursor_email() or "team",
                  **harness_usage.cursor_status()}
    return {harness_usage.HARNESS_CLAUDE: claude,
            harness_usage.HARNESS_CODEX: harness_usage.codex_present(),
            harness_usage.HARNESS_CURSOR: cursor}


def _source_notes(sources: dict[str, Any]) -> list[str]:
    cursor = sources.get(harness_usage.HARNESS_CURSOR)
    if not isinstance(cursor, dict):
        return []
    if cursor.get("not_read"):
        return [(f"Cursor usage was not read ({cursor['not_read']}), so it is not in "
                 f"these figures.")]
    if cursor.get("truncated"):
        cap = harness_usage._CURSOR_MAX_PAGES * harness_usage._CURSOR_PAGE
        return [(f"The Cursor figure is a lower bound: the Admin API had more usage "
                 f"events than the {cap:,} a read takes.")]
    return []


def _rec_epoch(ts: Any) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _month_start_epoch() -> float:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


# ── The current session ──────────────────────────────────────────────────────

# Passed as session_id by a caller that knows there is no session it can
# measure: a Cursor hook without the Admin API that holds Cursor's usage, or a
# hook payload with no session id. Any other value would be resolved, and the
# fallback, the latest transcript, may be another agent's session entirely.
# Not a valid session id (see _SAFE_SESSION_ID), so it can never name one.
NO_SESSION = "<no-session>"


def resolve_session(session_id: str | None = None) -> tuple[str | None, str | None]:
    """(session id, where it came from) for "this session".

    In order: an id the caller passed (the guard hook has it in its payload),
    CLAUDE_CODE_SESSION_ID (Claude Code sets it for the processes it starts),
    CODEX_SESSION_ID (Codex CLI sets it for the shell commands it runs), then
    the session whose Claude transcript or Codex rollout was written last,
    which is the one calling when only one agent is running. The source is
    returned so a guess is never reported as a fact. NO_SESSION resolves to
    no session at all.
    """
    if session_id == NO_SESSION:
        return None, None
    if session_id:
        return str(session_id), "argument"
    env = os.getenv("CLAUDE_CODE_SESSION_ID", "").strip()
    if env:
        return env, "env"
    env = os.getenv("CODEX_SESSION_ID", "").strip()
    if env:
        return env, "env"
    proj = _claude_projects_dir()
    latest: tuple[float, str] | None = None
    try:
        for path in proj.rglob("*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if latest is None or mtime > latest[0]:
                latest = (mtime, _path_session(path))
    except OSError:
        pass
    codex = None
    with contextlib.suppress(*_READER_ERRORS):
        codex = harness_usage.codex_latest_session()
    if codex and (latest is None or codex[0] > latest[0]):
        latest = codex
    if latest is None:
        return None, None
    return latest[1], "latest_activity"


def _session_lens(budget: dict[str, Any], session_id: str | None, *,
                  allow_network: bool = True) -> dict[str, Any] | None:
    sid, source = resolve_session(session_id)
    if not sid:
        return None
    u = read_session_usage(sid, allow_network=allow_network)
    cap = budget["session_caps"].get(sid) or budget["session_cap"] or 0.0
    usd = u["usd_equivalent"]
    pct = usd / cap if cap > 0 else None
    return {
        "id": sid, "id_source": source,
        "usd_equivalent": usd, "billable_tokens": u["billable_tokens"],
        "messages": u["messages"],
        "cost_by_model": u["cost_by_model"], "unpriced_models": u["unpriced_models"],
        "cost_by_harness": u["cost_by_harness"], "unpriced_usd": u["unpriced_usd"],
        "first_activity": u["first_activity"], "last_activity": u["last_activity"],
        "source_notes": u.get("source_notes") or [],
        "cap_usd": cap or None,
        "cap_scope": ("this_session" if sid in budget["session_caps"]
                      else "every_session" if cap else None),
        "pct_of_cap": round(pct, 3) if pct is not None else None,
        "remaining_usd": round(max(0.0, cap - usd), 2) if cap > 0 else None,
        "verdict": _verdict(pct) if pct is not None else None,
    }


# Said wherever "this session" is really the latest transcript's session.
GUESSED_SESSION_NOTE = "session guessed from the most recently active transcript"


def _guessed(session: dict[str, Any] | None) -> bool:
    return bool(session) and session.get("id_source") == "latest_activity"


def _verdict(pct: float) -> str:
    return BUDGET_OVER if pct >= 1.0 else (BUDGET_WARN if pct >= _WARN_AT else BUDGET_OK)


_RANK = {BUDGET_OK: 0, BUDGET_WARN: 1, BUDGET_OVER: 2}


# ── Status + gate ────────────────────────────────────────────────────────────

def _called_by_guard() -> bool:
    """Whether status() was called by guard.check_budget_gate.

    The guard runs on every tool call and reads only the verdict, so it should
    not pay for figures it throws away. guard.py cannot pass for_gate itself
    yet (it is owned elsewhere), so the caller's module says which it is.
    """
    try:
        return sys._getframe(2).f_globals.get("__name__") == "finops.guard"
    except ValueError:
        return False


def status(session_id: str | None = None, *, for_gate: bool | None = None) -> dict[str, Any]:
    """Where you stand. Honest by construction: tokens and burn rate are exact
    (read from local logs); dollars are ONLY ever an estimate at list price, never
    your real bill. Two lenses:
      - metered (pay-per-token / enterprise): a dollar spend budget, gate on it.
      - flat (subscription): budget your USAGE so you do not run out, and see how
        much subsidized compute you are pulling for your fixed fee.
    Either can add a per-session cap, which gates this session's own spend; the
    verdict is the worse of the two. `session_id` names the session (see
    resolve_session for what "this session" means without one).

    for_gate: only the verdict is wanted (the guard, on every tool call), so
    nothing is read that cannot change it: no transcripts at all when no cap is
    set, the month only under a monthly cap, the session only under a session
    cap, never the window. None means "the guard called", detected."""
    if for_gate is None:
        for_gate = _called_by_guard()
    budget = get_budget()
    mode = budget["mode"]
    monthly_cap = (mode == "metered" and budget["spend_cap"] > 0) or budget["monthly_tokens"] > 0
    session_cap = budget["session_cap"] > 0 or bool(budget["session_caps"])
    if for_gate and not monthly_cap and not session_cap:
        # Nothing is capped, so nothing can be over: the verdict needs no reading.
        return {"verdict": BUDGET_OK, "verdict_basis": "none", "mode": mode,
                "budget": budget, "pct_of_budget": None, "month_verdict": BUDGET_OK,
                "month_verdict_basis": "none", "month_pct_of_budget": None,
                "session": None, "gate_only": True}
    now = time.time()
    empty = _tally([], source_present=False)
    # The guard never waits on the network: Cursor comes from its cache there.
    net = not for_gate
    window = empty if for_gate else read_agent_usage(now - _WINDOW_HOURS * 3600)
    mtd = (empty if for_gate and not monthly_cap
           else read_agent_usage(_month_start_epoch(), allow_network=net))

    tokens_mtd = mtd["billable_tokens"]        # exact
    est_usd_mtd = mtd["usd_equivalent"]        # ESTIMATE at list price, not a bill

    mtok = tokens_mtd / 1e6 if tokens_mtd else 0.0
    cost_per_1m_list = round(est_usd_mtd / mtok, 2) if mtok else None
    cost_per_1m_effective = (round(budget["plan_cost"] / mtok, 2)
                             if mode == "flat" and budget["plan_cost"] > 0 and mtok else None)

    # Verdict off the honest dial for the lens: metered → the dollar spend cap
    # (labeled estimate until a real Cost API is wired); either mode → a usage cap.
    verdict, pct, basis = BUDGET_OK, None, "none"
    if mode == "metered" and budget["spend_cap"] > 0:
        pct, basis = est_usd_mtd / budget["spend_cap"], "spend"
    elif budget["monthly_tokens"] > 0:
        pct, basis = tokens_mtd / budget["monthly_tokens"], "tokens"
    if pct is not None:
        verdict = _verdict(pct)
    # The month's own standing, kept apart from the session's: the overall
    # verdict below may be the session's, and a row about the month must not
    # show that.
    month_verdict, month_pct, month_basis = verdict, pct, basis

    # The per-session cap can only make the verdict worse. On a tie the one
    # further past its line is the one to name.
    session = (None if for_gate and not session_cap
               else _session_lens(budget, session_id, allow_network=net))
    if session and session["verdict"] is not None:
        s_rank, m_rank = _RANK[session["verdict"]], _RANK[verdict]
        if basis == "none" or s_rank > m_rank or (
                s_rank == m_rank and s_rank > 0 and session["pct_of_cap"] > (pct or 0)):
            verdict, pct, basis = session["verdict"], session["pct_of_cap"], "session"

    headroom = {
        "session_usd": session["remaining_usd"] if session else None,
        "month_usd": (round(max(0.0, budget["spend_cap"] - est_usd_mtd), 2)
                      if mode == "metered" and budget["spend_cap"] > 0 else None),
        "month_tokens": (max(0, budget["monthly_tokens"] - tokens_mtd)
                         if budget["monthly_tokens"] > 0 else None),
    }

    burn = round(window["billable_tokens"] / max(_WINDOW_HOURS, 0.1))

    subsidy = None
    if mode == "flat" and budget["plan_cost"] > 0:
        subsidy = {
            "plan_cost_usd": budget["plan_cost"],
            "compute_value_est_usd": est_usd_mtd,
            "multiple": round(est_usd_mtd / budget["plan_cost"], 1) if budget["plan_cost"] else None,
        }

    return {
        "verdict": verdict,
        "verdict_basis": basis,
        "mode": mode,
        "window_hours": _WINDOW_HOURS,
        "window": window,
        "month_to_date": mtd,
        "billable_tokens_mtd": tokens_mtd,
        "est_usd_mtd_list_price": est_usd_mtd,
        "budget": budget,
        "plan_label": budget["plan_label"],
        "pct_of_budget": round(pct, 3) if pct is not None else None,
        "month_verdict": month_verdict,
        "month_verdict_basis": month_basis,
        "month_pct_of_budget": round(month_pct, 3) if month_pct is not None else None,
        "session": session,
        "headroom": headroom,
        "burn_tokens_per_hour": burn,
        "subsidy": subsidy,
        "cost_per_1m_list": cost_per_1m_list,
        "cost_per_1m_effective": cost_per_1m_effective,
        "summary": _with_fallback_note(
            _summary_line(verdict, basis, mode, tokens_mtd, est_usd_mtd, budget,
                          subsidy, window, cost_per_1m_effective, session),
            {"session": session, "month": mtd, "window": window}),
        # Set when `session` is a guess, not the caller's own session.
        "session_note": (f"{GUESSED_SESSION_NOTE}; pass session_id to name yours"
                         if _guessed(session) else None),
        # The figures a gate-only status skipped reading are zeros, not usage.
        "gate_only": for_gate,
    }


# How the summary tells someone with no budget to set one. The CLI swaps it for
# the flags when it is not talking to a terminal (see cli_ai_budget).
SET_BUDGET_HINT = "Run `nable ai-budget` to set a budget."


def _with_fallback_note(line: tuple[str, str], lenses: dict[str, Any]) -> str:
    """The summary, plus how much of the dollars it quotes rest on a fallback
    rate, whenever any do. The lens is the one the line is about."""
    text, lens = line
    usd = (lenses.get(lens) or {}).get("unpriced_usd") or 0.0
    if usd > 0:
        text += f" This includes ~${usd:,.2f} priced at a fallback rate (see unpriced_models)."
    for note in (lenses.get(lens) or {}).get("source_notes") or []:
        text += f" {note}"
    return text


def _summary_line(verdict, basis, mode, tokens_mtd, est_usd, budget, subsidy, window,
                  eff_per_1m, session=None) -> tuple[str, str]:
    """(summary, the lens it is about: "session", "month" or "window")."""
    tag = {BUDGET_OK: "on track", BUDGET_WARN: "approaching your budget",
           BUDGET_OVER: "over budget"}[verdict]
    if basis == "session":
        if _guessed(session):
            return (f"~${session['usd_equivalent']:,.2f} of the latest session's "
                    f"${session['cap_usd']:,.2f} cap used (estimated at list price; "
                    f"{GUESSED_SESSION_NOTE}), {tag}."), "session"
        return (f"~${session['usd_equivalent']:,.2f} of this session's "
                f"${session['cap_usd']:,.2f} cap used (estimated at list price), "
                f"{tag}."), "session"
    return _month_summary(tag, basis, mode, tokens_mtd, est_usd, budget, subsidy, window,
                          eff_per_1m)


def _month_summary(tag, basis, mode, tokens_mtd, est_usd, budget, subsidy, window,
                   eff_per_1m) -> tuple[str, str]:
    if basis == "spend":
        return (f"~${est_usd:,.0f} estimated at list price of your "
                f"${budget['spend_cap']:,.0f} spend cap, {tag}."), "month"
    if basis == "tokens":
        return (f"{tokens_mtd:,} of {budget['monthly_tokens']:,} tokens this month, "
                f"{tag}."), "month"
    if subsidy and subsidy["multiple"]:
        extra = f" ~${eff_per_1m:g}/1M effective." if eff_per_1m else ""
        return (f"You pay ${subsidy['plan_cost_usd']:,.0f}/mo and have pulled "
                f"~${est_usd:,.0f} of compute (estimated at list price), "
                f"~{subsidy['multiple']:g}x your plan.{extra} The provider covers the "
                f"rest."), "month"
    # A budget IS configured but there is no usage to measure against yet. Confirm
    # it, never tell someone to set a budget they just set (found dogfooding).
    if mode == "flat" and budget["plan_cost"] > 0:
        return (f"Your ${budget['plan_cost']:,.0f}/mo plan is set. No agent usage recorded yet "
                f"this month; the numbers fill in as your agent runs."), "month"
    if mode == "metered" and budget["spend_cap"] > 0:
        return (f"Your ${budget['spend_cap']:,.0f}/mo spend cap is set. No agent usage recorded "
                f"yet this month."), "month"
    if budget["monthly_tokens"] > 0:
        return (f"Usage cap of {budget['monthly_tokens']:,} tokens/mo is set. No agent usage "
                f"recorded yet this month."), "month"
    return (f"{window['billable_tokens']:,} tokens in the last {_WINDOW_HOURS:g}h "
            f"(~${window['usd_equivalent']:,.0f} at list price). "
            f"{SET_BUDGET_HINT}"), "window"


def check(estimated_next_tokens: int = 0, session_id: str | None = None) -> dict[str, Any]:
    """The gate the agent calls before a big task. Advice only, never blocks.

    Metered plan: about dollars against your spend cap (estimated). Flat plan: about
    usage and not getting rate-limited, spoken in tokens and burn rate, never a fake
    dollar overage. Mirrors policy.py: a verdict and a reason, the human decides."""
    st = status(session_id=session_id)
    verdict, reason = st["verdict"], st["summary"]

    # A token/usage budget is exact, so a next-task estimate can honestly tip it.
    if estimated_next_tokens and st["budget"]["monthly_tokens"] > 0:
        after = st["billable_tokens_mtd"] + estimated_next_tokens
        pct_after = after / st["budget"]["monthly_tokens"]
        if pct_after >= 1.0 and verdict != BUDGET_OVER:
            verdict = BUDGET_WARN
            reason = (f"this task (~{estimated_next_tokens:,} tokens) would push you to "
                      f"{pct_after * 100:.0f}% of your monthly token budget.")

    if st["verdict_basis"] == "session":
        s = st["session"]
        left, cap = s["remaining_usd"], s["cap_usd"]
        whose, who = ("the latest session's", "The latest session") if _guessed(s) else (
            "this session's", "This session")
        rec = {
            BUDGET_OK: f"Proceed. ~${left:,.2f} of {whose} ${cap:,.2f} cap left.",
            BUDGET_WARN: (f"Proceed with a tight scope: ~${left:,.2f} of {whose} "
                          f"${cap:,.2f} cap left."),
            BUDGET_OVER: (f"{who} is past its ${cap:,.2f} cap. Confirm with the "
                          f"human before continuing."),
        }[verdict]
        if _guessed(s):
            rec += (f" ({GUESSED_SESSION_NOTE}; pass session_id so the cap is measured "
                    f"against your own session.)")
    elif st["mode"] == "metered":
        rec = {
            BUDGET_OK: "Proceed.",
            BUDGET_WARN: "Proceed, but you are close to your spend cap. Consider a tighter scope.",
            BUDGET_OVER: "You are over your AI spend cap. Confirm with the human before continuing.",
        }[verdict]
    else:
        rec = {
            BUDGET_OK: f"Proceed. Flat plan, so this is about pace, not a bill: ~{st['burn_tokens_per_hour']:,} tok/hr.",
            BUDGET_WARN: "Proceed, but you are near the usage budget you set for the month.",
            BUDGET_OVER: "You are past the usage budget you set. Confirm with the human first.",
        }[verdict]

    return {
        "verdict": verdict,
        "reason": reason,
        "recommendation": rec,
        "advice_only": True,
        "mode": st["mode"],
        "billable_tokens_mtd": st["billable_tokens_mtd"],
        "est_usd_mtd_list_price": st["est_usd_mtd_list_price"],
        "burn_tokens_per_hour": st["burn_tokens_per_hour"],
        "verdict_basis": st["verdict_basis"],
        "session": st["session"],
        "headroom": st["headroom"],
    }
