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
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from . import cloud_standing, harness_usage, llm_prices, token_budget

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
    # By guard_paths' rule (_datadir): a settings.json env entry gives "~/x"
    # unexpanded, and the guard protects the home directory's ~/x.
    from ._datadir import env_data_dir
    d = env_data_dir(Path.home() / ".nable")[0]
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


def _tail_in_place(path: Path, offset: int, tail: bytes) -> bool:
    """The last line a cached parse read still sits where it ended. A cheap
    guard against a file rewritten in place to the same size within the
    filesystem's timestamp resolution."""
    if not tail:
        return True
    try:
        with path.open("rb") as fh:
            fh.seek(offset - len(tail))
            return fh.read(len(tail)) == tail
    except (OSError, ValueError):
        return False


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
        return _tail_in_place(path, self.offset, self.tail)

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
    # Said when the guard found the cache old or missing and a read is under way.
    refreshing = "; refreshing in the background" if cursor.get("refreshing") else ""
    if cursor.get("not_read"):
        return [(f"Cursor usage was not read ({cursor['not_read']}), so it is not in "
                 f"these figures{refreshing}.")]
    notes = []
    if cursor.get("stale"):
        fetched = cursor.get("fetched_at")
        from .budget.summary import age_words
        age = (f"{age_words(max(time.time() - fetched, 0) / 3600)} old"
               if isinstance(fetched, (int, float)) else "past its hour")
        notes.append(f"The Cursor figure is from an Admin API read {age}{refreshing}.")
    if cursor.get("truncated"):
        cap = harness_usage._CURSOR_MAX_PAGES * harness_usage._CURSOR_PAGE
        notes.append(f"The Cursor figure is a lower bound: the Admin API had more usage "
                     f"events than the {cap:,} a read takes.")
    return notes


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


# ── Org model budgets (nable.org/budgets.yaml) ───────────────────────────────
#
# A team's, a person's or the org's ai_budget fact (finops.org.model) is
# compared with what this machine's coding agents spent this month (UTC), at
# list price: only tokens of a model llm_prices has a cited row for are
# dollars; the rest are counted and said, never priced at a guess. It is this
# machine alone, so a team's or the org's figure can only be under-counted.
# Whose budget: FINOPS_GUARD_PERSON, else git's user.email for the agent's
# directory, read once and kept beside the ledger until git's config changes.

PERSON_ENV = "FINOPS_GUARD_PERSON"
PERSON_CACHE = "guard-person.json"
_PERSON_KEPT = 50
_AGENT_NAMES = {harness_usage.HARNESS_CLAUDE: "Claude Code",
                harness_usage.HARNESS_CODEX: "Codex",
                harness_usage.HARNESS_CURSOR: "Cursor"}


