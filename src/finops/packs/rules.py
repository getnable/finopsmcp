# SPDX-License-Identifier: Apache-2.0
"""Guard rules as the guard's hook uses them: the rule type and tighten().

Split out of content.py (which re-exports both, so finops.packs.content.tighten
is this function) because the hook reads them on every agent tool call, and
content.py brings PyYAML and every other schema with it. This module is
standard library only; content.py still parses and validates the files.
"""
from __future__ import annotations

import json
import re
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# What a regex condition or guard pattern reads of its input, at most. This
# bounds a pattern that runs in linear time, and nothing else: a pattern that
# backtracks exponentially needs only a few dozen characters to run for
# hours, so 4096 is no bound for it. Those are refused when a pack is
# validated (content.regex_problem), and in the hook MATCH_BUDGET_S stops a
# pattern that slips past that check.
MAX_MATCH_INPUT = 4096
# How long one pack guard rule may search one input in the hook, where a timer
# can stop it (POSIX, the main thread). Past it the rule counts as a match
# that asks, with a reason naming the rule: never a silent allow. Elsewhere
# (Windows, a worker thread) only the validation-time check applies.
MATCH_BUDGET_S = 0.05
GUARD_TARGETS = ("command", "mcp")
GUARD_VERDICTS = ("ask", "deny")
# When a rule applies at all. Absent: always. "freeze": only while an org
# model change freeze (org facts of kind freeze) covers what the call touches.
# A condition only narrows when a rule fires, so it can never loosen anything;
# and a freeze nobody confirmed caps the rule at ask (a guess may restrict,
# never stop a command outright).
GUARD_DURING = ("freeze",)
# Most permissive first. tighten() only ever moves right.
VERDICT_ORDER = ("allow", "warn", "ask", "deny")


class _OverBudget(Exception):
    """Raised by the SIGALRM handler inside a pattern search."""


def _on_alarm(signum: int, frame: Any) -> None:
    raise _OverBudget


def _can_time() -> bool:
    """A timer can stop a search here: POSIX, the main thread, and no other
    ITIMER_REAL armed (that one is not ours to replace)."""
    if not hasattr(signal, "setitimer") or \
            threading.current_thread() is not threading.main_thread():
        return False
    try:
        return signal.getitimer(signal.ITIMER_REAL)[0] == 0.0
    except (OSError, ValueError):
        return False


def _search(pattern: re.Pattern[str], text: str) -> bool | None:
    """Whether `pattern` finds a match in `text`; None when the search ran
    past MATCH_BUDGET_S and was stopped. The re engine checks for signals
    while it backtracks, so the alarm lands inside the search."""
    if not _can_time():
        return pattern.search(text) is not None
    try:
        old = signal.signal(signal.SIGALRM, _on_alarm)
    except (OSError, ValueError):
        return pattern.search(text) is not None
    try:
        try:
            signal.setitimer(signal.ITIMER_REAL, MATCH_BUDGET_S)
            return pattern.search(text) is not None
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    except _OverBudget:
        return None
    finally:
        signal.signal(signal.SIGALRM, old)


