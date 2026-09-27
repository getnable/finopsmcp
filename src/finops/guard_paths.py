# SPDX-License-Identifier: Apache-2.0
"""The files that decide what the guard allows, which an agent must not edit.

The org model, the policy file, the installed packs and the guard's own
switches and records are how a person tells the guard what is allowed: a
confirmed per-team threshold, a trusted pack key, the off flag. An agent
that edits them directly can loosen its own guard (append a confirmed
threshold to nable.org/policy.yaml, add a key to packs.trusted_keys, delete
a guard-rule pack, touch ~/.finops/guard-off), so every write to one asks:

  a shell command       guard.gate_command (the protected-write self rule)
  an MCP tool call      guard.gate_mcp_call (a path or a command argument)
  Claude Code's editor  Write, Edit, MultiEdit and NotebookEdit, answered by
                        guard_plugin before the guard is imported

protected() lists them; match() says whether a path is one of them. Both
run on every editor tool call Claude Code sends the hook, before the guard
is imported, so this module is standard library only, creates nothing (the
data directory rule is copied from guard_ledger._data_dir without its
mkdir), reads no file and runs no subprocess: a few environment variables,
a walk up to the git root and a realpath per entry. Where a module that
knows a path better is already imported (a test's override of the ledger
or the packs root), its answer is used instead.

Paths are compared after realpath on both sides, so `~`, `$HOME`, a
relative path from inside the repo, `..` and a symlinked directory name all
reach the same file. On macOS and Windows, whose default filesystems ignore
case, the comparison does too.
"""
from __future__ import annotations

import contextvars
import fnmatch
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

# The names nable gives the files beside the decision ledger (guard_ledger,
# budget/summary.py, guard_org.py). Copied, not imported: this module must
# not pull in the ledger's redaction patterns.
_LEDGER_FILES = (
    ("guard-ledger.jsonl", "the guard's decision ledger"),
    ("guard-ledger.anchor.json", "the decision ledger's anchor"),
    ("guard-ledger.unrecorded.jsonl", "the record of verdicts the ledger missed"),
    ("budget-summary.json", "the budget summary the guard checks spend against"),
    ("org-model-cache.json", "the guard's cache of the org model"),
    ("packs-guard-cache.json", "the guard's cache of pack rules and price books"),
    # guard_approvals.STORE_NAME: an approval written here lets a call through.
    ("guard-approvals.json", "the one-time approvals for calls the guard stopped"),
)
# Directories beside the ledger: facts derived from the org model's files,
# keyed on their content (written into, they would be read as facts).
_LEDGER_DIRS = (
    ("org-parse-cache", "the guard's cache of facts parsed from the org model"),
)
POLICY_FILE_NAME = "nable.policy.yaml"
ORG_DIR_NAME = "nable.org"
# Any file with one of these names: `nable budget ci-gate --budget-file` and
# sync_budgets_from_yaml read budgets from it, wherever it is kept.
PROTECTED_NAMES = {"budget.yml": "a budget file the guard's budgets come from",
                   "budget.yaml": "a budget file the guard's budgets come from"}
_FOLD = sys.platform in ("darwin", "win32")


class Protected(NamedTuple):
    """A NamedTuple, not a dataclass: importing dataclasses costs the
    file-edit fast path more than everything else it does."""

    path: str       # absolute and resolved (realpath), the form compared
    shown: str      # as a person would write it: ~ for the home directory
    what: str       # what it is, for the reason a human reads
    tree: bool = False      # a directory: everything under it is protected too
    # A directory that only a change to all of it reaches (`rm -rf ~/.finops`,
    # however little is in it); a file written inside it is not protected.
    whole: bool = False

    def as_dict(self) -> dict:
        return {"path": self.shown, "what": self.what, "tree": self.tree}


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _home() -> Path:
    return Path.home()


# What asking an already-imported module for a path may raise (its data dir
# could not be created); the copied rule answers instead.
_LOOKUP_ERRORS = (OSError, AttributeError, TypeError, ValueError)


def data_dir() -> Path:
    """guard_ledger._data_dir's rule (FINOPS_PROFILE, FINOPS_DATA_DIR or
    ~/.finops), without creating anything. A test pins that the two agree."""
    db = sys.modules.get("finops.storage.db")
    try:
        if db is not None:
            return Path(db.data_dir())
    except _LOOKUP_ERRORS:
        db = None
    profile = _env("FINOPS_PROFILE")
    if profile:
        return _home() / ".finops" / "profiles" / profile
    raw = os.environ.get("FINOPS_DATA_DIR", "")
    return Path(raw).expanduser() if raw else _home() / ".finops"


