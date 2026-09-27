# SPDX-License-Identifier: Apache-2.0
"""Where the org model lives, how it is read, and the only code that writes it.

Resolution, first match wins:
  1. an explicit dir (the API's `dir=`, the CLI's --dir)
  2. FINOPS_ORG_DIR
  3. `nable.org/` at the root of the git repo holding the working directory,
     only when that directory already exists
  4. <nable data dir>/org/, the same data dir the guard ledger uses

A repo's `nable.org/` is read over the data dir's model, not instead of it:
the person's own facts still apply in a repo that ships its own. And a repo
dir is somebody else's until a person trusts it (`nable org trust --here`,
or `nable org init --here`, which records the repo's root and remote in
<data dir>/org/trusted-repos.json). Until then its facts are read, cited as
"likely", and never pick the guard's team, and its thresholds may only lower
a figure: cloning a repo must not raise what an agent may run unasked.

Nothing creates `nable.org/` in a repo except `nable org init --here`: a tool
that litters the repo it was run in gets uninstalled, and the repo is the
customer's.

Writes: one file per fact kind, facts sorted by (kind, subject kind, subject
id), written to a temp file in the same directory and swapped in with
os.replace, so a crash leaves the old file and never half a new one. Entries
the loader could not read are written back as they were: a fact nobody could
parse is still somebody's fact. A file that is not valid YAML at all is never
rewritten. Plain PyYAML does not round-trip comments: the comment block at
the top of a file is kept, and so are the comment lines just above an entry
(it moves with its entry when the file is sorted); a comment inside an entry
is not.

Confirming, rejecting and stating a fact take a HumanDecision, which only
the `nable org` CLI makes: the Python API is not a way around the terminal.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .adapters import ADAPTERS
from .model import (
    FILE_FOR_KIND,
    HEADER,
    KNOWN_FILES,
    Fact,
    FactError,
    OrgModel,
    _warn,
    local_today,
    sort_key,
    subject_of,
    validate_value,
)

ORG_DIR_NAME = "nable.org"
ENV_DIR = "FINOPS_ORG_DIR"
TRUST_FILE = "trusted-repos.json"
DEFAULT_REVIEW_DAYS = 180


class OrgError(Exception):
    """A write that cannot be done safely, or a key that names no fact."""


# ── the human ─────────────────────────────────────────────────────────────────

_MINT = object()


class HumanDecision:
    """A person's decision, as the `nable org` CLI records it: who (from
    --as, or on a terminal git's user.email, then $USER) and how they were
    named. confirm(), reject(), confirm_many(), reject_many(), set_fact() and
    trust() take one instead of a name, and only cli._who makes one, so a
    script or an agent calling the Python API cannot sign a person's name
    without going through the command a person runs."""

    __slots__ = ("how", "who")

    def __init__(self, who: str, how: str, *, _token: object = None) -> None:
        if _token is not _MINT:
            raise OrgError("a HumanDecision is made by the `nable org` CLI only")
        who = (who or "").strip()
        if not who:
            raise OrgError("a human decision needs a name (confirmed_by)")
        self.who, self.how = who, how

    def __str__(self) -> str:
        return self.who

    def __repr__(self) -> str:
        return f"HumanDecision({self.who!r}, {self.how!r})"


def _human(by: Any) -> str:
    if not isinstance(by, HumanDecision):
        raise OrgError("confirming, rejecting or stating an org fact is a person's decision: "
                       "run `nable org confirm|reject|set` in a terminal (or with --as WHO). "
                       "The Python API takes the HumanDecision that command makes, not a name.")
    return by.who


# ── where ─────────────────────────────────────────────────────────────────────

def git_root(start: str | os.PathLike | None = None) -> Path | None:
    """The nearest directory at or above `start` holding a .git entry (a
    directory, or a file in a worktree). No subprocess: this is on the path
    of the guard hook."""
    try:
        p = Path(start or os.getcwd()).resolve()
    except OSError:
        return None
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return d
    return None


def repo_path_of(path: str | os.PathLike | None = None) -> str | None:
    """`path` (default: the working directory) relative to its git root, in
    the form a repo_path subject uses ("." for the root). None outside a repo."""
    root = git_root(path)
    if root is None:
        return None
    try:
        rel = Path(path or os.getcwd()).resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    return rel.as_posix() if str(rel) != "." else "."


def _git_dir(root: Path) -> Path | None:
    g = root / ".git"
    if g.is_dir():
        return g
    try:
        text = g.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("gitdir:"):
            p = Path(line[len("gitdir:"):].strip())
            return p if p.is_absolute() else (root / p).resolve()
    return None


def remote_url(root: Path) -> str:
    """The repo's origin URL (else its first remote's), read from .git/config
    without running git; "" when it has none."""
    gd = _git_dir(root)
    if gd is None:
        return ""
    common = gd
    with contextlib.suppress(OSError):
        common = (gd / (gd / "commondir").read_text(encoding="utf-8").strip()).resolve()
    try:
        text = (common / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    remotes: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            m = re.match(r'\[\s*remote\s+"([^"]*)"\s*\]', s)
            current = m.group(1) if m else None
        elif current is not None and "=" in s:
            k, _, v = s.partition("=")
            if k.strip().lower() == "url":
                remotes.setdefault(current, v.strip().strip('"'))
    return remotes.get("origin") or next(iter(remotes.values()), "")


def repo_slug(url: str) -> str:
    """"github.com/acme/payments" for any spelling of that remote (https,
    ssh, scp-like, with or without .git); "" for no URL."""
    u = (url or "").strip()
    if not u:
        return ""
    if "://" in u:
        u = u.split("://", 1)[1]
        host, _, path = u.partition("/")
        host = host.rsplit("@", 1)[-1].split(":", 1)[0]
        u = f"{host}/{path}"
    else:
        m = re.match(r"^(?:[^@/]+@)?([^:/]+):(.+)$", u)
        if m:
            u = f"{m.group(1)}/{m.group(2)}"
    u = re.sub(r"/+", "/", u.replace("\\", "/")).strip("/")
    u = u.removesuffix(".git")
    return u.strip("/").lower()


def repo_identity(root: Path) -> str:
    """What a repo_path id names a repo by: its remote's slug, else the
    name of its root directory."""
    return repo_slug(remote_url(root)) or root.name.lower()


def repo_subject(path: str | os.PathLike | None = None, *, bare: bool = False) -> str | None:
    """"repo_path:<repo>//<path>" for `path` (default: the working
    directory), or "repo_path:<path>" with bare=True. None outside a repo."""
    root = git_root(path)
    rel = repo_path_of(path)
    if root is None or rel is None:
        return None
    return f"repo_path:{rel}" if bare else f"repo_path:{repo_identity(root)}//{rel}"


def _data_dir() -> Path:
    # Imported here: guard_ledger compiles its redaction patterns on import,
    # and a caller with FINOPS_ORG_DIR set or a repo dir should not pay for it.
    from ..guard_ledger import _data_dir as ledger_data_dir
    return ledger_data_dir()


def resolve_dir(dir: str | os.PathLike | None = None, *,
                cwd: str | os.PathLike | None = None) -> tuple[Path, str]:
    """(org dir, which rule chose it): argument | FINOPS_ORG_DIR | repo | data_dir.
    `cwd` is the directory to look for a repo from (default: this process's).
    Creates nothing except the nable data dir itself (as the ledger does)."""
    if dir:
        return Path(dir).expanduser(), "argument"
    env = os.environ.get(ENV_DIR, "").strip()
    if env:
        return Path(env).expanduser(), ENV_DIR
    root = git_root(cwd)
    if root is not None and (root / ORG_DIR_NAME).is_dir():
        return root / ORG_DIR_NAME, "repo"
    return _data_dir() / "org", "data_dir"


def _anchor(d: Path) -> str | None:
    """The repo a bare repo_path in dir `d` is about: only a nable.org/ at a
    repo's root has one."""
    try:
        if d.name == ORG_DIR_NAME and (d.parent / ".git").exists():
            return repo_identity(d.parent)
    except OSError:
        pass
    return None


# ── trust ─────────────────────────────────────────────────────────────────────

def trust_path() -> Path:
    """Beside the data dir's org model, so the guard protects it the same way."""
    return _data_dir() / "org" / TRUST_FILE


def trusted_repos() -> list[dict[str, str]]:
    try:
        doc = json.loads(trust_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = doc.get("repos") if isinstance(doc, dict) else None
    return [r for r in rows or [] if isinstance(r, dict) and isinstance(r.get("root"), str)]


def _trust_key(root: Path) -> tuple[str, str]:
    return str(root.resolve()), remote_url(root)


def is_trusted(root: Path | None) -> bool:
    """Whether a person trusted this repo's nable.org/: its root path and its
    remote both as recorded (a different clone at that path, or the same path
    pointed at another remote, is not the repo they trusted)."""
    if root is None:
        return False
    where, remote = _trust_key(root)
    return any(r.get("root") == where and r.get("remote", "") == remote
               for r in trusted_repos())


def trust(root: Path, by: HumanDecision, *, revoke: bool = False) -> bool:
    """Record (or with revoke, remove) a person's trust in `root`'s
    nable.org/. Returns whether anything changed."""
    who = _human(by)
    where, remote = _trust_key(root)
    rows = [r for r in trusted_repos() if r.get("root") != where]
    changed = len(rows) != len(trusted_repos())
    if not revoke:
        rows.append({"root": where, "remote": remote, "by": who, "at": _today()})
        changed = True
    if changed:
        p = trust_path()
        _atomic_write(p, json.dumps({"repos": rows}, indent=2) + "\n")
    return changed


@dataclass
class Layer:
    """One directory the model is read from. `layer` is its precedence: the
    directory the working directory chose (2) over the data dir's model read
    under a repo's (1)."""
    dir: Path
    source: str
    trusted: bool = True
    anchor: str | None = None
    layer: int = 2
    root: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"dir": str(self.dir), "source": self.source, "trusted": self.trusted,
                "repo": self.anchor}


def layers(dir: str | os.PathLike | None = None, *,
           cwd: str | os.PathLike | None = None) -> list[Layer]:
    """The directories load() reads, the chosen one first. A repo's
    nable.org/ has the data dir's model under it, and is trusted only when a
    person said so."""
    d, why = resolve_dir(dir, cwd=cwd)
    if why != "repo":
        return [Layer(d, why, True, _anchor(d), 2)]
    root = d.parent
    out = [Layer(d, why, is_trusted(root), _anchor(d), 2, root)]
    under = _data_dir() / "org"
    if under.resolve() != d.resolve():
        out.append(Layer(under, "data_dir", True, _anchor(under), 1))
    return out


# ── read ──────────────────────────────────────────────────────────────────────

def safe_yaml(text: str) -> Any:
    """yaml.safe_load, through libyaml's CSafeLoader where PyYAML has it: the
    same safe constructor on a C parser, about ten times faster. The guard
    hook reads the model inside its ~100 ms, and the pure-Python parser took
    about 100 ms for a few hundred facts on its own."""
    import yaml
    try:
        from yaml import CSafeLoader as SafeLoader
    except ImportError:                  # PyYAML built without libyaml
        from yaml import SafeLoader
    return yaml.load(text, Loader=SafeLoader)


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


class _Verbatim(str):
    """An entry's text as it was in the file: how an entry the loader could
    not read is written back (a re-dump would turn an octal-looking account
    id into another number)."""


def _entry_comments(lines: list[str], n: int
                    ) -> tuple[list[str], list[list[str]], list[str]] | None:
    """(header, [comment lines above entry i], [entry i's own text]) when
    the file is a block list whose top-level "- " lines number `n` (the
    entries parsed), else None. The header is the top comment block up to its
    last blank line; the comments after that blank line, and every column-0
    comment run just above a later entry, belong to the entry below them."""
    starts = [i for i, line in enumerate(lines) if line.startswith("-")
              and (len(line) == 1 or line[1] in " \t")]
    if len(starts) != n or n == 0:
        return None
    top = lines[:starts[0]]
    if any(line.strip() and not _is_comment(line) for line in top):
        return None
    blanks = [i for i, line in enumerate(top) if not line.strip()]
    cut = blanks[-1] + 1 if blanks else len(top)
    header = [line.rstrip() for line in top[:cut]]
    comments: list[list[str]] = [[line.rstrip() for line in top[cut:] if line.strip()]]
    ends: list[int] = []
    for s in starts[1:]:
        run: list[str] = []
        i = s - 1
        while i >= 0 and (not lines[i].strip() or lines[i].startswith("#")):
            if lines[i].strip():
                run.append(lines[i].rstrip())
            i -= 1
        comments.append(list(reversed(run)))
        ends.append(i + 1)
    ends.append(len(lines))
    bodies = ["\n".join(line.rstrip() for line in lines[a:b]).rstrip()
              for a, b in zip(starts, ends, strict=True)]
    return header, comments, bodies


class _File:
    """One org file as read: its top comment block, its facts, the entries
    that did not parse (kept verbatim), and whether it is safe to rewrite.

    Each fact read from a block list keeps its own text (`_text`), and is
    written back as that text while nothing changed it: a person's layout
    stays, and a file of thousands of facts is not dumped again to add one.
    A file nable parsed before is read from the parse cache (keyed on its
    content), so a proposal does not parse thousands of facts either."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.header: list[str] = []
        self.facts: list[Fact] = []
        self.unparsed: list[tuple[Any, list[str]]] = []
        self.why: list[str] = []          # why each unparsed entry did not parse
        self.error: str | None = None
        self.exists = path.exists()

    def load(self, warnings: list[str] | None = None) -> _File:
        if not self.exists:
            return self
        import yaml
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            self.error = f"{self.path.name}: not readable as YAML ({type(e).__name__}); ignored"
            _warn(warnings, self.error)
            return self
        # A Windows editor's byte-order mark is not part of the header.
        text = text.removeprefix("\ufeff")
        digest = _digest(text)
        if self._from_cache(digest, warnings):
            return self
        try:
            data = safe_yaml(text)
        except yaml.YAMLError as e:
            self.error = f"{self.path.name}: not readable as YAML ({type(e).__name__}); ignored"
            _warn(warnings, self.error)
            return self
        if data is not None and not isinstance(data, list):
            self.error = f"{self.path.name}: expected a list of facts; ignored"
            _warn(warnings, self.error)
            return self
        lines = text.splitlines()
        entries = data or []
        split = _entry_comments(lines, len(entries))
        bodies: list[str | None] = [None] * len(entries)
        if split is not None:
            header, comments, texts = split
            bodies = list(texts)
        else:
            header, comments = [], [[] for _ in entries]
            for line in lines:
                if _is_comment(line) or not line.strip():
                    header.append(line.rstrip())
                    continue
                break
        self.header = [h for h in header if h.strip() != HEADER]
        while self.header and not self.header[-1].strip():
            self.header.pop()
        while self.header and not self.header[0].strip():
            self.header.pop(0)
        skipped: list[str] = []
        for i, raw in enumerate(entries):
            try:
                f = Fact.from_dict(raw, origin=self.path.name)
                f.comments = comments[i]
                if bodies[i]:
                    f._text = bodies[i]  # type: ignore[attr-defined]
                self.facts.append(f)
            except Exception as e:  # noqa: BLE001 - kept verbatim, never dropped
                body = bodies[i]
                self.unparsed.append((_Verbatim(body) if body else raw, comments[i]))
                reason = str(e) if isinstance(e, FactError) else f"{type(e).__name__}: {e}"
                self.why.append(reason)
                skipped.append(f"{self.path.name}[{i}]: skipped, {reason}")
        for msg in skipped:
            _warn(warnings, msg)
        _cache_put(self.path, digest, self.header, self.facts, self.unparsed, skipped,
                   self.why)
        return self

    def _from_cache(self, digest: str, warnings: list[str] | None) -> bool:
        doc = _cache_get(self.path, digest)
        if doc is None:
            return False
        try:
            facts: list[Fact] = []
            for row, comments, body in doc["facts"]:
                f = Fact.from_cache(row, origin=self.path.name)
                f.comments = list(comments)
                if body:
                    f._text = body  # type: ignore[attr-defined]
                facts.append(f)
            unparsed = [(_Verbatim(u) if how == "text" else u, list(c))
                        for how, u, c in doc["unparsed"]]
            header = [str(h) for h in doc["header"]]
            skipped = [str(w) for w in doc["warnings"]]
            why = [str(w) for w in doc["why"]]
        except (KeyError, TypeError, ValueError):
            return False
        self.header, self.facts, self.unparsed, self.why = header, facts, unparsed, why
        for msg in skipped:
            _warn(warnings, msg)
        return True


# ── the parse cache ───────────────────────────────────────────────────────────
#
# Parsing YAML is most of what reading a model of thousands of facts costs.
# Each org file's parsed form is kept beside the guard ledger, keyed on the
# file's path and a hash of its content, so a changed file is always parsed
# again. Derived data only: a missing, stale or broken entry is a parse.

_CACHE_DIR = "org-parse-cache"
_CACHE_VERSION = 1


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()


def _cache_file(path: Path) -> Path | None:
    try:
        from ..guard_ledger import ledger_path
        where = str(path.resolve())
        return ledger_path().parent / _CACHE_DIR / (_digest(where)[:20] + ".json")
    except Exception:  # noqa: BLE001 - no cache is a parse, nothing more
        return None


def _cache_get(path: Path, digest: str) -> dict[str, Any] | None:
    p = _cache_file(path)
    if p is None:
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("v") != _CACHE_VERSION \
            or doc.get("path") != str(path.resolve()) or doc.get("digest") != digest:
        return None
    return doc


def _cache_put(path: Path, digest: str, header: list[str], facts: list[Fact],
               unparsed: list[tuple[Any, list[str]]], warnings: list[str],
               why: list[str]) -> None:
    p = _cache_file(path)
    if p is None:
        return
    try:
        body = json.dumps({
            "v": _CACHE_VERSION, "path": str(path.resolve()), "digest": digest,
            "header": header, "warnings": warnings, "why": why,
            "facts": [[f.to_cache(), f.comments, getattr(f, "_text", None)] for f in facts],
            "unparsed": [["text" if isinstance(u, _Verbatim) else "raw", u, c]
                         for u, c in unparsed]})
    except (TypeError, ValueError):
        return        # something in it is not JSON (a date in an extra field): no cache
    tmp = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, p)
    except OSError:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _files(d: Path, warnings: list[str] | None = None) -> dict[str, _File]:
    return {name: _File(d / name).load(warnings) for name in KNOWN_FILES}


def _read_dir(d: Path, warnings: list[str]) -> dict[str, _File]:
    """The org files in `d`, read, and a warning for any other YAML file."""
    if not d.is_dir():
        return {}
    for name in sorted(os.listdir(d)):
        if name.endswith((".yaml", ".yml")) and not name.startswith(".") \
                and name not in KNOWN_FILES:
            _warn(warnings, f"{name}: not an org model file ({', '.join(KNOWN_FILES)}); ignored")
    return {name: _File(d / name).load(warnings) for name in KNOWN_FILES
            if (d / name).exists()}


def _assemble(read: list[tuple[Layer, dict[str, _File]]], warnings: list[str], *,
              legacy: bool) -> list[Fact]:
    """The model's facts from each layer's files, the first layer first, and
    the legacy facts under them all."""
    facts: list[Fact] = []
    seen: dict[str, int] = {}
    live_legacy: set[str] = set()
    if legacy:
        from .legacy import live_sources
        live_legacy = live_sources()
    for n, (layer, files) in enumerate(read):
        for fl in files.values():
            for f in fl.facts:
                if f.key in seen:
                    if seen[f.key] == n:
                        _warn(warnings, f"{f.origin}: fact {f.key} appears more than once; "
                                        "the first is used")
                    continue
                if f.live and f.source in live_legacy:
                    # A copy `nable org init` imported from a legacy file that
                    # is still there: the file itself answers, as edited since.
                    continue
                seen[f.key] = n
                f.src_dir = str(layer.dir)
                f.trusted = layer.trusted
                f.anchor = layer.anchor
                f.layer = layer.layer
                facts.append(f)
    if legacy:
        from .legacy import legacy_facts
        held = {f.slot for f in facts if f.confirmed}
        for f in legacy_facts(warnings=warnings):
            if f.key in seen or f.slot in held:
                continue
            seen[f.key] = -1
            facts.append(f)
    return facts


def load(dir: str | os.PathLike | None = None, *, legacy: bool = True,
         cwd: str | os.PathLike | None = None) -> OrgModel:
    """The org model. Never raises for bad content: a bad entry is skipped
    with a warning naming its file and index (also in OrgModel.warnings).

    In a repo with its own nable.org/, the data dir's model is read under it
    (see layers()). Legacy facts (tag_rules.yaml, accounts.yaml,
    FINOPS_REQUIRED_TAGS and FINOPS_PROTECTED_TAGS) are merged under the file
    facts: one is dropped when a file fact has its key, or a confirmed file
    fact holds its slot. A file fact imported from a legacy source that still
    exists gives way to that source."""
    ls = layers(dir, cwd=cwd)
    warnings: list[str] = []
    read = [(layer, _read_dir(layer.dir, warnings)) for layer in ls]
    facts = _assemble(read, warnings, legacy=legacy)
    return OrgModel(facts, dir=ls[0].dir, dir_source=ls[0].source, warnings=warnings,
                    layers=ls)


# ── write ─────────────────────────────────────────────────────────────────────

_DUMPER: Any = None


def _dumper() -> Any:
    """A safe dumper that writes subject and value in flow style, on
    libyaml's emitter where PyYAML has it (a few thousand facts in tens of
    milliseconds, not seconds)."""
    global _DUMPER
    if _DUMPER is None:
        try:
            from yaml import CSafeDumper as Base
        except ImportError:
            from yaml import SafeDumper as Base

        class _Dumper(Base):  # type: ignore[misc, valid-type]
            pass

        _Dumper.add_representer(
            _Flow, lambda dumper, data: dumper.represent_mapping(
                "tag:yaml.org,2002:map", data, flow_style=True))
        _DUMPER = _Dumper
    return _DUMPER


class _Flow(dict):
    pass


def _shaped(d: dict[str, Any]) -> dict[str, Any]:
    out = dict(d)
    for k in ("subject", "value"):
        if isinstance(out.get(k), dict):
            out[k] = _Flow(out[k])
    for k in ("proposed_at", "confirmed_at", "review_after"):
        if isinstance(out.get(k), str):
            with contextlib.suppress(ValueError):
                out[k] = date.fromisoformat(out[k])
    return out


def _yaml_text(entries: list[Any], header: Iterable[str] = (),
               comments: list[list[str]] | None = None) -> str:
    """The file text: the header line, the kept header comments, then the
    entries, each after its own comment lines."""
    import yaml

    def dump(chunk: list[Any]) -> str:
        return yaml.dump([_shaped(e) if isinstance(e, dict) else e for e in chunk],
                         Dumper=_dumper(), sort_keys=False, default_flow_style=False,
                         allow_unicode=True, width=10_000)

    lines = [HEADER, *[h for h in header if h.strip() != HEADER]]
    if not entries:
        return "\n".join(lines) + "\n[]\n"
    comments = comments or [[] for _ in entries]
    parts: list[str] = []
    chunk: list[Any] = []
    for e, c in zip(entries, comments, strict=True):
        if (c or isinstance(e, _Verbatim)) and chunk:
            parts.append(dump(chunk))
            chunk = []
        if c:
            parts.append("\n".join(c) + "\n")
        if isinstance(e, _Verbatim):
            parts.append(e + "\n")
            continue
        chunk.append(e)
    if chunk:
        parts.append(dump(chunk))
    return "\n".join(lines) + "\n" + "".join(parts)


def _atomic_write(path: Path, text: str) -> None:
    """Temp file in the same directory, fsync, os.replace. Keeps the file's
    mode when it exists; a new file gets 0644 under the umask (it is meant to
    be committed and read, not a secret)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
    else:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write_file(f: _File) -> None:
    if f.error:
        raise OrgError(f"{f.error}; fix or move it before nable writes to it")
    facts = sorted(f.facts, key=sort_key)
    # A fact nothing changed is written as the text it was read as.
    entries: list[Any] = [_Verbatim(x._text) if getattr(x, "_text", None) else x.to_dict()
                          for x in facts]
    comments = [list(x.comments) for x in facts]
    for raw, c in f.unparsed:
        entries.append(raw)
        comments.append(list(c))
    text = _yaml_text(entries, f.header, comments)
    _atomic_write(f.path, text)
    f.facts = facts
    split = _entry_comments(text.splitlines(), len(entries))
    if split is not None:
        for x, body in zip(facts, split[2], strict=False):
            x._text = body  # type: ignore[attr-defined]
    skipped = [f"{f.path.name}[{len(facts) + j}]: skipped, {why}" for j, why in enumerate(f.why)]
    _cache_put(f.path, _digest(text), list(f.header), facts, f.unparsed, skipped, list(f.why))


