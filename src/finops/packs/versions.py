# SPDX-License-Identifier: Apache-2.0
"""Pack versions and version ranges, without a dependency.

Pack versions are strict semver (1.2.0, 1.2.0-rc.1). Ranges are what a
manifest's `nable_api` holds: comma-separated clauses such as ">=1.0,<2.0",
each an operator (>=, >, <=, <, ==, !=) and a numeric version of one to three
parts, padded with zeros ("1" is 1.0.0). Every clause must hold.
"""
from __future__ import annotations

import re

_SEMVER = re.compile(
    r"^(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})"
    r"(?:-([0-9A-Za-z-]{1,32}(?:\.[0-9A-Za-z-]{1,32}){0,4}))?$")
_NUMERIC = re.compile(r"^(\d{1,6})(?:\.(\d{1,6}))?(?:\.(\d{1,6}))?$")
_CLAUSE = re.compile(r"^\s*(>=|<=|==|!=|>|<)\s*([0-9][0-9.]*)\s*$")

VersionKey = tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]


def is_semver(text: object) -> bool:
    return isinstance(text, str) and bool(_SEMVER.match(text))


def _pre_part(ident: str) -> tuple[int, int | str]:
    """Semver 11.4.1 to 11.4.3: numeric identifiers compare numerically and
    sort before alphanumeric ones, which compare as ASCII text."""
    return (0, int(ident)) if ident.isdigit() else (1, ident)


def version_key(text: str) -> VersionKey:
    """Sort key for a semver string, in semver precedence: a pre-release sorts
    before its release, rc.2 before rc.10, 1 before alpha, and a longer set of
    identifiers after a shorter one it starts with (alpha < alpha.1).
    Raises ValueError for anything that is not strict semver."""
    m = _SEMVER.match(text or "")
    if not m:
        raise ValueError(f"{text!r} is not a version like 1.2.0")
    pre = m.group(4)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)),
            0 if pre else 1, tuple(_pre_part(x) for x in pre.split(".")) if pre else ())


def _numeric_key(text: str) -> tuple[int, int, int]:
    m = _NUMERIC.match(text.strip())
    if not m:
        raise ValueError(f"{text!r} is not a version like 1.0")
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))


def parse_range(spec: object) -> list[tuple[str, tuple[int, int, int]]]:
    """">=1.0,<2.0" -> [(">=", (1, 0, 0)), ("<", (2, 0, 0))]. Raises ValueError
    with the clause that is wrong."""
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("must be a version range such as \">=1.0,<2.0\"")
    clauses = []
    for part in spec.split(","):
        m = _CLAUSE.match(part)
        if not m:
            raise ValueError(f"clause {part.strip()!r} is not an operator "
                             "(>=, >, <=, <, ==, !=) followed by a version")
        clauses.append((m.group(1), _numeric_key(m.group(2))))
    return clauses


def in_range(version: str, spec: str) -> bool:
    """Whether a numeric version ("1.0") satisfies every clause of `spec`."""
    v = _numeric_key(version)
    for op, bound in parse_range(spec):
        ok = {">=": v >= bound, ">": v > bound, "<=": v <= bound, "<": v < bound,
              "==": v == bound, "!=": v != bound}[op]
        if not ok:
            return False
    return True