def _ledger() -> Path:
    led = sys.modules.get("finops.guard_ledger")
    try:
        if led is not None:
            return Path(led.ledger_path())
    except _LOOKUP_ERRORS:
        led = None
    return data_dir() / "guard-ledger.jsonl"


def _packs_root() -> Path:
    store = sys.modules.get("finops.packs.store")
    try:
        if store is not None:
            return Path(store.packs_root())
    except _LOOKUP_ERRORS:
        store = None
    return data_dir() / "packs"


def _policy_files() -> list[Path]:
    """FINOPS_POLICY_FILE and the data directory's nable.policy.yaml: both,
    since the agent's shell may not carry the variable the hook sees."""
    out = []
    raw = _env("FINOPS_POLICY_FILE")
    if raw:
        out.append(Path(raw).expanduser())
    out.append(data_dir() / POLICY_FILE_NAME)
    return out


def git_root(start: str | os.PathLike | None = None) -> Path | None:
    """finops.org.store.git_root, copied: the org package is not light."""
    try:
        p = Path(start or os.getcwd()).resolve()
    except (OSError, RuntimeError):
        return None
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return d
    return None


# ── What git would put back ───────────────────────────────────────────────────
# `git checkout .`, `git restore .` and `git stash` change only the files git
# tracks, and push only the ones changed since the last commit: a protected
# file that is untracked (Claude Code's settings.local.json) or untouched is
# not theirs to change. Read from the index and the reflog's time, without
# running git; anything this does not read says None, and the caller falls
# back to what is there.
_INDEX_MAX = 64 * 1024 * 1024
_CHANGED_WALK_MAX = 512


def _git_dir(root: str) -> Path | None:
    dot = Path(root) / ".git"
    if dot.is_dir():
        return dot
    try:
        text = dot.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gd = Path(text[len("gitdir:"):].strip())
    return gd if gd.is_absolute() else Path(root) / gd