@contextlib.contextmanager
def _locked(d: Path) -> Iterator[None]:
    """Serialise writers on the directory itself (flock on its fd), so no
    lock file lands in the customer's repo. Best effort where flock is absent."""
    d.mkdir(parents=True, exist_ok=True)
    fd = None
    try:
        import fcntl
        fd = os.open(d, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except (ImportError, OSError):
        if fd is not None:
            os.close(fd)
        fd = None
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)


def _today() -> str:
    return local_today().isoformat()


def ensure_dir(d: Path) -> list[Path]:
    """Create the org dir and any missing file, header only. Returns the
    files created."""
    made: list[Path] = []
    d.mkdir(parents=True, exist_ok=True)
    for name in KNOWN_FILES:
        p = d / name
        if not p.exists():
            _atomic_write(p, _yaml_text([]))
            made.append(p)
    return made


def _proposal(fact: Fact) -> Fact:
    """The fact as a proposer may write it: proposed, no human fields."""
    subject = subject_of(fact.subject.to_dict())
    value = validate_value(fact.fact, subject, fact.value)
    if not isinstance(fact.source, str) or not fact.source.strip():
        raise FactError("source must be a non-empty string")
    conf = min(max(float(fact.confidence), 0.0), 1.0)
    return replace(fact, subject=subject, value=value, status="proposed", confidence=conf,
                   proposed_at=fact.proposed_at or _today(), confirmed_by=None,
                   confirmed_at=None, origin=None)