def _read_git_email(cwd: str | None) -> str | None:
    """`git config --get user.email` run in `cwd`, or None. The one place a
    process is started for the identity; the cache keeps it off every call."""
    import shutil
    import subprocess
    git = shutil.which("git")
    if not git:
        return None
    try:
        r = subprocess.run([git, "config", "--get", "user.email"],  # nosec B603
                           cwd=cwd if cwd and os.path.isdir(cwd) else None,
                           capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    email = (r.stdout or "").strip()
    return email or None


def _stamp(p: Path) -> list[Any]:
    try:
        st = p.stat()
    except OSError:
        return [str(p), None]
    return [str(p), st.st_mtime_ns, st.st_size, st.st_ino]


def _git_config_files(root: Path | None) -> list[Path]:
    """guard_paths.git_config_files: the files git reads user.email from,
    each one of the guard's own files."""
    from . import guard_paths
    return guard_paths.git_config_files(root)


def _person_cache_path() -> Path:
    from . import guard_ledger
    return guard_ledger.ledger_path().with_name(PERSON_CACHE)


def _month_key() -> str:
    """This UTC month, as guard-person.json keeps it ("2026-10")."""
    return time.strftime("%Y-%m", time.gmtime(_month_start_epoch()))


def _read_person_cache() -> tuple[Path, dict[str, Any]]:
    path = _person_cache_path()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        entries = doc.get("entries") if isinstance(doc, dict) and doc.get("v") == 1 else None
    except (OSError, ValueError):
        entries = None
    return path, entries if isinstance(entries, dict) else {}


def _write_person_cache(path: Path, entries: dict[str, Any]) -> None:
    kept = dict(list(entries.items())[-_PERSON_KEPT:])
    with contextlib.suppress(OSError, TypeError, ValueError):
        path.parent.mkdir(parents=True, exist_ok=True)
        harness_usage.write_json_atomic(path, {"v": 1, "entries": kept})


def _root_key(cwd: str | None) -> tuple[Path | None, str]:
    from . import guard_paths
    root = guard_paths.git_root(cwd)
    return root, str(root) if root is not None else ""


def _lower(x: Any) -> str:
    return x.strip().lower() if isinstance(x, str) else ""


def _cached_git_email(cwd: str | None) -> str | None:
    """git's user.email for `cwd`, from guard-person.json while the config
    files it came from are as they were, else read again (and kept).

    When a read again finds another email than the one kept, the one kept is
    remembered as `earlier` for the rest of the UTC month (this machine's
    month holds what was spent under it, so its budgets still apply), and
    `moved` is set until the guard has asked about it once (identity_moved).
    That is the backstop for a change no check saw coming: a file an include
    names, a script."""
    root, where = _root_key(cwd)
    stamp = [_stamp(p) for p in _git_config_files(root)]
    path, entries = _read_person_cache()
    hit = entries.get(where)
    hit = hit if isinstance(hit, dict) else None
    if hit is not None and hit.get("stamp") == stamp:
        email = hit.get("email")
        return email if isinstance(email, str) and email else None
    email = _read_git_email(str(root) if root is not None else cwd)
    month = _month_key()
    entry: dict[str, Any] = {"stamp": stamp, "email": email, "month": month}
    if hit is not None:
        same_month = hit.get("month", month) == month
        earlier = [e for e in (hit.get("earlier") or []) if isinstance(e, str)] \
            if same_month else []
        before, now = _lower(hit.get("email")), _lower(email)
        moved = hit.get("moved") if same_month and isinstance(hit.get("moved"), dict) else None
        if before and before != now:
            if before not in earlier:
                earlier.append(before)
            moved = {"from": before, "to": now or None}
        earlier = [e for e in earlier if e != now]
        if earlier:
            entry["earlier"] = earlier
        if moved is not None and _lower(moved.get("from")) != now:
            entry["moved"] = moved
    entries.pop(where, None)
    entries[where] = entry
    _write_person_cache(path, entries)
    return email


def developer(cwd: str | None = None) -> tuple[str | None, str]:
    """(who this machine's agents spend for, where that came from):
    FINOPS_GUARD_PERSON, else git's user.email for the agent's directory,
    lowercased (a budget's person is matched without regard to case).
    (None, "") when neither says."""
    env = os.environ.get(PERSON_ENV, "").strip()
    if env:
        return env.lower(), PERSON_ENV
    email = _cached_git_email(cwd)
    return (email.strip().lower(), "git user.email") if email else (None, "")


def earlier_developers(cwd: str | None = None) -> list[str]:
    """The other emails git's user.email gave for `cwd`'s repo earlier this
    UTC month (guard-person.json), lowercased; [] under FINOPS_GUARD_PERSON,
    which names the person whatever git says. Their budgets still apply this
    month: this machine's month holds what was spent under them."""
    if os.environ.get(PERSON_ENV, "").strip():
        return []
    _root, where = _root_key(cwd)
    _path, entries = _read_person_cache()
    hit = entries.get(where)
    if not isinstance(hit, dict) or hit.get("month", _month_key()) != _month_key():
        return []
    return [e for e in (hit.get("earlier") or []) if isinstance(e, str) and e]


def identity_moved(cwd: str | None = None) -> dict[str, Any] | None:
    """{"from", "to"} once after git's user.email for `cwd`'s repo changed
    (to None when it was removed), then None: the guard asks about it once.
    None under FINOPS_GUARD_PERSON."""
    if os.environ.get(PERSON_ENV, "").strip():
        return None
    _root, where = _root_key(cwd)
    path, entries = _read_person_cache()
    hit = entries.get(where)
    if not isinstance(hit, dict) or not isinstance(hit.get("moved"), dict):
        return None
    moved = hit.pop("moved")
    _write_person_cache(path, entries)
    return {"from": _lower(moved.get("from")) or None, "to": _lower(moved.get("to")) or None}


def budgets_for(model: Any, cwd: str | None = None) -> tuple[str | None, str, list[dict]]:
    """(person, where that came from, the org model's AI budgets that apply):
    the person's, and those of each identity git gave for the repo earlier
    this month (each hit's "person" says whose), each budget once."""
    person, source = developer(cwd)
    seen: set[tuple[str, str]] = set()
    hits: list[dict[str, Any]] = []
    for who in [person, *[e for e in earlier_developers(cwd) if e != person]]:
        for h in model.ai_budgets_for(who, strict=True):
            if (h["scope"], h["subject"]) in seen:
                continue
            seen.add((h["scope"], h["subject"]))
            hits.append({**h, "person": who})
    return person, source, hits


def list_price_usd(r: dict[str, Any]) -> Decimal | None:
    """One response at its model's cited list price (llm_prices), exact, or
    None when llm_prices has no row for the model. A figure the harness
    reported itself (Cursor's) is not used: every dollar here has one source."""
    price = llm_prices.price_for(r["model"])
    if price is None:
        return None
    return price.cost_exact(input_tokens=int(r["input"]), output_tokens=int(r["output"]),
                            cache_write_5m_tokens=int(r["cache_write"]) - int(r["cache_write_1h"]),
                            cache_write_1h_tokens=int(r["cache_write_1h"]),
                            cache_read_tokens=int(r["cache_read"]), fast=bool(r.get("fast")),
                            us_only=bool(r.get("us_only")))


def _tokens(r: dict[str, Any]) -> int:
    return int(r["input"]) + int(r["output"]) + int(r["cache_write"]) + int(r["cache_read"])


def cursor_read_since() -> float | None:
    """Where agent_responses' Cursor read starts (a day before this UTC
    month), or None when it reads no Cursor usage."""
    if not (harness_usage.cursor_enabled() and harness_usage.cursor_email()):
        return None
    return _month_start_epoch() - harness_usage.CURSOR_READ_BEFORE_S


def agent_responses(since_epoch: float, *, allow_network: bool = False
                    ) -> tuple[list[dict[str, Any]], list[str]]:
    """(this machine's coding-agent responses since `since_epoch`, notes on
    what is left out): Claude Code's transcripts, Codex CLI's rollouts and,
    when its Admin API read is scoped to one person (CURSOR_ADMIN_USER_EMAIL),
    Cursor's. A team-wide Cursor read is somebody else's usage too, so it is
    left out and said. Raises when a reader fails: a figure missing a source
    it should have read is not this machine's spend."""
    proj = _claude_projects_dir()
    out = _responses(proj, since_epoch) if proj.is_dir() else []
    others, notes = _other_responses(since_epoch, allow_network=allow_network)
    return out + others, notes


def _other_responses(since_epoch: float, *, allow_network: bool = False
                     ) -> tuple[list[dict[str, Any]], list[str]]:
    """agent_responses without Claude Code's transcripts: Codex CLI's
    rollouts, and Cursor's when its read is scoped to one person."""
    notes: list[str] = []
    out = list(harness_usage.codex_responses(since_epoch))
    if harness_usage.cursor_enabled():
        if harness_usage.cursor_email():
            out += harness_usage.cursor_responses(since_epoch, month_start=_month_start_epoch(),
                                                  allow_network=allow_network)
            notes += _source_notes(_sources(True))
        else:
            notes.append("Cursor's Admin API key reads the whole team's usage, so Cursor is "
                         "left out; set CURSOR_ADMIN_USER_EMAIL to count one person's")
    return out, notes


# ── The month, priced once (D109) ────────────────────────────────────────────
#
# The org model's budgets are checked at every shell and MCP call against the
# month to date. Pricing every response of the month on every call was most of
# what a heavy month cost the guard (40,000 responses: about 370 ms a call).
# The month cache prices each transcript's responses once. In the data dir,
# each 0600:
#
#   ai-budget-month/<hash>.json  one transcript's priced responses this UTC
#                                month, with its size, mtime and inode and
#                                where its last read stopped
#   ai-budget-month.json         per transcript: those stats, its total, and
#                                its responses' keys (hashed), to count a
#                                response two transcripts hold once
#   ai-budget-month.total.json   the month's total, the stats of every
#                                transcript it was made from, and the prices
#                                of Codex and Cursor responses by what they
#                                were priced on
#
# all made for one UTC month, its start and one llm_prices table
# (prices_version), and made again for any other. A call whose transcripts
# all have the size, mtime and inode the total was made from reads the last
# file alone and prices nothing. A transcript that only grew (Claude Code
# appends) has its new lines read and priced, and nothing else; any other
# change to it makes its entry again from the transcript. A response logged
# in more than one transcript (a resumed session copies the one it resumes)
# counts once: the larger copy, so a copy rewritten lower in either file does
# not lower the month. A transcript that cannot be read is left out of that
# call's figure and said (a note), and nothing about it is kept: no shard, no
# index entry and no total, so the next call reads it again. A failed read is
# never kept as $0.

MONTH_CACHE = "ai-budget-month.json"
MONTH_TOTAL = "ai-budget-month.total.json"
MONTH_SHARDS = "ai-budget-month"
_MONTH_CACHE_VERSION = 2


def prices_version() -> str:
    """A fingerprint of the llm_prices table every list price here comes
    from: a change to any rate, alias, fast-mode rate, multiplier or date
    makes another."""
    import hashlib
    blob = repr((sorted((k, dataclasses.astuple(v)) for k, v in llm_prices.MODEL_PRICES.items()),
                 sorted(llm_prices.ALIASES.items()), sorted(llm_prices.FAST_MODE.items()),
                 llm_prices.US_ONLY_MULTIPLIER, sorted(llm_prices.AS_OF_BY_PROVIDER.items())))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _priced(r: dict[str, Any]) -> list[Any]:
    """One response as the month cache keeps it: [ts, exact list-price USD as
    text or None when unpriced, its tokens, its model, its provider or None]."""
    got = list_price_usd(r)
    price = llm_prices.price_for(r["model"]) if got is not None else None
    return [r["ts"], None if got is None else str(got), _tokens(r), str(r["model"]),
            price.provider if price is not None else None]


def _copy_size(row: list[Any]) -> tuple[Decimal, int]:
    """How large one priced copy of a response is: its list price (an
    unpriced one below any priced one), then its tokens."""
    return (Decimal(row[1]) if row[1] is not None else Decimal(-1), int(row[2]))


def _key_hash(key: str) -> str:
    import hashlib
    return hashlib.sha1(key.encode("utf-8", "surrogateescape"),
                        usedforsecurity=False).hexdigest()[:16]


def _sums(rows: Any) -> dict[str, Any]:
    """{usd, unpriced, models, providers} over priced responses."""
    usd = Decimal(0)
    unpriced = 0
    models: set[str] = set()
    providers: set[str] = set()
    for _ts, got, tokens, model, provider in rows:
        if got is None:
            unpriced += int(tokens)
            models.add(str(model))
            continue
        usd += Decimal(got)
        if provider:
            providers.add(str(provider))
    return {"usd": str(usd), "unpriced": unpriced, "models": sorted(models),
            "providers": sorted(providers)}


class _MonthTally:
    """The month cache (see above), for one UTC month, its start and one
    price table."""

    def __init__(self, since: float, month: str, prices: str):
        self.since = since
        self.header = {"v": _MONTH_CACHE_VERSION, "month": month, "since": since,
                       "prices": prices}
        self.root = _data_dir()

    def _load(self, path: Path) -> dict[str, Any]:
        """The file's document when it is this month's cache, else {}."""
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(doc, dict) or any(doc.get(k) != v for k, v in self.header.items()):
            return {}
        return doc

    def _write(self, path: Path, doc: dict[str, Any]) -> None:
        with contextlib.suppress(OSError, TypeError, ValueError):
            path.parent.mkdir(mode=0o700, exist_ok=True)
            harness_usage.write_json_atomic(path, {**self.header, **doc})

    def _shard(self, path: str) -> Path:
        import hashlib
        name = hashlib.sha256(path.encode("utf-8", "surrogateescape")).hexdigest()[:40]
        return self.root / MONTH_SHARDS / f"{name}.json"

    def _stats(self, proj: Path) -> list[tuple[str, os.stat_result]]:
        out = []
        for p in sorted(str(x) for x in _transcripts(proj, None)):
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_mtime >= self.since - 1:   # an older file holds nothing of the month
                out.append((p, st))
        return out

    def _read_file(self, path: str, st: os.stat_result,
                   entry: dict[str, Any] | None) -> dict[str, Any]:
        """The transcript's shard: extended by its new lines, each priced,
        when it only grew since `entry`; else made again from the file.
        OSError when the file could not be read whole: the caller keeps
        nothing of it."""
        try:
            tail = bytes.fromhex(str(entry.get("tail") or "")) if entry else b""
            was = int(entry.get("offset") or 0) if entry else 0
        except (TypeError, ValueError):
            entry, tail, was = None, b"", 0
        responses: dict[str, Any] = {}
        offset = lines = 0
        if (entry and entry.get("path") == path and st.st_ino == entry.get("ino")
                and st.st_size >= was and isinstance(entry.get("r"), dict)
                and _tail_in_place(Path(path), was, tail)):
            responses = dict(entry["r"])
            offset, lines = was, int(entry.get("lines") or 0)
        last = b""
        with open(path, "rb") as fh:
            fh.seek(offset)
            for line in fh:
                if not line.endswith(b"\n"):
                    break          # a line still being written: read it next time
                hit = _parse_usage_line(line, Path(path), lines)
                if hit is not None and hit[1]["ts"] >= self.header["since"]:
                    responses[json.dumps(list(hit[0]))] = _priced(hit[1])
                lines += 1
                offset += len(line)
                last = line
        if not last and offset == was:
            last = tail                # nothing new was read: keep the line it ended on
        return {"path": path, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                "ino": st.st_ino, "offset": offset, "lines": lines, "tail": last[-256:].hex(),
                "r": responses}

    def total(self, proj: Path) -> dict[str, Any]:
        """{usd (exact, as text), unpriced_tokens, unpriced_models, providers,
        responses} for Claude Code's transcripts this month."""
        stats = self._stats(proj)
        stamp = [[p, st.st_size, st.st_mtime_ns, st.st_ino] for p, st in stats]
        kept = self._load(self.root / MONTH_TOTAL)
        if kept.get("stamp") == stamp and isinstance(kept.get("claude"), dict):
            return kept["claude"]
        index = self._load(self.root / MONTH_CACHE).get("files")
        index = index if isinstance(index, dict) else {}
        files: dict[str, Any] = {}
        not_read = 0
        for p, st in stats:
            f = index.get(p)
            if isinstance(f, dict) and f.get("stat") == [st.st_size, st.st_mtime_ns, st.st_ino]:
                files[p] = f
                continue
            shard = self._load(self._shard(p))
            try:
                entry = self._read_file(p, st, shard or None)
            except OSError:
                not_read += 1      # nothing of it is kept, so the next call reads it again
                continue
            self._write(self._shard(p), entry)
            rows = entry["r"]
            files[p] = {"stat": [st.st_size, st.st_mtime_ns, st.st_ino], **_sums(rows.values()),
                        "keys": [_key_hash(k) for k in rows], "shared": {}}
        claude = self._combine(files, [p for p, _st in stats if p in files])
        self._write(self.root / MONTH_CACHE, {"files": files})
        if set(index) - set(files):
            self._prune({self._shard(p).name for p, _st in stats})
        if not_read:
            # No total for stats a transcript was left out of: a later call
            # with the same stats would take it as the whole month.
            claude["not_read"] = not_read
            self._write(self.root / MONTH_TOTAL, {"stamp": None, "claude": None,
                                                   "other": kept.get("other") or {}})
            return claude
        self._write(self.root / MONTH_TOTAL, {"stamp": stamp, "claude": claude,
                                               "other": kept.get("other") or {}})
        return claude

    def _combine(self, files: dict[str, Any], order: list[str]) -> dict[str, Any]:
        """The month's total over `files`, a response two transcripts hold
        counted once: the larger copy (by list price, then tokens), whatever
        the paths, so a copy rewritten lower does not lower the month. Each
        copy's figures are read from its shard once and kept in the index
        (shared)."""
        holders: dict[str, list[str]] = {}
        for p in order:
            for h in files[p]["keys"]:
                holders.setdefault(h, []).append(p)
        usd = sum((Decimal(files[p]["usd"]) for p in order), Decimal(0))
        unpriced = sum(int(files[p]["unpriced"]) for p in order)
        models = {m for p in order for m in files[p]["models"]}
        providers = {x for p in order for x in files[p]["providers"]}
        dup = {h: ps for h, ps in holders.items() if len(ps) > 1}
        for p in dict.fromkeys(p for ps in dup.values() for p in ps):
            mine = [h for h in files[p]["keys"] if h in dup]
            shared = files[p].setdefault("shared", {})
            if any(h not in shared for h in mine):
                rows = self._load(self._shard(p)).get("r") or {}
                by_hash = {_key_hash(k): v for k, v in rows.items()}
                shared.update({h: by_hash[h] for h in mine if h in by_hash})
        for h, ps in dup.items():
            copies = [row for p in ps if (row := files[p]["shared"].get(h))]
            if not copies:
                continue
            kept = max(copies, key=_copy_size)
            for row in copies:
                if row is kept:
                    continue
                if row[1] is None:
                    unpriced -= int(row[2])
                else:
                    usd -= Decimal(row[1])
        return {"usd": str(usd), "unpriced_tokens": unpriced, "unpriced_models": sorted(models),
                "providers": sorted(providers), "responses": len(holders)}

    def _prune(self, keep: set[str]) -> None:
        """Drop the shards of transcripts that are gone."""
        with contextlib.suppress(OSError):
            for f in (self.root / MONTH_SHARDS).iterdir():
                if f.suffix == ".json" and f.name not in keep:
                    with contextlib.suppress(OSError):
                        f.unlink()

    def other_prices(self) -> dict[str, Any]:
        memo = self._load(self.root / MONTH_TOTAL).get("other")
        return memo if isinstance(memo, dict) else {}

    def keep_other_prices(self, memo: dict[str, Any]) -> None:
        kept = self._load(self.root / MONTH_TOTAL)
        self._write(self.root / MONTH_TOTAL,
                    {**{k: v for k, v in kept.items() if k not in self.header}, "other": memo})


def _other_key(r: dict[str, Any]) -> str:
    """What a Codex or Cursor response's list price depends on, as text."""
    return json.dumps([str(r["model"]), int(r["input"]), int(r["output"]),
                       int(r["cache_write"]), int(r["cache_write_1h"]), int(r["cache_read"]),
                       bool(r.get("fast")), bool(r.get("us_only"))])


def month_agent_usage(*, allow_network: bool = False) -> dict[str, Any]:
    """This machine's coding-agent spend this month (UTC), at list price:
    {usd, usd_cents, unpriced_tokens, unpriced_models, agents, providers,
    notes, month_start}. usd covers priced tokens only; usd_cents is it
    rounded up to the cent, as text. Claude Code's transcripts come from the
    month cache (_MonthTally); Codex's and Cursor's responses are read each
    time and priced once each, by what their price depends on."""
    since = _month_start_epoch()
    tally = _MonthTally(since, _month_key(), prices_version())
    proj = _claude_projects_dir()
    claude = tally.total(proj) if proj.is_dir() else {
        "usd": "0", "unpriced_tokens": 0, "unpriced_models": [], "providers": [],
        "responses": 0}
    others, notes = _other_responses(since, allow_network=allow_network)
    if claude.get("not_read"):
        n = claude["not_read"]
        what = "1 Claude Code transcript was" if n == 1 else f"{n} Claude Code transcripts were"
        notes.append(f"{what} not read on this call and {'is' if n == 1 else 'are'} not in the "
                     "figure, which can only under-count; the next call reads "
                     f"{'it' if n == 1 else 'them'} again")
    usd = Decimal(claude["usd"])
    unpriced = int(claude["unpriced_tokens"])
    unpriced_models: set[str] = set(claude["unpriced_models"])
    agents: set[str] = {harness_usage.HARNESS_CLAUDE} if claude["responses"] else set()
    providers: set[str] = set(claude["providers"])
    memo = tally.other_prices() if others else {}
    used: dict[str, Any] = {}
    for r in others:
        agents.add(r.get("harness") or harness_usage.HARNESS_CLAUDE)
        key = _other_key(r)
        hit = memo.get(key)
        if not (isinstance(hit, list) and len(hit) == 2):
            got = list_price_usd(r)
            price = llm_prices.price_for(r["model"]) if got is not None else None
            hit = [None if got is None else str(got), price.provider if price else None]
        used[key] = hit
        if hit[0] is None:
            unpriced += _tokens(r)
            unpriced_models.add(str(r["model"]))
            continue
        usd += Decimal(hit[0])
        if hit[1]:
            providers.add(str(hit[1]))
    if used != memo and (used or memo):
        tally.keep_other_prices(used)
    cents = usd.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return {"usd": float(usd), "usd_exact": str(usd), "usd_cents": format(cents, "f"),
            "unpriced_tokens": unpriced, "unpriced_models": sorted(unpriced_models),
            "agents": sorted(agents), "providers": sorted(providers), "notes": notes,
            "month_start": since}


def price_source(providers: list[str] | tuple[str, ...]) -> str:
    """Where the dollars' rates come from, for a reason a person reads."""
    names = {"anthropic": "Anthropic", "openai": "OpenAI"}
    parts = [f"{names.get(p, p)}'s as of {llm_prices.AS_OF_BY_PROVIDER.get(p, llm_prices.AS_OF)}"
             f" ({llm_prices.SOURCES.get(p, '')})" for p in providers] or [
        f"as of {llm_prices.AS_OF}"]
    return "list prices from llm_prices, " + ", ".join(parts)


def _org_dirs(cwd: str | None) -> list[Path]:
    """The org model dirs the guard reads from `cwd`, found without
    importing the org package (finops.org.store.resolve_dir's rule)."""
    from . import guard_paths
    env = os.environ.get("FINOPS_ORG_DIR", "").strip()
    if env:
        return [Path(env).expanduser()]
    out: list[Path] = []
    root = guard_paths.git_root(cwd)
    if root is not None:
        out.append(Path(root) / "nable.org")
    out.append(guard_paths.data_dir() / "org")
    return out


def has_org_budgets(cwd: str | None = None) -> bool:
    """Whether any org model dir the guard reads from `cwd` holds a
    budgets.yaml: a stat or three, so a call with none costs nothing more."""
    return any((d / "budgets.yaml").is_file() for d in _org_dirs(cwd))


_SCOPE_WORDS = {"person": "the monthly budget for {subject}",
                "per_person": "the per person budget of {subject}",
                "team": "the monthly budget of {subject}",
                "org": "the org's monthly budget ({subject})"}


def org_budget_status(cwd: str | None = None) -> dict[str, Any] | None:
    """The org model's AI budgets against this machine's month: None when
    no budgets.yaml is there or none applies, else {verdict, hit, usd, pct,
    person, person_source, usage, hits}, `hit` the budget furthest past its
    line. Raises when the org model or the usage cannot be read: the caller
    decides what that means (the guard: no opinion)."""
    if not has_org_budgets(cwd):
        return None
    from . import guard_org
    model = guard_org.load_model(cwd)
    person, source, hits = budgets_for(model, cwd)
    if not hits:
        return None
    usage = month_agent_usage(allow_network=False)
    machine = Decimal(str(usage.get("usd_exact") or repr(float(usage["usd"]))))
    box, box_why = box_standing(person)
    rows = []
    for h in hits:
        # The box's figure for this budget, when it is about this person
        # (cloud_standing): the larger of the two is compared, never the sum.
        theirs = (cloud_standing.figure(box, h["scope"], h["subject"])
                  if box is not None and h["person"] == box["person"] else None)
        compared = "box" if theirs is not None and theirs > machine else "machine"
        larger = theirs if compared == "box" else machine
        pct = float(larger) / h["usd"] if h["usd"] > 0 else 0.0
        cover = (cloud_standing.coverage(box, h["scope"], h["subject"])
                 if theirs is not None else None)
        # How current that figure is: its own as_of, else (a box that sends
        # none) the answer's.
        at, own = (cloud_standing.figure_as_of(box, h["scope"], h["subject"])
                   if theirs is not None else (None, False))
        age = (cloud_standing.figure_age_hours(box, h["scope"], h["subject"])
               if theirs is not None else None)
        rows.append({**h, "pct": pct, "verdict": _verdict(pct), "machine_usd": machine,
                     "box_usd": theirs, "compared": compared, "box_cover": cover,
                     "box_as_of": at, "box_as_of_own": own, "box_age_hours": age})
    worst = max(rows, key=lambda h: (_RANK[h["verdict"]], h["sure"], h["pct"]))
    if worst["scope"] in ("person", "per_person") and worst["person"] != person:
        person, source = worst["person"], (f"git user.email earlier this month; it is "
                                           f"{person or 'not set'} now")
    return {"verdict": worst["verdict"], "hit": worst, "usd": usage["usd"], "pct": worst["pct"],
            "person": person, "person_source": source, "usage": usage, "hits": rows,
            "box": _box_said(box, box_why)}


def box_standing(person: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """(the box's standing the gate may use, None), or (None, why not): read
    from cloud-standing.json alone, never the network (cloud_standing)."""
    try:
        return cloud_standing.for_gate(person, _month_key())
    except Exception as exc:  # noqa: BLE001 - a file not read is said, and stops nothing
        return None, f"{cloud_standing.FILE_NAME} was not read ({type(exc).__name__})"


def _box_said(box: dict[str, Any] | None, why: str | None) -> dict[str, Any]:
    """What a status says of the box's standing: used or not, and why not."""
    if box is None:
        return {"used": False, "why": why}
    return {"used": True, "why": None, "box": box["box"], "host": box["host"],
            "person": box["person"], "month": box["month"], "as_of": box["as_of"],
            "fetched_at": box["fetched_at"], "age_hours": cloud_standing.age_hours(box)}


def _cents(x: Decimal) -> str:
    return f"${x.quantize(Decimal('0.01'), rounding=ROUND_CEILING):,.2f}"


def _whose(h: dict[str, Any]) -> str:
    sid = str(h["subject"]).split(":", 1)[-1]
    return {"team": f"the team {sid}", "org": "the org"}.get(h["scope"], str(h["person"]))


def org_budget_words(st: dict[str, Any]) -> str:
    """The figure and the budget, as a reason says them: the file and fact
    key, the dollars and where their rates come from, and that they are
    this machine's alone."""
    h, u = st["hit"], st["usage"]
    what = _SCOPE_WORDS[h["scope"]].format(subject=h["subject"])
    agents = ", ".join(_AGENT_NAMES.get(a, a) for a in u["agents"]) or "no agent"
    budget = f"{what}, ${h['usd']:,.2f} ({h['file'] or 'budgets.yaml'}, fact {h['key']})"
    mine = f"${Decimal(u['usd_cents']):,.2f}"
    theirs = h.get("box_usd")
    if theirs is not None:
        return _box_words(st, h, u, budget=budget, agents=agents, mine=mine)
    text = (f"This machine's coding agents ({agents}) have spent {mine} in "
            f"list-price-equivalent dollars this month, {h['pct'] * 100:.0f}% of {budget}. "
            f"Priced at {price_source(u['providers'])}: usage, not an invoice, and "
            "this machine only: the person's other machines are not counted")
    if h["scope"] in ("team", "org"):
        text += ", nor anyone else's, so the figure can only under-count"
    return _unpriced_and_notes(text, u)


def _whose_records(h: dict[str, Any]) -> str:
    """Whose records a figure sums, as the reason names them."""
    if h["scope"] == "team":
        return "the team's records"
    if h["scope"] == "org":
        return "the org's records"
    return f"{h['person']}'s records"


def _cover_words(h: dict[str, Any]) -> str:
    """How current the box's figure for this budget is, from its own records:
    the box's as_of is the newest receipt from anyone in the workspace."""
    c = h.get("box_cover") or {}
    if h["scope"] == "team":
        return (f"; {c.get('people_sent', 0)} of the team's {c.get('people', 0)} people sent, "
                f"their records running to {c.get('latest_day') or 'no day'}, the oldest of "
                f"their last days {c.get('oldest_last_day') or 'none'}")
    if h["scope"] == "org":
        return (f"; {c.get('people_sent', 0)} people sent, their records running to "
                f"{c.get('latest_day') or 'no day'}")
    return (f"; {h['person']}'s records cover {c.get('days', 0)} day(s), the last "
            f"{c.get('last_day') or 'none'}")


# What the box said, as a reason shows it -> the same reason without the box's
# figures, for the ledger (ledger_safe). Bounded: one entry per reason made.
_LEDGER_SAFE: dict[str, str] = {}
_LEDGER_SAFE_MAX = 32


def ledger_safe(text: str) -> str:
    """`text` with every reason _box_words made in this process replaced by
    its version without the box's figures, coverage, host and times. The
    guard ledger is sent to whichever workspace this machine sends it to,
    and the box's figures are its own workspace's."""
    for shown, safe in _LEDGER_SAFE.items():
        if shown in text:
            text = text.replace(shown, safe)
    return text


def _box_words(st: dict[str, Any], h: dict[str, Any], u: dict[str, Any], *, budget: str,
               agents: str, mine: str) -> str:
    """The reason when the box's standing counts: both figures, how current
    the box's is, and which, the larger, was compared (never their sum).
    Its version for the ledger says the box's figure was read, not what it
    was (ledger_safe)."""
    from .budget.summary import age_words
    box = st.get("box") or {}
    theirs = _cents(h["box_usd"])
    # The compared figure's own as_of when the box sent one; the answer's,
    # the newest receipt from anyone in the workspace, only when it did not.
    at = h.get("box_as_of") or box.get("fetched_at")
    newest = (f"the newest of {_whose_records(h)} it received" if h.get("box_as_of_own")
              else "the newest it received from anyone in the workspace")
    when = (f"the box's records are as of {at}, {newest} "
            f"({age_words(h.get('box_age_hours'))} old, read by `nable cloud status` "
            f"at {box.get('fetched_at')})")
    on_box = (f"on the box ({box.get('host')}), {_whose(h)} stands at {theirs} in "
              f"list-price-equivalent dollars this month, from what people sent with "
              f"`nable cloud connect`{_cover_words(h)}; {when}")
    on_box_safe = (f"on the box, the month to date of {_whose(h)} was read by `nable cloud "
                   "status` (its figure is not kept in this ledger)")
    here = f"this machine's coding agents ({agents}) have spent {mine}"
    pct = f"{h['pct'] * 100:.0f}%"
    if h["compared"] == "box":
        text = (f"{on_box[0].upper()}{on_box[1:]}; {here}. The larger, the box's {theirs}, is "
                f"the one compared: {pct} of {budget}")
        safe = (f"{on_box_safe[0].upper()}{on_box_safe[1:]}; {here}. The larger, the box's, "
                f"is the one compared, against {budget}")
    else:
        text = (f"{here[0].upper()}{here[1:]}; {on_box}. The larger, this machine's {mine}, "
                f"is the one compared: {pct} of {budget}")
        safe = (f"{here[0].upper()}{here[1:]}; {on_box_safe}. The larger, this machine's "
                f"{mine}, is the one compared: {pct} of {budget}")
    rest = (f". The two are never added: the box's figure already holds whatever this "
            f"machine sent. Priced at {price_source(u['providers'])} on each machine that "
            "sent: usage, not an invoice. A machine that has not sent is not in the box's "
            "figure, so it can still under-count")
    text, safe = _unpriced_and_notes(text + rest, u), _unpriced_and_notes(safe + rest, u)
    if len(_LEDGER_SAFE) >= _LEDGER_SAFE_MAX:
        _LEDGER_SAFE.clear()
    _LEDGER_SAFE[text] = safe
    return text


def _unpriced_and_notes(text: str, u: dict[str, Any]) -> str:
    if u["unpriced_tokens"]:
        text += (f". {u['unpriced_tokens']:,} tokens from {', '.join(u['unpriced_models'])} "
                 "are not priced (llm_prices has no cited rate) and are not in the figure")
    for note in u["notes"]:
        text += f". {note.rstrip('.')}"
    return text
