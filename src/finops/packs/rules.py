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
from dataclasses import dataclass
from typing import Any

# What a regex condition or guard pattern reads of its input, at most. Keeps a
# bad pattern's worst case bounded by the input rather than by the caller.
MAX_MATCH_INPUT = 4096
GUARD_TARGETS = ("command", "mcp")
GUARD_VERDICTS = ("ask", "deny")
# Most permissive first. tighten() only ever moves right.
VERDICT_ORDER = ("allow", "warn", "ask", "deny")


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

    def matches_command(self, command: str) -> bool:
        return self.target == "command" and isinstance(command, str) \
            and self.pattern.search(command[:MAX_MATCH_INPUT]) is not None

    def matches_tool(self, tool: str, args: Any = None) -> bool:
        """MCP calls are matched as "<tool name> <args as sorted JSON>"."""
        if self.target != "mcp" or not isinstance(tool, str):
            return False
        try:
            blob = json.dumps(args if args is not None else {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            blob = ""
        return self.pattern.search(f"{tool} {blob}"[:MAX_MATCH_INPUT]) is not None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "pack": self.pack, "target": self.target,
                "pattern": self.pattern.pattern, "verdict": self.verdict,
                "reason": self.reason, "price_hint": self.price_hint}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GuardRule:
        """The inverse of to_dict, for a cache of rules content.py validated.
        Raises on anything that is not that shape, and on a verdict that could
        loosen: a cache cannot carry what a pack could not."""
        if d["target"] not in GUARD_TARGETS or d["verdict"] not in GUARD_VERDICTS:
            raise ValueError(f"not a guard rule: {d.get('id')!r}")
        return cls(str(d["id"]), d["target"], re.compile(d["pattern"]), d["verdict"],
                   str(d["reason"]), d.get("price_hint"), str(d.get("pack") or ""))


def tighten(verdict: str, rules: list[GuardRule], *, command: str | None = None,
            tool: str | None = None, args: Any = None) -> dict[str, Any]:
    """The verdict after pack guard rules, which is never looser than `verdict`.

    For the guard's later wiring: it passes its own verdict in and takes the
    stricter of the two out. Packs can move allow to ask and ask to deny; they
    cannot move anything the other way, and a verdict this does not know (for
    example fail_open) passes through untouched."""
    out = {"verdict": verdict, "rules": []}
    if verdict not in VERDICT_ORDER:
        return out
    best = VERDICT_ORDER.index(verdict)
    for r in rules:
        hit = (command is not None and r.matches_command(command)) or \
              (tool is not None and r.matches_tool(tool, args))
        if not hit:
            continue
        out["rules"].append({"id": r.id, "pack": r.pack, "verdict": r.verdict,
                             "reason": r.reason, "price_hint": r.price_hint})
        best = max(best, VERDICT_ORDER.index(r.verdict))
    out["verdict"] = VERDICT_ORDER[best]
    return out