def propose(fact: Fact, dir: str | os.PathLike | None = None) -> str:
    """Add a proposal. Returns:
      "added"               written, status proposed
      "duplicate"           the same fact is already proposed or confirmed
      "suppressed_rejected" a human rejected this fact (the same slot and the
                            same thing said, whatever its spelling); not
                            proposed again
      "conflict"            written as a proposal beside a confirmed fact that
                            says otherwise; the confirmed one keeps answering

    Whatever status the caller set, the fact is written as proposed."""
    new = _proposal(fact)
    return _propose_all([new], dir)[0]


def propose_many(facts: Iterable[Fact], dir: str | os.PathLike | None = None) -> list[str]:
    """propose() for many facts under one lock and one write per file, in
    order, with the same answers; a fact that does not fit the schema answers
    "invalid" instead of raising. What adapters use: a few hundred proposals
    through propose() would re-read the whole directory for each one."""
    news: list[Fact | None] = []
    for f in facts:
        try:
            news.append(_proposal(f))
        except (FactError, TypeError, ValueError):
            news.append(None)
    return _propose_all(news, dir)


def _one_layer(d: Path, why: str = "argument") -> Layer:
    return Layer(d, why, True, _anchor(d), 2)


def _propose_all(news: list[Fact | None], dir: str | os.PathLike | None) -> list[str]:
    d, why = resolve_dir(dir)
    out: list[str] = []
    with _locked(d):
        warnings: list[str] = []
        files = _files(d, warnings)       # read once: the model and the rewrite share it
        model_facts = _assemble([(_one_layer(d, why), files)], warnings, legacy=True)
        statuses: dict[str, set[str]] = {}
        for f in model_facts:
            statuses.setdefault(f.key, set()).add(f.status)
        rejected = {f.said for f in model_facts if f.status == "rejected"}
        confirmed_slots = {f.slot for f in model_facts if f.confirmed}
        where = {f.key: (n, i) for n, fl in files.items() for i, f in enumerate(fl.facts)}
        changed: set[str] = set()
        for new in news:
            if new is None:
                out.append("invalid")
                continue
            same = statuses.get(new.key, set())
            if "rejected" in same or new.said in rejected:
                out.append("suppressed_rejected")
                continue
            if same & {"proposed", "confirmed"}:
                out.append("duplicate")
                continue
            if new.key in where:
                # An expired fact proposed again comes back in place.
                n, i = where[new.key]
                old = files[n].facts[i]
                files[n].facts[i] = replace(new, origin=n, comments=old.comments)
            else:
                n = FILE_FOR_KIND[new.fact]
                files[n].facts.append(replace(new, origin=n))
                where[new.key] = (n, len(files[n].facts) - 1)
            changed.add(n)
            statuses[new.key] = {"proposed"}
            out.append("conflict" if new.slot in confirmed_slots else "added")
        for name in sorted(changed):
            _write_file(files[name])
    return out


