# SPDX-License-Identifier: Apache-2.0
"""Where the org model lives, how it is read, and the only code that writes it.

Resolution, first match wins:
  1. an explicit dir (the API's `dir=`, the CLI's --dir)
  2. FINOPS_ORG_DIR
  3. `nable.org/` at the root of the git repo holding the working directory,
     only when that directory already exists
  4. <nable data dir>/org/, the same data dir the guard ledger uses

Nothing creates `nable.org/` in a repo except `nable org init --here`: a tool
that litters the repo it was run in gets uninstalled, and the repo is the
customer's.

Writes: one file per fact kind, facts sorted by (kind, subject kind, subject
id), written to a temp file in the same directory and swapped in with
os.replace, so a crash leaves the old file and never half a new one. Entries
the loader could not read are written back as they were: a fact nobody could
parse is still somebody's fact. A file that is not valid YAML at all is never
rewritten. Plain PyYAML does not round-trip comments; the comment block at the
top of a file is kept, comments between facts are not.
"""
from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

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


class OrgError(Exception):
    """A write that cannot be done safely, or a key that names no fact."""


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


def _data_dir() -> Path:
    # Imported here: guard_ledger compiles its redaction patterns on import,
    # and a caller with FINOPS_ORG_DIR set or a repo dir should not pay for it.
    from ..guard_ledger import _data_dir as ledger_data_dir
    return ledger_data_dir()


def resolve_dir(dir: str | os.PathLike | None = None) -> tuple[Path, str]:
    """(org dir, which rule chose it): argument | FINOPS_ORG_DIR | repo | data_dir.
    Creates nothing except the nable data dir itself (as the ledger does)."""
    if dir:
        return Path(dir).expanduser(), "argument"
    env = os.environ.get(ENV_DIR, "").strip()
    if env:
        return Path(env).expanduser(), ENV_DIR
    root = git_root()
    if root is not None and (root / ORG_DIR_NAME).is_dir():
        return root / ORG_DIR_NAME, "repo"
    return _data_dir() / "org", "data_dir"


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