def git_tracked(path: str, root: str, tree: bool = False) -> bool | None:
    """Does the index of the repo at `root` hold `path` (or, for a `tree`,
    anything under it)? None when that cannot be told: no index, a split or
    v4 index, a path outside the repo, a filesystem that ignores case."""
    if _FOLD:
        return None
    rel = os.path.relpath(path, root)
    if rel == "." or rel.startswith(".."):
        return None
    gd = _git_dir(root)
    try:
        if gd is None or any(n.startswith("sharedindex.") for n in os.listdir(gd)):
            return None
        index = gd / "index"
        if index.stat().st_size > _INDEX_MAX:
            return None
        charge(1 + index.stat().st_size // (1024 * 1024))
        data = index.read_bytes()
    except OSError:
        return None
    if data[:4] != b"DIRC" or int.from_bytes(data[4:8], "big") not in (2, 3):
        return None
    needle = rel.replace(os.sep, "/").encode("utf-8", "surrogateescape")
    needle += b"/" if tree else b"\x00"
    i = data.find(needle, 12)
    while i >= 0:
        # An entry's name follows its flags (and in v3, maybe two bytes of
        # extended flags), whose low 12 bits are the name's length.
        end = data.find(b"\x00", i)
        for back in (2, 4):
            n = int.from_bytes(data[i - back:i - back + 2], "big") & 0xFFF
            if i - back >= 12 and (n == end - i or (n == 0xFFF and end - i >= 0xFFF)):
                return True
        i = data.find(needle, i + 1)
    return False


def git_changed(path: str, root: str, tree: bool = False) -> bool | None:
    """Has `path` (or, for a `tree`, anything in it) changed since the repo
    at `root` last moved HEAD (its reflog's time)? A path that is not there
    has (a checkout brings it back). None when there is no reflog."""
    gd = _git_dir(root)
    try:
        since = (gd / "logs" / "HEAD").stat().st_mtime if gd is not None else None
    except OSError:
        since = None
    if since is None:
        return None
    try:
        if os.lstat(path).st_mtime > since:
            return True
    except OSError:
        return True
    if not tree:
        return False
    seen = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for name in (*dirnames, *filenames):
            seen += 1
            if seen > _CHANGED_WALK_MAX:
                return True
            try:
                if os.lstat(os.path.join(dirpath, name)).st_mtime > since:
                    return True
            except OSError:
                return True
        charge(1 + (len(dirnames) + len(filenames)) // _UNIT_ENTRIES)
    return False


def _claude_user_dir() -> Path:
    from . import guard_plugin
    return guard_plugin.claude_user_dir()


def _project_dirs(cwd: str | None) -> list[Path]:
    """Where the agent's project settings live: CLAUDE_PROJECT_DIR, the
    working directory and its git root."""
    dirs: list[Path] = []
    for raw in (_env("CLAUDE_PROJECT_DIR"), cwd or ""):
        if raw:
            dirs.append(Path(raw).expanduser())
    try:
        dirs.append(Path(cwd) if cwd else Path.cwd())
    except OSError:
        pass
    root = git_root(cwd)
    if root is not None:
        dirs.append(root)
    return list(dict.fromkeys(dirs))


def _managed_settings() -> list[Path]:
    if sys.platform == "darwin":
        return [Path("/Library/Application Support/ClaudeCode/managed-settings.json")]
    if sys.platform == "win32":
        return [Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
                / "ClaudeCode" / "managed-settings.json"]
    return [Path("/etc/claude-code/managed-settings.json")]


def _hook_files(projects: list[Path]) -> list[tuple[Path, str]]:
    """Every settings or hooks file a harness reads the guard's hook from
    (guard_adapters.hooks_path, per harness and scope; a test pins that the
    two agree), plus Codex's config.toml, whose [features] table can turn
    its hooks off."""
    home = _home()
    codex = Path(_env("CODEX_HOME")) if _env("CODEX_HOME") else home / ".codex"
    copilot = Path(_env("COPILOT_HOME")) if _env("COPILOT_HOME") else home / ".copilot"
    gemini = (Path(_env("GEMINI_CLI_HOME")) if _env("GEMINI_CLI_HOME") else home) / ".gemini"
    user = _claude_user_dir()
    out = [(user / "settings.json", "Claude Code's user settings, which carry the guard hook"),
           (user / "settings.local.json", "Claude Code's user settings"),
           (home / ".cursor" / "hooks.json", "Cursor's hooks file, which carries the guard hook"),
           (codex / "hooks.json", "Codex's hooks file, which carries the guard hook"),
           (codex / "config.toml", "Codex's config, which can turn its hooks off"),
           (copilot / "hooks" / "nable-guard.json", "Copilot's guard hook file"),
           (gemini / "settings.json", "Gemini CLI's settings, which carry the guard hook"),
           (home / "Documents" / "Cline" / "Hooks" / "PreToolUse", "Cline's guard hook")]
    out += [(p, "Claude Code's managed settings") for p in _managed_settings()]
    for d in projects:
        out += [(d / ".claude" / "settings.json", "this project's Claude Code settings"),
                (d / ".claude" / "settings.local.json", "this project's Claude Code settings"),
                (d / ".cursor" / "hooks.json", "this project's Cursor hooks"),
                (d / ".codex" / "hooks.json", "this project's Codex hooks"),
                (d / ".codex" / "config.toml", "this project's Codex config"),
                (d / ".github" / "hooks" / "nable-guard.json", "this project's Copilot guard hook"),
                (d / ".gemini" / "settings.json", "this project's Gemini CLI settings"),
                (d / ".clinerules" / "hooks" / "PreToolUse", "this project's Cline guard hook")]
    return out


def _real(p: Path | str) -> str:
    s = os.path.realpath(os.fspath(p))
    return s.casefold() if _FOLD else s


def _shown(p: Path) -> str:
    s = os.fspath(p)
    home = os.fspath(_home())
    if home and home != os.sep and (s == home or s.startswith(home + os.sep)):
        return "~" + s[len(home):]
    return s


def protected(cwd: str | None = None) -> list[Protected]:
    """Every protected path for an agent working in `cwd` (default: this
    process's), most important first, each once."""
    from . import guard_plugin

    ledger = _ledger()
    data = data_dir()
    entries: list[tuple[Path, str, bool]] = []
    org_env = _env("FINOPS_ORG_DIR")
    if org_env:
        entries.append((Path(org_env).expanduser(), "the org model (FINOPS_ORG_DIR)", True))
    root = git_root(cwd)
    if root is not None:
        # Whether it exists or not: creating it is what makes it the org
        # model this repo's agents are judged by.
        entries.append((root / ORG_DIR_NAME, "this repo's org model", True))
    entries.append((data / "org", "the org model in nable's data directory", True))
    entries += [(p, "the guard's policy file", False) for p in _policy_files()]
    packs = _packs_root()
    entries.append((packs, "the installed packs and their index", True))
    entries.append((guard_plugin.off_flag_path(), "the guard's off switch", False))
    for dirpath in dict.fromkeys((ledger.parent, data)):
        entries += [(dirpath / name, what, False) for name, what in _LEDGER_FILES]
        entries += [(dirpath / name, what, True) for name, what in _LEDGER_DIRS]
    ai = Path(os.environ["FINOPS_DATA_DIR"]).expanduser() if os.environ.get(
        "FINOPS_DATA_DIR") else _home() / ".nable"
    entries.append((ai / "ai-budget.json", "the agent's AI budget", False))
    tag_rules = _env("FINOPS_TAG_RULES")
    accounts = _env("FINOPS_ACCOUNTS_FILE")
    entries.append((Path(tag_rules).expanduser() if tag_rules
                    else _home() / ".finops" / "tag_rules.yaml",
                    "tag rules the org model reads as confirmed facts", False))
    entries.append((Path(accounts).expanduser() if accounts
                    else _home() / ".finops-mcp" / "accounts.yaml",
                    "accounts the org model reads as confirmed facts", False))
    entries += [(p, what, False) for p, what in _hook_files(_project_dirs(cwd))]
    entries.append((_claude_user_dir() / "plugins",
                    ("Claude Code's installed plugins, the nable plugin's guard hook "
                     "among them"), True))
    out: list[Protected] = []
    seen: set[str] = set()
    for p, what, tree in entries:
        try:
            key = _real(p)
        except (OSError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(Protected(key, _shown(p), what, tree))
    try:
        key = _real(data)
    except (OSError, ValueError):
        return out
    if key not in seen:
        out.append(Protected(key, _shown(data), "nable's data directory, which holds the "
                             "guard's own files", whole=True))
    return out


# ── Work done for one check ──────────────────────────────────────────────────
# The guard's shell check runs this module over every path a command names.
# Each resolve, directory listing and brace expansion is charged here while
# a meter is running (meter(): the guard's shell and MCP checks), and past
# its units TooMuch is raised, so a command padded with thousands of paths
# gets an answer (an ask) inside the hook's timeout instead of none. The
# editor fast path runs no meter and is charged nothing.
_METER: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "nable_guard_paths_meter", default=None)
# One unit (a quarter of a millisecond or so): a path with this many parts to
# resolve (realpath stats each), this many directory entries to list, or
# this much text to build.
_UNIT_PARTS = 16
_UNIT_ENTRIES = 64
_UNIT_CHARS = 1024
# The longest a path may grow by its variables before it is not read.
_EXPAND_MAX = 64 * 1024


class TooMuch(Exception):
    """A metered check ran out of units."""


def meter(units: int) -> contextvars.Token:
    """Start charging this context's work against `units`; unmeter() stops."""
    return _METER.set([units])


def unmeter(token: contextvars.Token) -> None:
    _METER.reset(token)


def charge(units: int) -> None:
    """Spend `units` of the running meter, if there is one."""
    left = _METER.get()
    if left is not None:
        left[0] -= units
        if left[0] < 0:
            raise TooMuch


class _TooLong(Exception):
    pass


_VAR_RE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")
_GLOB_CHARS = frozenset("*?[")
_BRACE_TOKEN_RE = re.compile(r"[{}]")
# The alternatives of one brace that are resolved. Past these, only the ones
# that name something protected by its file or directory name are.
_BRACE_MAX = 64
# Directory entries a glob's expansion may list before it is taken to match.
_GLOB_VISITS = 4096


def _expand(path: str, env: Mapping[str, str] | None) -> str | None:
    """`$VAR` and `${VAR}` from `env` (assignments earlier on the command
    line), then the environment; `~` and `~user`. None when a variable is
    unset, or written in a form this does not read (`${X:-y}`), or when it
    grows past _EXPAND_MAX (`$V$V$V...`), which a meter is charged for."""
    grown = [len(path)]

    def one(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        val = (env or {}).get(name)
        if val is None:
            val = os.environ.get(name)
        if val is None:
            return m.group(0)
        grown[0] += len(val)
        if grown[0] > _EXPAND_MAX:
            raise _TooLong
        # `D=~/.finops` assigns the expanded home, as the shell does.
        return os.path.expanduser(val) if val.startswith("~") else val
    try:
        for _ in range(4):              # D=$HOME/x; E=$D/y; rm $E/z
            if "$" not in path:
                break
            grown[0] = len(path)
            path = _VAR_RE.sub(one, path)
    except _TooLong:
        charge(_EXPAND_MAX // _UNIT_PARTS)
        return None
    if "$" in path:
        return None
    return os.path.expanduser(path)


def _braces(path: str, entries: list[Protected]) -> list[str]:
    """One level of brace expansion: `~/.finops/{a,guard-off}` is two paths.
    The first `{...}` with a comma and no brace inside, found in one pass
    over the braces: a regex over `{a,a,a,...` with no `}` rescanned the rest
    from every comma, quadratic in the length."""
    start = -1
    for m in _BRACE_TOKEN_RE.finditer(path):
        if m.group() == "{":
            start = m.start()
            continue
        if start < 0:
            continue
        body = path[start + 1:m.start()]
        if "," in body:
            alts = body.split(",")
            if len(alts) > _BRACE_MAX:
                names = {n for e in entries for n in (os.path.basename(e.path),
                                                      os.path.basename(os.path.dirname(e.path)))}
                names.update(PROTECTED_NAMES, (ORG_DIR_NAME,))
                marked = re.compile("|".join(re.escape(n) for n in names if n))
                alts = alts[:_BRACE_MAX] + [a for a in alts[_BRACE_MAX:]
                                            if marked.search(a)][:_BRACE_MAX * 3]
            charge((len(alts) * (len(path) - len(body)) + len(body)) // _UNIT_CHARS)
            return [path[:start] + a + path[m.end():] for a in alts]
        start = -1
    return [path]


_BRACE_PATHS_MAX = 256


def _all_braces(path: str, entries: list[Protected]) -> list[str]:
    """Every level of brace expansion (`{a,{b,guard-off}}`), up to
    _BRACE_PATHS_MAX paths."""
    out: list[str] = []
    todo = [path]
    while todo and len(out) + len(todo) < _BRACE_PATHS_MAX:
        one = todo.pop()
        alts = _braces(one, entries) if "{" in one else [one]
        if alts == [one]:
            out.append(one)
        else:
            todo += alts
    return out + todo


class _TooMany(Exception):
    pass


def _glob_paths(base: str, comps: list[str]):
    """Each path that exists and that the glob `base`/`comps` expands to
    (`**` as zsh and bash's globstar read it). Raises _TooMany past
    _GLOB_VISITS directory entries listed."""
    budget = [_GLOB_VISITS]

    def walk(d: str, k: int):
        if k == len(comps):
            yield d
            return
        c = comps[k]
        if not _GLOB_CHARS & set(c):
            nxt = os.path.join(d, c)
            if os.path.lexists(nxt):
                yield from walk(nxt, k + 1)
            return
        try:
            names = os.listdir(d)
        except OSError:
            return
        charge(1 + len(names) // _UNIT_ENTRIES)
        budget[0] -= len(names) + 1
        if budget[0] < 0:
            raise _TooMany
        if c == "**":
            yield from walk(d, k + 1)
            for n in names:
                sub = os.path.join(d, n)
                if os.path.isdir(sub) and not os.path.islink(sub):
                    yield from walk(sub, k)
            return
        for n in names:
            if fnmatch.fnmatchcase(n.casefold() if _FOLD else n, c):
                yield from walk(os.path.join(d, n), k + 1)
    return walk(base, 0)


def _glob_first(base: str, comps: list[str]) -> str | None:
    """The first path the glob `base`/`comps` expands to, or None. Past
    _GLOB_VISITS directory entries, `base` is returned as if one matched."""
    try:
        return next(_glob_paths(base, comps), None)
    except _TooMany:
        return base
    except (OSError, RecursionError, ValueError):
        return None


def glob_names(pattern: str, cwd: str | None = None, *,
               env: Mapping[str, str] | None = None) -> list[str] | None:
    """The file names a shell glob expands to (`config/*.yaml` to budget.yaml
    and the rest), [] when it expands to nothing or is not a glob, None when
    it names more than the guard lists (_GLOB_VISITS)."""
    p = _expand(pattern, env)
    if p is None or not _GLOB_CHARS & set(p):
        return []
    if not os.path.isabs(p):
        p = os.path.join(os.path.expanduser(cwd or os.getcwd()), p)
    parts = p.split(os.sep)
    i = next(n for n, part in enumerate(parts) if _GLOB_CHARS & set(part))
    charge(1 + p.count(os.sep) // _UNIT_PARTS)
    try:
        fixed = os.path.realpath(os.sep.join(parts[:i]) or os.sep)
        return list(dict.fromkeys(os.path.basename(f) for f in _glob_paths(
            fixed, [c.casefold() if _FOLD else c for c in parts[i:] if c and c != "."])))
    except _TooMany:
        return None
    except (OSError, RecursionError, ValueError):
        return []


def _org_component(real: str) -> Protected | None:
    """A path with a nable.org directory in it, wherever it is: the org model
    of the repo that directory is in, or of one the agent is about to make."""
    parts = real.split(os.sep)
    if ORG_DIR_NAME not in parts:
        return None
    d = os.sep.join(parts[:parts.index(ORG_DIR_NAME) + 1])
    return Protected(d, _shown(Path(d)), "a repo's org model", True)


def resolve(path: str, cwd: str | None = None, *,
            env: Mapping[str, str] | None = None) -> str | None:
    """`path` as the shell or an editor would open it from `cwd`: variables
    and `~` expanded, relative to `cwd`, symlinks resolved. None when it
    cannot be resolved (an unset variable, a NUL byte)."""
    if not isinstance(path, str) or not path or "\0" in path:
        return None
    p = _expand(path, env)
    if p is None:
        return None
    if not os.path.isabs(p):
        base = cwd or os.getcwd()
        p = os.path.join(os.path.expanduser(base), p)
    charge(1 + p.count(os.sep) // _UNIT_PARTS)
    try:
        return _real(p)
    except (OSError, ValueError):
        return None


def _under(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def is_under(child: str, parent: str) -> bool:
    """Both resolved: `child` is `parent` or inside it."""
    return _under(child, parent)


def _match_glob(pattern: str, cwd: str | None, env: Mapping[str, str] | None,
                entries: list[Protected], ancestors: bool) -> Protected | None:
    """A glob (`nable.org/*.yaml`, `~/.fin*/guard-off`) that could name a
    protected path. The fixed directory in front of the first wildcard is
    resolved; the rest is matched a path component at a time, as the shell
    expands it. A glob that names a protected file literally
    (`~/.fin*/guard-off`) matches it whether or not it is there; one that
    reaches it only through a wildcard (`rm -rf *`, `rm build/*`) matches
    only what is there, since the shell expands a wildcard to nothing else."""
    p = _expand(pattern, env)
    if p is None:
        return None
    if not os.path.isabs(p):
        p = os.path.join(os.path.expanduser(cwd or os.getcwd()), p)
    parts = p.split(os.sep)
    i = next(n for n, part in enumerate(parts) if _GLOB_CHARS & set(part))
    fixed = os.sep.join(parts[:i]) or os.sep
    charge(1 + p.count(os.sep) // _UNIT_PARTS)
    try:
        fixed = _real(fixed)
    except (OSError, ValueError):
        return None
    rest: list[str] = []
    for c in parts[i:]:
        if c == "..":
            # `*/../.claude` is .claude beside what `*` matched.
            if rest:
                rest.pop()
            else:
                fixed = os.path.dirname(fixed)
        elif c and c != ".":
            rest.append(c.casefold() if _FOLD else c)
    if not rest:
        return _match_one(fixed, cwd, env, entries, ancestors)
    full = os.path.join(fixed, *rest)
    org = _org_component(full)
    if org is not None:
        return org
    literal = not _GLOB_CHARS & set(rest[-1])
    for e in entries:
        if e.tree and _under(fixed, e.path):
            return e                    # whatever it matches is inside a protected tree
        if e.path == fixed or not _under(e.path, fixed):
            continue
        comps = e.path[len(fixed.rstrip(os.sep)) + 1:].split(os.sep)
        n = min(len(comps), len(rest))
        if not all(fnmatch.fnmatchcase(c, r) for c, r in zip(comps[:n], rest[:n])):
            continue
        if e.whole:
            if ancestors and len(comps) >= len(rest) and os.path.lexists(e.path):
                return e
        elif len(comps) == len(rest):
            if literal or os.path.lexists(e.path):
                return e
        elif len(comps) > len(rest):
            # The glob matches a directory the entry is in: `rm -rf ~/.fin*`.
            if ancestors and os.path.lexists(e.path):
                return e
        elif e.tree and os.path.lexists(e.path):
            return e                    # it reaches inside a protected tree: `*/policy.yaml`
    if literal:
        what = PROTECTED_NAMES.get(rest[-1])
        return Protected(full, _shown(Path(full)), what) if what else None
    # A wildcard name (`config/*.yaml`) is a budget file only when the glob
    # expands to one: `rm -rf build/*` is not a write to budget.yml.
    for name, what in PROTECTED_NAMES.items():
        if fnmatch.fnmatchcase(name, rest[-1]):
            found = _glob_first(fixed, [*rest[:-1], name])
            if found is not None:
                shown = found if found != fixed else full
                return Protected(shown, _shown(Path(shown)), what)
    return None


def match(path: str, cwd: str | None = None, *, entries: list[Protected] | None = None,
          ancestors: bool = False, env: Mapping[str, str] | None = None) -> Protected | None:
    """The protected entry `path` is, or is inside; with `ancestors`, also
    one it contains that is there (`rm -rf ~/.finops` takes the ledger with
    it; `chmod -R 755 .` in a repo without nable.org/ changes none). Any path
    through a directory named nable.org is protected too. `env` holds
    variables assigned earlier on a command line. Braces and globs are read
    as the shell would expand them. None when it is none of them."""
    entries = entries if entries is not None else protected(cwd)
    for one in _all_braces(path, entries) if "{" in path else (path,):
        if _GLOB_CHARS & set(one):
            hit = _match_glob(one, cwd, env, entries, ancestors)
        else:
            hit = _match_one(one, cwd, env, entries, ancestors)
        if hit is not None:
            return hit
    return None


def match_file(path: str, cwd: str | None = None) -> Protected | None:
    """The protected entry a file nable itself is about to write is, or is
    inside: `path` as open() reads it (`~` expanded, relative to `cwd`,
    symlinks resolved, case folded where the disk folds it), with no shell
    variables or globs read into it. None when it is none of them."""
    if not isinstance(path, str) or not path or "\0" in path:
        return None
    p = os.path.join(os.path.expanduser(cwd or os.getcwd()), os.path.expanduser(path))
    try:
        real = _real(p)
    except (OSError, ValueError):
        return None
    return _match_real(real, protected(cwd), False)


def _match_one(path: str, cwd: str | None, env: Mapping[str, str] | None,
               entries: list[Protected], ancestors: bool) -> Protected | None:
    real = resolve(path, cwd, env=env)
    if real is None:
        return None
    return _match_real(real, entries, ancestors)


def _match_real(real: str, entries: list[Protected], ancestors: bool) -> Protected | None:
    for e in entries:
        if e.whole:
            if ancestors and _under(e.path, real) and os.path.lexists(e.path):
                return e
            continue
        if real == e.path or (e.tree and _under(real, e.path)):
            return e
        # A recursive change reaches only what is there: a repo's nable.org/
        # that does not exist is not in `rm -rf .`, and not the path to name.
        # (A directory that is not there either, `rm -rf ~/.cursor`, is
        # named for what it would hold.)
        if ancestors and _under(e.path, real) and (
                os.path.lexists(e.path) or not os.path.lexists(real)):
            return e
    org = _org_component(real)
    if org is not None:
        return org
    name = os.path.basename(real)
    what = PROTECTED_NAMES.get(name.casefold() if _FOLD else name)
    if what:
        return Protected(real, _shown(Path(real)), what)
    return None


# File names specific enough to nable that code naming one is taken to mean
# that file: `Path.home() / ".finops" / "guard-off"` in a one-liner has no
# path in it to resolve, but it has the name.
DISTINCTIVE_NAMES = frozenset({"guard-off", POLICY_FILE_NAME, ORG_DIR_NAME, "ai-budget.json",
                               "tag_rules.yaml", "nable-guard.json", "trusted-repos.json",
                               *PROTECTED_NAMES, *(n for n, _ in _LEDGER_FILES),
                               *(n for n, _ in _LEDGER_DIRS)})