def _one(model: OrgModel, key: str) -> Fact:
    found = model.find(key)
    if not found:
        raise OrgError(f"no fact with key {key!r} (see `nable org review`)")
    if len({f.key for f in found}) > 1:
        raise OrgError(f"key {key!r} matches {len(found)} facts; give more characters")
    return found[0]


def _next_review(f: Fact, today: str) -> str | None:
    """A confirmed fact past its review date, confirmed again, is due again
    one of its own intervals from today (confirmed_at to review_after), or
    DEFAULT_REVIEW_DAYS when that is unknown."""
    if not f.review_after or f.review_after >= today:
        return f.review_after
    days = DEFAULT_REVIEW_DAYS
    with contextlib.suppress(TypeError, ValueError):
        span = (date.fromisoformat(f.review_after) - date.fromisoformat(f.confirmed_at or "")).days
        if span > 0:
            days = span
    return (date.fromisoformat(today) + timedelta(days=days)).isoformat()


def _decide(key: str, by: HumanDecision, status: str, dir: str | os.PathLike | None) -> Fact:
    return _decide_many([key], by, status, dir)[0]


def _decide_many(keys: list[str], by: HumanDecision, status: str,
                 dir: str | os.PathLike | None) -> list[Fact]:
    """One human decision over several facts: one lock, one read, one write
    per file. Every key is checked before anything is written, so a bad key
    leaves the files as they were. A fact is decided in the directory it was
    read from (a repo's nable.org/, or the data dir's model under it)."""
    who = _human(by)
    today = _today()
    d, _ = resolve_dir(dir)
    with contextlib.ExitStack() as stack:
        stack.enter_context(_locked(d))
        model = load(dir)
        targets: list[Fact] = []
        for key in keys:
            t = _one(model, key)
            if all(t.key != x.key for x in targets):
                targets.append(t)
        files_in: dict[Path, dict[str, _File]] = {d: _files(d)}

        def files_for(f: Fact) -> tuple[Path, dict[str, _File]]:
            home = Path(f.src_dir) if f.src_dir and f.origin != "legacy" else d
            if home not in files_in:
                stack.enter_context(_locked(home))
                files_in[home] = _files(home)
            return home, files_in[home]

        changed: set[tuple[Path, str]] = set()
        out: list[Fact] = []
        for target in targets:
            home, files = files_for(target)
            if target.origin == "legacy":
                if status == "confirmed":
                    out.append(target)    # a human wrote the legacy file: already confirmed
                    continue
                decided = replace(target, status=status, confirmed_by=who,
                                  confirmed_at=today, origin=None)
                name = FILE_FOR_KIND[decided.fact]
                files[name].facts.append(replace(decided, origin=name))
                changed.add((home, name))
                out.append(decided)
                continue
            review = _next_review(target, today) if status == "confirmed" else \
                target.review_after
            decided = replace(target, status=status, confirmed_by=who, confirmed_at=today,
                              review_after=review)
            changed |= {(home, n) for n in _replace_in_files(
                files, decided, supersede=status == "confirmed", write=False)}
            out.append(decided)
        for home, name in sorted(changed):
            _write_file(files_in[home][name])
        return out