class _File:
    """One org file as read: its top comment block, its facts, the entries
    that did not parse (kept verbatim), and whether it is safe to rewrite."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.header: list[str] = []
        self.facts: list[Fact] = []
        self.unparsed: list[Any] = []
        self.error: str | None = None
        self.exists = path.exists()

    def load(self, warnings: list[str] | None = None) -> _File:
        if not self.exists:
            return self
        import yaml
        try:
            text = self.path.read_text(encoding="utf-8")
            data = safe_yaml(text)
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
            self.error = f"{self.path.name}: not readable as YAML ({type(e).__name__}); ignored"
            _warn(warnings, self.error)
            return self
        for line in text.splitlines():
            if line.startswith("#") or not line.strip():
                if line.strip() != HEADER:
                    self.header.append(line.rstrip())
                continue
            break
        while self.header and not self.header[-1].strip():
            self.header.pop()
        if data is not None and not isinstance(data, list):
            self.error = f"{self.path.name}: expected a list of facts; ignored"
            _warn(warnings, self.error)
            return self
        for i, raw in enumerate(data or []):
            try:
                self.facts.append(Fact.from_dict(raw, origin=self.path.name))
            except Exception as e:  # noqa: BLE001 - kept verbatim, never dropped
                self.unparsed.append(raw)
                reason = str(e) if isinstance(e, FactError) else f"{type(e).__name__}: {e}"
                _warn(warnings, f"{self.path.name}[{i}]: skipped, {reason}")
        return self


def _read_dir(d: Path, warnings: list[str]) -> list[Fact]:
    facts: list[Fact] = []
    if not d.is_dir():
        return facts
    for name in sorted(os.listdir(d)):
        if not name.endswith((".yaml", ".yml")) or name.startswith("."):
            continue
        if name not in KNOWN_FILES:
            _warn(warnings, f"{name}: not an org model file ({', '.join(KNOWN_FILES)}); ignored")
            continue
        facts.extend(_File(d / name).load(warnings).facts)
    return facts


def load(dir: str | os.PathLike | None = None, *, legacy: bool = True) -> OrgModel:
    """The org model. Never raises for bad content: a bad entry is skipped
    with a warning naming its file and index (also in OrgModel.warnings).

    Legacy facts (tag_rules.yaml, accounts.yaml, FINOPS_REQUIRED_TAGS and
    FINOPS_PROTECTED_TAGS) are merged under the file facts: one is dropped
    when a file fact has its key, or a confirmed file fact holds its slot."""
    d, why = resolve_dir(dir)
    warnings: list[str] = []
    facts: list[Fact] = []
    seen: set[str] = set()
    for f in _read_dir(d, warnings):
        if f.key in seen:
            _warn(warnings, f"{f.origin}: fact {f.key} appears more than once; the first is used")
            continue
        seen.add(f.key)
        facts.append(f)
    if legacy:
        from .legacy import legacy_facts
        held = {f.slot for f in facts if f.confirmed}
        for f in legacy_facts(warnings=warnings):
            if f.key in seen or f.slot in held:
                continue
            seen.add(f.key)
            facts.append(f)
    return OrgModel(facts, dir=d, dir_source=why, warnings=warnings)


# ── write ─────────────────────────────────────────────────────────────────────

def _yaml_text(entries: list[dict[str, Any]], header: Iterable[str] = ()) -> str:
    import yaml

    class _Flow(dict):
        pass

    class _Dumper(yaml.SafeDumper):
        pass

    _Dumper.add_representer(
        _Flow, lambda dumper, data: dumper.represent_mapping(
            "tag:yaml.org,2002:map", data, flow_style=True))

    def shaped(d: dict[str, Any]) -> dict[str, Any]:
        out = dict(d)
        for k in ("subject", "value"):
            if isinstance(out.get(k), dict):
                out[k] = _Flow(out[k])
        for k in ("proposed_at", "confirmed_at", "review_after"):
            if isinstance(out.get(k), str):
                with contextlib.suppress(ValueError):
                    out[k] = date.fromisoformat(out[k])
        return out

    lines = [HEADER, *[h for h in header if h.strip() != HEADER]]
    body = yaml.dump([shaped(e) if isinstance(e, dict) else e for e in entries],
                     Dumper=_Dumper, sort_keys=False, default_flow_style=False,
                     allow_unicode=True, width=10_000) if entries else "[]\n"
    return "\n".join(lines) + "\n" + body


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
    entries: list[Any] = [x.to_dict() for x in sorted(f.facts, key=sort_key)]
    entries.extend(f.unparsed)
    _atomic_write(f.path, _yaml_text(entries, f.header))


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


def _files(d: Path) -> dict[str, _File]:
    return {name: _File(d / name).load() for name in KNOWN_FILES}


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
      "suppressed_rejected" a human rejected this exact fact; not re-proposed
      "conflict"            written as a proposal beside a confirmed fact that
                            says otherwise; the confirmed one keeps answering

    Whatever status the caller set, the fact is written as proposed."""
    new = _proposal(fact)
    d, _ = resolve_dir(dir)
    with _locked(d):
        model = load(d)
        same = [f for f in model.facts if f.key == new.key]
        if any(f.status == "rejected" for f in same):
            return "suppressed_rejected"
        if any(f.live for f in same):
            return "duplicate"
        conflict = any(f.confirmed and f.slot == new.slot for f in model.facts)
        files = _files(d)
        expired = [(n, i) for n, fl in files.items() for i, f in enumerate(fl.facts)
                   if f.key == new.key]
        if expired:
            # An expired fact proposed again comes back in place, as a proposal.
            n, i = expired[0]
            files[n].facts[i] = replace(new, origin=n)
            _write_file(files[n])
        else:
            target = files[FILE_FOR_KIND[new.fact]]
            target.facts.append(new)
            _write_file(target)
    return "conflict" if conflict else "added"


def _one(model: OrgModel, key: str) -> Fact:
    found = model.find(key)
    if not found:
        raise OrgError(f"no fact with key {key!r} (see `nable org review`)")
    if len({f.key for f in found}) > 1:
        raise OrgError(f"key {key!r} matches {len(found)} facts; give more characters")
    return found[0]


