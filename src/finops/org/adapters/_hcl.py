# SPDX-License-Identifier: Apache-2.0
"""Just enough HCL to read Terraform's structure: blocks, their labels,
attributes as raw expressions, and the literal values those expressions
reduce to (strings, lists and maps of strings, var. and local. references).

Not an HCL implementation. Anything it cannot reduce to a literal (a
function call, a conditional, an unknown variable) reads as None, and the
adapter proposes nothing from it: a guess from a half-read expression is
worse than no proposal. Strings and heredocs are skipped properly, so a
brace or a # inside one never shifts a block.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_IDENT = re.compile(r"[A-Za-z_][\w-]*")
_HEREDOC = re.compile(r"<<-?([A-Za-z_]\w*)[ \t]*\n")


def _skip_string(s: str, i: int) -> int:
    """`s[i]` is a quote: the index just past the closing quote, template
    sequences (${ } and %{ }) with their nested strings skipped whole."""
    n = len(s)
    i += 1
    while i < n:
        ch = s[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            return i + 1
        if ch in "$%" and s.startswith("{", i + 1):
            i = _skip_braces(s, i + 1)
            continue
        if ch == "\n":
            return i                      # an unterminated string ends at the line
        i += 1
    return n


def _skip_heredoc(s: str, i: int) -> int | None:
    m = _HEREDOC.match(s, i)
    if not m:
        return None
    end = re.compile(r"^[ \t]*" + re.escape(m.group(1)) + r"[ \t]*$", re.MULTILINE).search(s, m.end())
    return end.end() if end else len(s)


def _skip_braces(s: str, i: int) -> int:
    """`s[i]` opens a bracket: the index past its match."""
    pairs = {"{": "}", "[": "]", "(": ")"}
    stack = [pairs[s[i]]]
    i += 1
    n = len(s)
    while i < n and stack:
        ch = s[i]
        if ch == '"':
            i = _skip_string(s, i)
            continue
        if ch == "<" and s.startswith("<<", i):
            j = _skip_heredoc(s, i)
            if j is not None:
                i = j
                continue
        if ch in pairs:
            stack.append(pairs[ch])
        elif ch == stack[-1]:
            stack.pop()
        i += 1
    return i


def strip_comments(s: str) -> str:
    """Comments (#, //, /* */) blanked to spaces, newlines kept, so line
    numbers still hold. Strings and heredocs are left alone."""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == '"':
            j = _skip_string(s, i)
            out.append(s[i:j])
            i = j
        elif ch == "<" and s.startswith("<<", i) and (j := _skip_heredoc(s, i)) is not None:
            out.append(s[i:j])
            i = j
        elif ch == "#" or s.startswith("//", i):
            j = s.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif s.startswith("/*", i):
            j = s.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", s[i:j]))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


@dataclass
class Block:
    type: str
    labels: list[str]
    line: int
    attrs: dict[str, str] = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)

    def find(self, type_: str) -> list[Block]:
        return [b for b in self.blocks if b.type == type_]


def _expr_end(s: str, i: int, end: int) -> int:
    """The end of an attribute expression starting at i: the newline (or,
    in an object, the comma) at bracket depth zero."""
    while i < end:
        ch = s[i]
        if ch in "\n,":
            return i
        if ch == '"':
            i = _skip_string(s, i)
            continue
        if ch == "<" and s.startswith("<<", i) and (j := _skip_heredoc(s, i)) is not None:
            i = j
            continue
        if ch in "{[(":
            i = _skip_braces(s, i)
            continue
        if ch in "}":
            return i
        i += 1
    return end


def _parse_body(s: str, start: int, end: int, into: Block) -> None:
    i = start
    while i < end:
        while i < end and s[i] in " \t\r\n,;":
            i += 1
        if i >= end:
            break
        m = _IDENT.match(s, i)
        if not m:
            if s[i] == '"':               # a quoted key in an object body
                j = _skip_string(s, i)
                name = s[i + 1:j - 1]
                i = j
            else:
                nl = s.find("\n", i)
                i = end if nl < 0 or nl >= end else nl + 1
                continue
        else:
            name = m.group(0)
            i = m.end()
        j = i
        while j < end and s[j] in " \t":
            j += 1
        if j < end and s[j] in "=:" and not s.startswith("==", j):
            k = _expr_end(s, j + 1, end)
            into.attrs[name] = s[j + 1:k].strip()
            i = k
            continue
        labels: list[str] = []
        while j < end:
            if s[j] == '"':
                k = _skip_string(s, j)
                labels.append(s[j + 1:k - 1])
                j = k
            elif (lm := _IDENT.match(s, j)) is not None:
                labels.append(lm.group(0))
                j = lm.end()
            else:
                break
            while j < end and s[j] in " \t":
                j += 1
        if j < end and s[j] == "{":
            k = _skip_braces(s, j)
            child = Block(name, labels, s.count("\n", 0, i) + 1)
            _parse_body(s, j + 1, k - 1, child)
            into.blocks.append(child)
            i = k
            continue
        nl = s.find("\n", j)
        i = end if nl < 0 or nl >= end else nl + 1


def parse(text: str) -> Block:
    """The file as a root block: top-level blocks and attributes."""
    s = strip_comments(text)
    root = Block("file", [], 1)
    _parse_body(s, 0, len(s), root)
    return root


def parse_json(text: str) -> Block:
    """A .tf.json file in the same shape (the blocks the adapter reads)."""
    root = Block("file", [], 1)
    try:
        data = json.loads(text)
    except ValueError:
        return root

    def lit(v: Any) -> str:
        return json.dumps(v)

    def fill(block: Block, body: Any) -> None:
        if not isinstance(body, dict):
            return
        for k, v in body.items():
            if isinstance(v, dict) and k in ("assume_role", "default_tags", "metadata",
                                             "backend", "assume_role_with_web_identity"):
                child = Block(k, [], 1)
                fill(child, v)
                block.blocks.append(child)
            elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v) and \
                    k in ("assume_role", "default_tags", "metadata"):
                for x in v:
                    child = Block(k, [], 1)
                    fill(child, x)
                    block.blocks.append(child)
            else:
                block.attrs[k] = lit(v)

    if not isinstance(data, dict):
        return root
    for kind, depth in (("provider", 1), ("resource", 2), ("module", 1), ("variable", 1),
                        ("locals", 0), ("terraform", 0)):
        section = data.get(kind)
        if section is None:
            continue
        items = section if isinstance(section, list) else [section]
        for item in items:
            if not isinstance(item, dict):
                continue
            if depth == 0:
                b = Block(kind, [], 1)
                fill(b, item)
                root.blocks.append(b)
                continue
            for l1, body in item.items():
                if depth == 1:
                    for one in body if isinstance(body, list) else [body]:
                        b = Block(kind, [l1], 1)
                        fill(b, one)
                        root.blocks.append(b)
                else:
                    if not isinstance(body, dict):
                        continue
                    for l2, inner in body.items():
                        for one in inner if isinstance(inner, list) else [inner]:
                            b = Block(kind, [l1, l2], 1)
                            fill(b, one)
                            root.blocks.append(b)
    return root


# ── literal values ────────────────────────────────────────────────────────────

_REF = re.compile(r"^(var|local)\.([A-Za-z_][\w-]*)$")
_INTERP = re.compile(r"\$\{\s*(var|local)\.([A-Za-z_][\w-]*)\s*\}")


class Scope:
    """var. and local. values of one module directory, as raw expressions,
    reduced on demand (with a depth bound: locals may refer to each other)."""

    def __init__(self, variables: dict[str, str] | None = None,
                 locals_: dict[str, str] | None = None) -> None:
        self.vars = dict(variables or {})
        self.locals = dict(locals_ or {})

    def ref(self, kind: str, name: str) -> str | None:
        return (self.vars if kind == "var" else self.locals).get(name)


def _unquote(body: str) -> str:
    try:
        return json.loads('"' + body + '"')
    except ValueError:
        return body


def string(expr: str | None, scope: Scope | None = None, depth: int = 0) -> str | None:
    """A literal string, after var./local. substitution; else None."""
    if expr is None or depth > 8:
        return None
    e = expr.strip()
    m = _REF.match(e)
    if m and scope is not None:
        return string(scope.ref(m.group(1), m.group(2)), scope, depth + 1)
    if len(e) >= 2 and e[0] == '"' and _skip_string(e, 0) == len(e):
        body = e[1:-1]

        def sub(mm: re.Match[str]) -> str:
            got = string(scope.ref(mm.group(1), mm.group(2)), scope, depth + 1) \
                if scope is not None else None
            return got if got is not None else mm.group(0)

        body = _INTERP.sub(sub, body)
        if "${" in body or "%{" in body:
            return None
        return _unquote(body)
    return None


def _split_top(body: str) -> list[str]:
    parts: list[str] = []
    i, start, n = 0, 0, len(body)
    while i < n:
        ch = body[i]
        if ch == '"':
            i = _skip_string(body, i)
            continue
        if ch in "{[(":
            i = _skip_braces(body, i)
            continue
        if ch in ",\n":
            parts.append(body[start:i])
            start = i + 1
        i += 1
    parts.append(body[start:])
    return [p.strip() for p in parts if p.strip()]


def strings(expr: str | None, scope: Scope | None = None, depth: int = 0) -> list[str]:
    """A literal list of strings (items that are not literal are dropped)."""
    if expr is None or depth > 8:
        return []
    e = expr.strip()
    m = _REF.match(e)
    if m and scope is not None:
        return strings(scope.ref(m.group(1), m.group(2)), scope, depth + 1)
    if e.startswith("[") and e.endswith("]"):
        return [v for v in (string(p, scope, depth + 1) for p in _split_top(e[1:-1]))
                if v is not None]
    one = string(e, scope, depth + 1)
    return [one] if one is not None else []


def mapping(expr: str | None, scope: Scope | None = None, depth: int = 0) -> dict[str, str]:
    """A literal map of strings ({ K = "v", "K2" = "v2" }); values that are
    not literal are dropped."""
    if expr is None or depth > 8:
        return {}
    e = expr.strip()
    m = _REF.match(e)
    if m and scope is not None:
        return mapping(scope.ref(m.group(1), m.group(2)), scope, depth + 1)
    if not (e.startswith("{") and e.endswith("}")):
        return {}
    b = Block("map", [], 1)
    _parse_body(e, 1, len(e) - 1, b)
    out: dict[str, str] = {}
    for k, v in b.attrs.items():
        got = string(v, scope, depth + 1)
        if got is not None:
            out[k] = got
    return out