def _replace_in_files(files: dict[str, _File], fact: Fact, *, supersede: bool,
                      write: bool = True) -> set[str]:
    """Put `fact` in place of the entry with its key (or add it), and, when
    it is a confirmed fact, expire the other confirmed facts in its slot: two
    confirmed answers to one question is a question nobody can answer.
    Returns the files changed (written unless write=False)."""
    changed: set[str] = set()
    placed = False
    for name, fl in files.items():
        for i, f in enumerate(fl.facts):
            if f.key == fact.key:
                if not placed:
                    fl.facts[i] = replace(fact, origin=name,
                                          comments=fact.comments or f.comments)
                    placed = True
                    changed.add(name)
            elif supersede and f.confirmed and f.slot == fact.slot:
                fl.facts[i] = replace(f, status="expired")
                changed.add(name)
    if not placed:
        name = FILE_FOR_KIND[fact.fact]
        files[name].facts.append(replace(fact, origin=name))
        changed.add(name)
    if write:
        for name in sorted(changed):
            _write_file(files[name])
    return changed


def confirm(key: str, by: HumanDecision, dir: str | os.PathLike | None = None) -> Fact:
    """A human says yes. `by` is the HumanDecision the CLI made (the CLI
    with a terminal or --as); the MCP server and adapters have none."""
    return _decide(key, by, "confirmed", dir)