@dataclass(frozen=True)
class GuardRule:
    """A pattern the guard may use to tighten a verdict, never to loosen one."""

    id: str
    target: str
    pattern: re.Pattern[str]
    verdict: str
    reason: str
    price_hint: dict[str, Any] | None = None
    pack: str = ""
    during: str | None = None

    def matches_command(self, command: str) -> bool:
        """True when the rule matches, or its pattern ran out of time."""
        return self.match_command(command) is not False

    def matches_tool(self, tool: str, args: Any = None) -> bool:
        """MCP calls are matched as "<tool name> <args as sorted JSON>"."""
        return self.match_tool(tool, args) is not False

    def match_command(self, command: str) -> bool | None:
        """Whether the rule matches `command`; None when its pattern ran past
        MATCH_BUDGET_S and was stopped."""
        if self.target != "command" or not isinstance(command, str):
            return False
        return _search(self.pattern, command[:MAX_MATCH_INPUT])

    def match_tool(self, tool: str, args: Any = None) -> bool | None:
        """match_command for an MCP call."""
        if self.target != "mcp" or not isinstance(tool, str):
            return False
        try:
            blob = json.dumps(args if args is not None else {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            blob = ""
        return _search(self.pattern, f"{tool} {blob}"[:MAX_MATCH_INPUT])

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "pack": self.pack, "target": self.target,
                "pattern": self.pattern.pattern, "verdict": self.verdict,
                "reason": self.reason, "price_hint": self.price_hint, "during": self.during}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GuardRule:
        """The inverse of to_dict, for a cache of rules content.py validated.
        Raises on anything that is not that shape, and on a verdict that could
        loosen: a cache cannot carry what a pack could not."""
        if d["target"] not in GUARD_TARGETS or d["verdict"] not in GUARD_VERDICTS \
                or d.get("during") not in (None, *GUARD_DURING):
            raise ValueError(f"not a guard rule: {d.get('id')!r}")
        return cls(str(d["id"]), d["target"], re.compile(d["pattern"]), d["verdict"],
                   str(d["reason"]), d.get("price_hint"), str(d.get("pack") or ""),
                   d.get("during"))


def tighten(verdict: str, rules: list[GuardRule], *, command: str | None = None,
            tool: str | None = None, args: Any = None,
            freeze: Callable[[], dict[str, Any] | None] | None = None) -> dict[str, Any]:
    """The verdict after pack guard rules, which is never looser than `verdict`.

    For the guard's later wiring: it passes its own verdict in and takes the
    stricter of the two out. Packs can move allow to ask and ask to deny; they
    cannot move anything the other way, and a verdict this does not know (for
    example fail_open) passes through untouched.

    A rule whose pattern runs past MATCH_BUDGET_S counts as a match that asks,
    with a reason naming the rule: the guard cannot tell whether it matched,
    so a person decides, and the pack's author learns which rule to fix.

    A rule with `during: freeze` applies only while a change freeze covers the
    call. `freeze` is called, at most once and only when such a rule matches,
    and returns the freeze in force ({"sure", "words", "key", ...}, as
    guard_org.freeze does) or None. With None the rule does not apply. A
    freeze that is not sure (a proposal, or a repo's nobody trusted) caps the
    rule at ask. When `freeze` is missing or raises, nobody can tell whether a
    freeze is in force, so the rule asks: never an allow, never a deny."""
    out: dict[str, Any] = {"verdict": verdict, "rules": []}
    if verdict not in VERDICT_ORDER:
        return out
    best = VERDICT_ORDER.index(verdict)
    memo: list[tuple[dict[str, Any] | None, bool]] = []

    def freeze_state() -> tuple[dict[str, Any] | None, bool]:
        """(the freeze in force or None, whether that could be told)."""
        if not memo:
            try:
                if freeze is None:
                    raise LookupError("no freeze lookup was given")
                got = freeze()
                memo.append((got if isinstance(got, dict) else None, True))
            except Exception:  # noqa: BLE001 - not knowing asks; it never allows
                memo.append((None, False))
        return memo[0]

    for r in rules:
        hit: bool | None = False
        if command is not None:
            hit = r.match_command(command)
        if hit is False and tool is not None:
            hit = r.match_tool(tool, args)
        if hit is False:
            continue
        fz: dict[str, Any] | None = None
        if r.during == "freeze":
            fz, known = freeze_state()
            if not known:
                out["rules"].append({
                    "id": r.id, "pack": r.pack, "verdict": "ask", "during": r.during,
                    "reason": (f"{r.reason.rstrip(' .')} (rule {r.id} applies during a change "
                               "freeze, and the guard could not tell whether one is in force, "
                               "so it asks)"),
                    "price_hint": None})
                best = max(best, VERDICT_ORDER.index("ask"))
                continue
            if fz is None:
                continue
        if hit is None:
            out["rules"].append({
                "id": r.id, "pack": r.pack, "verdict": "ask", "timed_out": True,
                "reason": (f"the pattern of guard rule {r.id} in pack {r.pack or '(unnamed)'} "
                           f"ran longer than {MATCH_BUDGET_S * 1000:g} ms on this call and was "
                           "stopped, so the guard cannot tell whether it matches; fix or "
                           "remove that rule"),
                "price_hint": None})
            best = max(best, VERDICT_ORDER.index("ask"))
            continue
        give, said = r.verdict, r.reason
        entry: dict[str, Any] = {"id": r.id, "pack": r.pack}
        if fz is not None:
            if give == "deny" and not fz.get("sure"):
                give = "ask"             # a guess may restrict, never stop outright
            said = f"{r.reason.rstrip()} {fz.get('words') or 'A change freeze is in force.'}"
            entry["during"] = r.during
            entry["freeze"] = {k: fz[k] for k in ("key", "subject", "reason", "end", "mode",
                                                  "sure") if k in fz}
        entry.update(verdict=give, reason=said, price_hint=r.price_hint)
        out["rules"].append(entry)
        best = max(best, VERDICT_ORDER.index(give))
    out["verdict"] = VERDICT_ORDER[best]
    return out