def _decide(key: str, by: str, status: str, dir: str | os.PathLike | None) -> Fact:
    by = (by or "").strip()
    if not by:
        raise OrgError("a human decision needs a name (confirmed_by)")
    d, _ = resolve_dir(dir)
    with _locked(d):
        model = load(d)
        target = _one(model, key)
        if target.origin == "legacy":
            if status == "confirmed":
                return target        # a human wrote the legacy file: already confirmed
            files = _files(d)
            decided = replace(target, status=status, confirmed_by=by,
                              confirmed_at=_today(), origin=None)
            files[FILE_FOR_KIND[decided.fact]].facts.append(decided)
            _write_file(files[FILE_FOR_KIND[decided.fact]])
            return decided
        files = _files(d)
        decided = replace(target, status=status, confirmed_by=by, confirmed_at=_today())
        _replace_in_files(files, decided, supersede=status == "confirmed")
        return decided


def _replace_in_files(files: dict[str, _File], fact: Fact, *, supersede: bool) -> None:
    """Put `fact` in place of the entry with its key (or add it), and, when
    it is a confirmed fact, expire the other confirmed facts in its slot: two
    confirmed answers to one question is a question nobody can answer."""
    changed: set[str] = set()
    placed = False
    for name, fl in files.items():
        for i, f in enumerate(fl.facts):
            if f.key == fact.key:
                if not placed:
                    fl.facts[i] = replace(fact, origin=name)
                    placed = True
                    changed.add(name)
            elif supersede and f.confirmed and f.slot == fact.slot:
                fl.facts[i] = replace(f, status="expired")
                changed.add(name)
    if not placed:
        name = FILE_FOR_KIND[fact.fact]
        files[name].facts.append(replace(fact, origin=name))
        changed.add(name)
    for name in sorted(changed):
        _write_file(files[name])


def confirm(key: str, by: str, dir: str | os.PathLike | None = None) -> Fact:
    """A human says yes. Only a human path calls this (the CLI with a TTY or
    --as); the MCP server and adapters never do."""
    return _decide(key, by, "confirmed", dir)


def reject(key: str, by: str, dir: str | os.PathLike | None = None) -> Fact:
    """A human says no. The fact is kept, so it is never proposed again."""
    return _decide(key, by, "rejected", dir)


def set_fact(fact: Fact, by: str, dir: str | os.PathLike | None = None) -> Fact:
    """A human states a fact directly: confirmed, confidence 1.0, source
    human unless the caller names one."""
    by = (by or "").strip()
    if not by:
        raise OrgError("a human fact needs a name (confirmed_by)")
    value = validate_value(fact.fact, fact.subject, fact.value)
    human = replace(fact, value=value, status="confirmed", confidence=1.0,
                    source=fact.source or "human", proposed_at=fact.proposed_at or _today(),
                    confirmed_by=by, confirmed_at=_today(), origin=None)
    d, _ = resolve_dir(dir)
    with _locked(d):
        _replace_in_files(_files(d), human, supersede=True)
    return human


def import_legacy(dir: str | os.PathLike | None = None) -> int:
    """Write the legacy facts into the org files, so the directory holds the
    whole model. Returns how many were added."""
    from .legacy import legacy_facts
    d, _ = resolve_dir(dir)
    added = 0
    with _locked(d):
        model = load(d, legacy=False)
        keys = {f.key for f in model.facts}
        held = {f.slot for f in model.facts if f.confirmed}
        files = _files(d)
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


# Adapters that read the org's own systems (CODEOWNERS, Terraform state, AWS
# Organizations) and return proposals. Empty in v0; `nable org init` runs each.
ADAPTERS: list[Callable[[OrgModel], Iterable[Fact]]] = []


def run_adapters(dir: str | os.PathLike | None = None) -> dict[str, int]:
    """Run every adapter and propose what it returns. A failing adapter is
    counted, never fatal."""
    counts: dict[str, int] = {}
    model = load(dir)
    for adapter in ADAPTERS:
        try:
            for f in adapter(model):
                r = propose(f, dir)
                counts[r] = counts.get(r, 0) + 1
        except Exception:  # noqa: BLE001 - one adapter never stops init
            counts["failed"] = counts.get("failed", 0) + 1
    return counts