def reject(key: str, by: HumanDecision, dir: str | os.PathLike | None = None) -> Fact:
    """A human says no. The fact is kept, so it is never proposed again."""
    return _decide(key, by, "rejected", dir)


def confirm_many(keys: Iterable[str], by: HumanDecision,
                 dir: str | os.PathLike | None = None) -> list[Fact]:
    """A human says yes to several facts at once (a bulk question). The same
    human path as confirm(); nothing is written if any key is unknown."""
    return _decide_many(list(keys), by, "confirmed", dir)


def reject_many(keys: Iterable[str], by: HumanDecision,
                dir: str | os.PathLike | None = None) -> list[Fact]:
    """A human says no to several facts at once; each is kept as rejected."""
    return _decide_many(list(keys), by, "rejected", dir)


def set_fact(fact: Fact, by: HumanDecision, dir: str | os.PathLike | None = None) -> Fact:
    """A human states a fact directly: confirmed, confidence 1.0, source
    human unless the caller names one."""
    who = _human(by)
    value = validate_value(fact.fact, fact.subject, fact.value)
    human = replace(fact, value=value, status="confirmed", confidence=1.0,
                    source=fact.source or "human", proposed_at=fact.proposed_at or _today(),
                    confirmed_by=who, confirmed_at=_today(), origin=None)
    d, _ = resolve_dir(dir)
    with _locked(d):
        _replace_in_files(_files(d), human, supersede=True)
    return human


def import_legacy(dir: str | os.PathLike | None = None) -> int:
    """Write the legacy facts into the org files, so the directory holds the
    whole model. Returns how many were added. Not a human decision of its
    own: a person wrote each legacy file."""
    from .legacy import legacy_facts
    d, _ = resolve_dir(dir)
    added = 0
    with _locked(d):
        files = _files(d)
        facts = [f for fl in files.values() for f in fl.facts]
        keys = {f.key for f in facts}
        held = {f.slot for f in facts if f.confirmed}
        changed: set[str] = set()
        for f in legacy_facts():
            if f.key in keys or f.slot in held:
                continue
            name = FILE_FOR_KIND[f.fact]
            files[name].facts.append(replace(f, origin=name))
            keys.add(f.key)
            changed.add(name)
            added += 1
        for name in sorted(changed):
            _write_file(files[name])
    return added


def make_fact(kind: str, subject: Any, value: dict[str, Any], *, source: str,
              confidence: float = 0.5, dollars_monthly: float | None = None,
              review_after: str | None = None) -> Fact:
    """A Fact from loose parts, validated. Raises FactError."""
    return Fact.from_dict({"fact": kind, "subject": subject_of(subject).to_dict(),
                           "value": value, "source": source, "confidence": confidence,
                           "status": "proposed", "dollars_monthly": dollars_monthly,
                           "review_after": review_after})


# ── export ────────────────────────────────────────────────────────────────────

def export(out: Any = None, fmt: str = "yaml", *, model: OrgModel | None = None,
           dir: str | os.PathLike | None = None) -> str:
    """The whole model (file and legacy facts) as YAML (the file format) or
    JSON (with keys). `out`: None for stdout, a path, or anything with write()."""
    if fmt not in ("yaml", "json"):
        raise ValueError("fmt must be yaml or json")
    m = model or load(dir)
    facts = sorted(m.facts, key=sort_key)
    if fmt == "json":
        text = json.dumps([f.summary() for f in facts], indent=2, sort_keys=False) + "\n"
    else:
        text = _yaml_text([f.to_dict() for f in facts])
    if out is None:
        sys.stdout.write(text)
    elif hasattr(out, "write"):
        out.write(text)
    else:
        _atomic_write(Path(out).expanduser(), text)
    return text


# ── adapters ──────────────────────────────────────────────────────────────────

@dataclass
class AdapterRun:
    """What one adapter proposed in one run, and what the store made of it."""
    id: str
    facts: list[Fact] = field(default_factory=list)
    results: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r] = out.get(r, 0) + 1
        return out

    def top(self, n: int = 3) -> list[Fact]:
        """What this run newly wrote (added, or beside a confirmed fact),
        most dollars first."""
        new = [f for f, r in zip(self.facts, self.results, strict=False)
               if r in ("added", "conflict")]
        return sorted(new, key=lambda f: (-(f.dollars_monthly or 0.0), -f.confidence,
                                          str(f.subject)))[:n]


def _pack_adapters() -> list[Any]:
    """Org-context adapters from installed packs (finops.packs.org_adapters):
    each runs out of process through the pack broker and only proposes. A
    broken pack install never stops init; it just adds no adapters."""
    try:
        from .. import packs
        return list(packs.org_adapters())
    except Exception as e:  # noqa: BLE001 - packs are optional
        import logging
        logging.getLogger("finops.org").warning("pack adapters not loaded: %s", e)
        return []


def run_adapters(dir: str | os.PathLike | None = None, *,
                 repos: Iterable[str | os.PathLike] | None = None,
                 data: Any = None, only: Iterable[str] | None = None) -> list[AdapterRun]:
    """Run every registered adapter (org.ADAPTERS), in order, and propose
    what each returns. Each sees the model and what the earlier ones proposed.
    Whatever an adapter sets, its facts are written as proposed. A failing
    adapter is reported, never fatal.

    repos: extra repos to read, after the git repo holding the working
    directory. data: the cost history (an adapters.data.CostData), default
    the local database, read once."""
    from .adapters import AdapterContext, adapter_id
    roots: list[Path] = []
    here = git_root()
    if here is not None:
        roots.append(here)
    for r in repos or ():
        p = Path(r).expanduser()
        root = git_root(p) or (p.resolve() if p.is_dir() else None)
        if root is not None and root not in roots:
            roots.append(root)
    ctx = AdapterContext(model=load(dir), repos=roots, data=data,
                         org_dir=resolve_dir(dir)[0])
    wanted = set(only) if only is not None else None
    runs: list[AdapterRun] = []
    for adapter in [*ADAPTERS, *_pack_adapters()]:
        run = AdapterRun(adapter_id(adapter))
        if wanted is not None and run.id not in wanted:
            continue
        runs.append(run)
        try:
            fn = adapter.load() if hasattr(adapter, "load") else adapter
            facts = [f for f in fn(ctx) if isinstance(f, Fact)]
        except Exception as e:  # noqa: BLE001 - one adapter never stops init
            run.error = f"{type(e).__name__}: {e}"
            continue
        # Only proposals: whatever the adapter set is dropped here, and again
        # in the store. A proposer cannot confirm.
        run.facts = [replace(f, status="proposed", confirmed_by=None, confirmed_at=None)
                     for f in facts]
        run.results = propose_many(run.facts, dir)
        # What later adapters build on: what is in the model as proposed now.
        # A fact a person rejected is not, whatever this adapter thinks.
        ctx.prior.extend(f for f, r in zip(run.facts, run.results, strict=True)
                         if r in ("added", "duplicate", "conflict"))
    return runs
