# SPDX-License-Identifier: Apache-2.0
"""Install, update, remove, list, audit, validate and scaffold packs.

Sources, and how each is pinned:

    ./path/to/pack                 a local directory (copied; .git skipped)
    ./pack-1.0.0.tar.gz            a local tarball (archive sha256 recorded)
    git+https://host/repo@<sha>    a git URL pinned to a full 40-hex commit,
                                   optionally #subdir=path/in/repo
    io.github.org/name[@1.2.0]     a registry entry, which resolves to one of
                                   the above plus a content digest to match

Every install stages the pack in a private directory under the packs root,
validates the manifest and every content file, checks [integrity] when the
manifest has it, computes the sha256 of every file always, checks the org's
packs: policy, shows the capabilities and waits for approval, and only then
moves the files into place and records them in the index.

Approval: an interactive prompt, or `--yes`. `--yes` is refused when the org
policy sets packs.require_signed, because a flag an agent can type is not a
person reviewing a pack. An unattended update (`auto`) applies only when it
adds no capability; one that adds any capability, host, secret or scope waits
for a person, which is the fix for an update that quietly starts sending data
somewhere new.

Tarballs are read member by member and nothing is written until every member
has been checked: an absolute path, a `..` component, a symlink, a hard link
or a device anywhere in the archive refuses the whole archive.
"""
from __future__ import annotations

import contextlib
import getpass
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from . import capabilities as caps_mod
from . import registry as registry_mod
from . import store
from .content import PackContent, load_content
from .errors import (
    ApprovalRequired,
    IntegrityError,
    PackError,
    PolicyRefusal,
    Problem,
    ValidationError,
)
from .manifest import (
    MANIFEST_NAME,
    Manifest,
    check_glob,
    check_name,
    check_namespace,
    load_manifest,
)
from .versions import version_key

GIT_TIMEOUT_S = 120
_COMMIT40 = re.compile(r"^[0-9a-f]{40}$")
TARBALL_SUFFIXES = (".tar.gz", ".tgz", ".tar")
# Where a first-party pack must come from when the org requires signed packs,
# until part 2 verifies signatures instead.
FIRST_PARTY_GIT_PREFIXES = ("https://github.com/getnable/",)


# ── sources ───────────────────────────────────────────────────────────────────

@dataclass
class Source:
    kind: str                      # dir | tarball | git | registry
    location: str                  # an absolute path, a git URL, or a registry ref
    commit: str | None = None
    subdir: str | None = None
    archive_sha256: str | None = None
    registry: str | None = None
    ref: str | None = None

    def spec(self) -> str:
        """The source as one string: what packs.allowed_sources globs match."""
        if self.kind == "git":
            return f"git+{self.location}@{self.commit}" + (f"#subdir={self.subdir}"
                                                           if self.subdir else "")
        return self.location

    def record(self) -> dict[str, Any]:
        out = {"kind": self.kind, "spec": self.spec(), "location": self.location}
        for k in ("commit", "subdir", "registry", "ref"):
            if getattr(self, k):
                out[k] = getattr(self, k)
        if self.archive_sha256:
            out["sha256"] = self.archive_sha256
        return out


def parse_source(text: str) -> Source:
    """What `nable pack install <text>` means. Raises PackError."""
    t = (text or "").strip()
    if not t:
        raise PackError("No pack source given")
    if t.startswith("git+"):
        rest, subdir = t[4:], None
        if "#" in rest:
            rest, frag = rest.split("#", 1)
            if not frag.startswith("subdir="):
                raise PackError(f"{t}: the only fragment a git source takes is #subdir=path")
            subdir = frag[len("subdir="):].strip("/")
            why = check_glob(subdir) or ("must be a path, not a glob"
                                         if any(c in subdir for c in "*?[") else None)
            if why:
                raise PackError(f"{t}: subdir {why}")
        url, sep, commit = rest.rpartition("@")
        if not sep or not _COMMIT40.match(commit):
            raise PackError(f"{t}: a git source must be pinned to a full 40-character commit, "
                            "as git+<url>@<commit>; a branch or tag can move under you")
        scheme = urlparse(url).scheme
        if scheme not in ("https", "ssh", "file") or url.startswith("-"):
            raise PackError(f"{t}: git sources must use https://, ssh:// or file://")
        return Source("git", url, commit=commit, subdir=subdir or None)
    if t.startswith(("http://", "https://")):
        raise PackError(f"{t}: install from a URL as git+https://...@<commit>, or download "
                        "the tarball and install the file")
    p = Path(t).expanduser()
    if p.exists():
        full = p.resolve()
        if full.is_dir():
            return Source("dir", str(full))
        if full.is_file() and full.name.endswith(TARBALL_SUFFIXES):
            return Source("tarball", str(full))
        raise PackError(f"{t} is neither a pack directory nor a .tar.gz, .tgz or .tar file")
    if registry_mod.parse_ref(t):
        return Source("registry", t)
    raise PackError(f"{t} is not a directory, a tarball, a pinned git URL or a registry "
                    "reference such as io.github.getnable/startup-credits-runway")


def _copy_dir(src: Path, dest: Path) -> None:
    for rel in store.walk(src, skip_ignored=True):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / rel, target)


def extract_tarball(archive: Path, dest: Path) -> None:
    """Extract a pack archive into `dest`, refusing the whole archive on any
    absolute path, `..`, link or special file. Nothing is written until every
    member has passed. Raises PackError."""
    try:
        tf = tarfile.open(archive, "r:*")  # noqa: SIM115 - closed by the `with tf` below
    except (tarfile.TarError, OSError) as e:
        raise PackError(f"{archive} is not a readable tarball ({e})") from None
    problems: list[Problem] = []
    members: list[tuple[tuple[str, ...], tarfile.TarInfo]] = []
    total = 0
    with tf:
        try:
            for m in tf:
                name = m.name
                if len(members) > store.MAX_FILES * 2:
                    problems.append(Problem(".", "the archive has too many members"))
                    break
                if "\\" in name:
                    problems.append(Problem(name, "contains a backslash"))
                    continue
                if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
                    problems.append(Problem(name, "is an absolute path"))
                    continue
                parts = tuple(x for x in PurePosixPath(name).parts if x not in ("", "."))
                if ".." in parts:
                    problems.append(Problem(name, "leaves the archive with .."))
                    continue
                if m.issym() or m.islnk():
                    problems.append(Problem(name, "is a link; packs may not contain links"))
                    continue
                if m.isdir():
                    members.append((parts, m))
                    continue
                if not m.isfile():
                    problems.append(Problem(name, "is a device, pipe or other special file"))
                    continue
                if m.size > store.MAX_FILE_BYTES:
                    problems.append(Problem(name, f"is larger than {store.MAX_FILE_BYTES} bytes"))
                    continue
                total += m.size
                members.append((parts, m))
        except tarfile.TarError as e:
            raise PackError(f"{archive} is not a readable tarball ({e})") from None
        if total > store.MAX_TOTAL_BYTES:
            problems.append(Problem(".", f"unpacks to more than {store.MAX_TOTAL_BYTES} bytes"))
        if problems:
            raise PackError(f"{archive} was refused and nothing was extracted", problems)
        root = dest.resolve()
        for parts, m in members:
            if not parts:
                continue
            target = dest.joinpath(*parts)
            if not target.resolve().is_relative_to(root):
                raise PackError(f"{archive} was refused", [Problem(m.name, "leaves the archive")])
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            fh = tf.extractfile(m)
            if fh is None:
                raise PackError(f"{archive} was refused", [Problem(m.name, "could not be read")])
            try:
                with fh, open(target, "xb") as out:
                    shutil.copyfileobj(fh, out)
            except FileExistsError:
                raise PackError(f"{archive} was refused",
                                [Problem(m.name, "appears twice in the archive")]) from None


def _git(args: list[str], cwd: Path | None = None) -> str:
    env = dict(os.environ)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="https:ssh:file",
               GIT_ASKPASS="", SSH_ASKPASS="")
    try:
        r = subprocess.run(  # a fixed argv and no shell; the URL follows "--"
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "advice.detachedHead=false", *args],
            cwd=cwd, env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
            check=False)
    except FileNotFoundError:
        raise PackError("git is not installed, so a git source cannot be fetched") from None
    except subprocess.TimeoutExpired:
        raise PackError(f"git {args[0]} took longer than {GIT_TIMEOUT_S}s and was stopped") from None
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip().splitlines()
        raise PackError(f"git {args[0]} failed: {detail[-1] if detail else r.returncode}")
    return r.stdout.strip()


def fetch_git(url: str, commit: str, dest: Path) -> None:
    """Clone `url` into `dest` and check out exactly `commit`, then drop .git."""
    _git(["clone", "--quiet", "--no-checkout", "--", url, str(dest)])
    _git(["checkout", "--quiet", commit, "--"], cwd=dest)
    head = _git(["rev-parse", "HEAD"], cwd=dest)
    if head != commit:
        raise PackError(f"{url} checked out {head}, not the pinned {commit}")
    shutil.rmtree(dest / ".git")


def _stage(src: Source, work: Path) -> Path:
    """Put the pack's files in `work` and return the pack root inside it."""
    if src.kind == "dir":
        root = work / "pack"
        root.mkdir()
        _copy_dir(Path(src.location), root)
        return root
    if src.kind == "tarball":
        src.archive_sha256 = store.sha256_file(Path(src.location))
        out = work / "pack"
        out.mkdir()
        extract_tarball(Path(src.location), out)
        if (out / MANIFEST_NAME).is_file():
            return out
        tops = list(out.iterdir())
        if len(tops) == 1 and tops[0].is_dir() and (tops[0] / MANIFEST_NAME).is_file():
            return tops[0]
        raise ValidationError(f"{src.location} has no {MANIFEST_NAME} at its top level")
    if src.kind == "git":
        out = work / "checkout"
        fetch_git(src.location, src.commit or "", out)
        if not src.subdir:
            return out
        root = out
        for part in PurePosixPath(src.subdir).parts:
            root = root / part
            if root.is_symlink() or not root.is_dir():
                raise PackError(f"{src.spec()}: subdir {src.subdir} is not a directory "
                                "in that commit")
        return root
    raise PackError(f"cannot stage a {src.kind} source")


# ── the plan: what would be installed, and what it may do ────────────────────

@dataclass
class Plan:
    manifest: Manifest
    source: Source
    root: Path
    files: dict[str, str]
    digest: str
    content: PackContent
    previous: dict[str, Any] | None = None
    added: dict[str, list[str]] = field(default_factory=dict)
    removed: dict[str, list[str]] = field(default_factory=dict)

    @property
    def is_update(self) -> bool:
        return self.previous is not None

    def summary(self) -> dict[str, Any]:
        m = self.manifest
        return {"id": m.id, "version": m.version, "tier": m.tier,
                "description": m.description, "maintainers": list(m.maintainers),
                "source": self.source.record(), "digest": self.digest,
                "capabilities": m.to_dict()["capabilities"],
                "provides": self.content.counts(),
                "code": [c.to_dict() for c in m.code],
                "previous_version": (self.previous or {}).get("version"),
                "capabilities_added": self.added, "capabilities_removed": self.removed}


def _prepare(src: Source, work: Path, *, expected_digest: str | None = None,
             registry_tier: str | None = None) -> Plan:
    root = _stage(src, work)
    # Not skip_ignored: staging already left .git behind for directories and
    # git checkouts, and whatever a tarball carries is installed, so it is hashed.
    files = store.hash_tree(root)
    manifest = load_manifest(root)
    if manifest.status == "archived":
        raise PackError(f"{manifest.id} {manifest.version} is archived by its maintainers, so "
                        "it is not installed; its registry entry or repository may name a "
                        "successor")
    content = load_content(root, manifest.provides)
    if content.problems:
        raise ValidationError(f"{manifest.id} {manifest.version} has invalid content",
                              content.problems)
    if manifest.integrity_files is not None:
        actual = {k: v for k, v in files.items() if k != MANIFEST_NAME}
        cmp = store.compare_files(manifest.integrity_files, actual)
        problems = ([Problem(p, "does not match its [integrity] sha256") for p in cmp["modified"]]
                    + [Problem(p, "is pinned in [integrity] but missing") for p in cmp["missing"]]
                    + [Problem(p, "is in the pack but not pinned in [integrity]")
                       for p in cmp["added"]])
        if problems:
            raise IntegrityError(f"{manifest.id} {manifest.version} failed its integrity check; "
                                 "nothing was installed", problems)
    digest = store.content_digest(files)
    if expected_digest and digest != expected_digest:
        raise IntegrityError(f"{manifest.id} {manifest.version}: the registry pins sha256 "
                             f"{expected_digest}, but the files hash to {digest}; nothing was "
                             "installed")
    if registry_tier and registry_tier != manifest.tier:
        raise IntegrityError(f"{manifest.id}: the registry lists it as {registry_tier}, but its "
                             f"manifest says {manifest.tier}; nothing was installed")
    return Plan(manifest, src, root, files, digest, content)


# ── org policy ────────────────────────────────────────────────────────────────

def _matches(patterns: list[str] | None, *candidates: str) -> bool:
    import fnmatch
    return any(fnmatch.fnmatchcase(c, p) for p in patterns or [] for c in candidates if c)


def provably_first_party(source: dict[str, Any]) -> bool:
    """Until signing (part 2), the only first-party provenance the core can
    check is where the files came from: a pinned commit in nable's own GitHub
    organization. A local copy proves nothing about who wrote it."""
    return source.get("kind") == "git" and str(source.get("location", "")).startswith(
        FIRST_PARTY_GIT_PREFIXES)


def policy_violations(pack_id: str, tier: str, caps: dict[str, Any], source: dict[str, Any],
                      pp: dict[str, Any]) -> list[Problem]:
    """What the org's packs: policy says about this pack. Empty means allowed.

    allowed_sources globs match the source spec only (a namespace is a claim
    until part 2 verifies it, so it cannot earn a pack a place on an
    allowlist); blocked_sources globs match the source spec or the pack id."""
    out: list[Problem] = []
    spec = str(source.get("spec", ""))
    where = pp.get("path") or "the org policy"
    if pp.get("invalid"):
        out.append(Problem("policy", f"{where} has a packs section (or file) that cannot be "
                           "read, so pack installs are refused until it is fixed"))
        return out
    if _matches(pp.get("blocked_sources"), spec, pack_id):
        out.append(Problem("policy", f"{pack_id} from {spec} matches packs.blocked_sources"))
    allowed = pp.get("allowed_sources")
    if allowed is not None and not _matches(allowed, spec):
        out.append(Problem("policy", f"{spec} is not in packs.allowed_sources"))
    if pp.get("require_signed"):
        if tier != "first-party":
            out.append(Problem("policy", f"{pack_id} is {tier}, and the org policy sets "
                               "packs.require_signed. Pack signatures arrive in pack SDK part 2; "
                               "until then only first-party packs can be installed under it"))
        elif not provably_first_party(source):
            out.append(Problem("policy", f"{pack_id} says it is first-party, but it comes from "
                               f"{spec}, and the org policy sets packs.require_signed. Until "
                               "signatures arrive (pack SDK part 2) a first-party pack must "
                               "come from a pinned commit under "
                               + ", ".join(FIRST_PARTY_GIT_PREFIXES)))
    ceiling = pp.get("allowed_capabilities")
    if ceiling is not None:
        out.extend(caps_mod.exceeds(caps, ceiling))
    return out


# ── the lifecycle ─────────────────────────────────────────────────────────────

def _whoami() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):  # no user database entry, as in some containers
        return os.environ.get("USER") or "unknown"


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """One pack operation at a time on this data dir (POSIX; a no-op elsewhere)."""
    root = store.packs_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # Windows
        yield
        return
    fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _invalidate() -> None:
    from . import runtime
    runtime.invalidate()


def _resolve(source: str | Source, registry: str | None
             ) -> tuple[Source, str | None, str | None]:
    src = source if isinstance(source, Source) else parse_source(source)
    if src.kind != "registry":
        return src, None, None
    entry = registry_mod.resolve(src.location, registry)
    resolved = parse_source(entry.source)
    if resolved.kind == "registry":
        raise PackError(f"{entry.id}: a registry entry cannot point at another entry")
    resolved.registry, resolved.ref = entry.registry, f"{entry.id}@{entry.version}"
    return resolved, entry.sha256, entry.tier


def install(source: str | Source, *, yes: bool = False, auto: bool = False,
            approve: Callable[[Plan], bool] | None = None, expect_update: bool = False,
            registry: str | None = None) -> dict[str, Any]:
    """Install (or update to) the pack at `source`. Returns what happened:
    {"status": "installed" | "updated" | "repaired" | "unchanged", "pack": {...}}.

    yes      the caller already approved (refused under packs.require_signed)
    auto     an unattended update: applies only when no capability is added
    approve  asked with the Plan when neither applies; True approves
    Raises PackError (ValidationError, IntegrityError, PolicyRefusal,
    ApprovalRequired, RegistryError) and installs nothing when it does."""
    from ..policy import pack_policy
    src, expected_digest, registry_tier = _resolve(source, registry)
    pp = pack_policy()
    if pp.get("invalid"):
        raise PolicyRefusal("Nothing was installed",
                            policy_violations("", "", {}, {}, pp) + [Problem("policy", p)
                                                                    for p in pp["problems"]])
    staging = store.packs_root() / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    with _locked():
        work = Path(tempfile.mkdtemp(prefix="stage-", dir=staging))
        try:
            plan = _prepare(src, work, expected_digest=expected_digest,
                            registry_tier=registry_tier)
            m = plan.manifest
            violations = policy_violations(m.id, m.tier, m.capabilities, src.record(), pp)
            if violations:
                raise PolicyRefusal(f"{m.id} {m.version} is not allowed by the org policy; "
                                    "nothing was installed", violations)
            idx = store.read_index()
            prev = idx["packs"].get(m.id)
            if expect_update and prev is None:
                raise PackError(f"{m.id} is not installed; use `nable pack install`")
            repair = False
            if prev is not None:
                if prev.get("version") == m.version:
                    if prev.get("digest") == plan.digest:
                        if _intact(prev):
                            return {"status": "unchanged", "pack": _public(prev)}
                        # The same approved pack, but its installed files were
                        # changed or removed: put the approved files back.
                        repair = True
                    else:
                        raise PackError(
                            f"{m.id} {m.version} is already installed with different files. "
                            "A changed pack needs a new version; to replace it anyway, "
                            f"`nable pack remove {m.id}` first")
                if version_key(m.version) < version_key(prev["version"]):
                    raise PackError(f"{m.id} {prev['version']} is installed and {m.version} is "
                                    f"older. To go back, `nable pack remove {m.id}` first")
                d = caps_mod.diff(prev.get("capabilities") or {}, m.capabilities)
                plan.previous, plan.added, plan.removed = prev, d["added"], d["removed"]
            how = _approve(plan, pp, yes=yes, auto=auto, approve=approve)
            entry = _commit(plan, idx, how)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    _invalidate()
    status = "repaired" if repair else "updated" if plan.is_update else "installed"
    return {"status": status, "pack": _public(entry)}


def _intact(entry: dict[str, Any]) -> bool:
    """Whether an installed pack's files are exactly the ones approved."""
    root = store.install_dir(entry["namespace"], entry["name"], entry["version"])
    try:
        return root.is_dir() and store.hash_tree(root) == (entry.get("files") or {})
    except PackError:
        return False


def _approve(plan: Plan, pp: dict[str, Any], *, yes: bool, auto: bool,
             approve: Callable[[Plan], bool] | None) -> str:
    m = plan.manifest
    if auto and not yes:
        if not plan.is_update:
            raise ApprovalRequired(f"{m.id} is not installed, and a first install always "
                                   "needs a person to approve it")
        if plan.added:
            added = "; ".join(f"{k}: {', '.join(v)}" for k, v in plan.added.items())
            raise ApprovalRequired(f"{m.id} {m.version} adds capabilities ({added}). An update "
                                   "that adds any capability waits for a person to approve it; "
                                   f"run `nable pack update {m.id}` at a terminal")
        return "auto-update (no new capabilities)"
    if yes:
        if pp.get("require_signed"):
            raise PolicyRefusal(f"{m.id}: --yes is refused because the org policy sets "
                                "packs.require_signed. Approve it at a terminal, where the "
                                "capabilities are shown")
        return "--yes"
    if approve is not None and approve(plan):
        return "interactive"
    raise ApprovalRequired(f"{m.id} {m.version} was not approved, so nothing was installed. "
                           "Run it at a terminal to review what it can do, or pass --yes")


def _commit(plan: Plan, idx: dict[str, Any], how: str) -> dict[str, Any]:
    m = plan.manifest
    dest = store.install_dir(m.namespace, m.name, m.version)
    if dest.exists():
        shutil.rmtree(dest)  # a leftover no index entry points at
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(plan.root), str(dest))
    now = store.now_iso()
    entry = {
        "namespace": m.namespace, "name": m.name, "version": m.version, "tier": m.tier,
        "description": m.description, "maintainers": list(m.maintainers),
        "nable_api": m.nable_api, "status": m.status,
        "source": plan.source.record(), "files": plan.files, "digest": plan.digest,
        "capabilities": m.to_dict()["capabilities"],
        "provides": {k: list(v) for k, v in m.provides.items()},
        "code": [c.to_dict() for c in m.code],
        "approved_by": _whoami(), "approval": how, "approved_at": now,
        "installed_at": now,
        "previous_version": (plan.previous or {}).get("version"),
    }
    idx["packs"][m.id] = entry
    idx["schema"] = store.INDEX_SCHEMA
    store.write_index(idx)
    if plan.previous and plan.previous.get("version") != m.version:
        old = store.install_dir(m.namespace, m.name, plan.previous["version"])
        shutil.rmtree(old, ignore_errors=True)
    return entry


def update(ref: str, *, yes: bool = False, auto: bool = False,
           approve: Callable[[Plan], bool] | None = None,
           registry: str | None = None) -> dict[str, Any]:
    """Update an installed pack: by id through the registry (to its newest
    version), or from a source given directly. Same approval rules as
    install(), plus the capability diff."""
    idx = store.read_index()
    installed = idx["packs"].get(ref)
    if installed is None:
        return install(ref, yes=yes, auto=auto, approve=approve, expect_update=True,
                       registry=registry)
    entry = registry_mod.resolve(ref, registry)
    if version_key(entry.version) <= version_key(installed["version"]):
        return {"status": "up-to-date", "pack": _public(installed),
                "registry_version": entry.version}
    return install(f"{entry.id}@{entry.version}", yes=yes, auto=auto, approve=approve,
                   expect_update=True, registry=registry)


def remove(pack_id: str) -> dict[str, Any]:
    """Delete an installed pack's files and its index entry."""
    with _locked():
        idx = store.read_index()
        entry = idx["packs"].pop(pack_id, None)
        if entry is None:
            raise PackError(f"{pack_id} is not installed")
        store.write_index(idx)
        dest = store.install_dir(entry["namespace"], entry["name"], entry["version"])
        shutil.rmtree(dest, ignore_errors=True)
        with contextlib.suppress(OSError):
            dest.parent.rmdir()
            dest.parent.parent.rmdir()
    _invalidate()
    return {"status": "removed", "pack": _public(entry)}


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    """An index entry without the per-file hash table (audit has that)."""
    out = {k: v for k, v in entry.items() if k != "files"}
    out["id"] = f"{entry.get('namespace')}/{entry.get('name')}"
    out["file_count"] = len(entry.get("files") or {})
    return out


def list_installed() -> list[dict[str, Any]]:
    idx = store.read_index()
    return [_public(e) for _, e in sorted(idx["packs"].items())]


def audit() -> dict[str, Any]:
    """Every installed pack: tier, capabilities, a fresh hash of every file
    against what was approved, and whether today's org policy still allows it.
    `ok` is False when any pack is tampered, missing, invalid or outside
    policy."""
    from ..policy import pack_policy
    idx = store.read_index()
    pp = pack_policy()
    packs = []
    for pid, e in sorted(idx["packs"].items()):
        root = store.install_dir(e["namespace"], e["name"], e["version"])
        row = _public(e)
        problems: list[str] = []
        status = "ok"
        if not root.is_dir():
            status = "missing"
            problems.append(f"{root} is gone")
        else:
            try:
                actual = store.hash_tree(root)
            except PackError as err:
                status = "tampered"
                problems.extend(str(p) for p in err.problems)
                actual = None
            if actual is not None:
                cmp = store.compare_files(e.get("files") or {}, actual)
                if any(cmp.values()):
                    status = "tampered"
                    problems += [f"{p}: changed since it was approved" for p in cmp["modified"]]
                    problems += [f"{p}: missing" for p in cmp["missing"]]
                    problems += [f"{p}: added after install" for p in cmp["added"]]
                row["integrity"] = cmp
            if status == "ok":
                try:
                    load_manifest(root)
                except ValidationError as err:
                    status = "invalid"
                    problems.append(str(err))
        viol = policy_violations(pid, e.get("tier", ""), e.get("capabilities") or {},
                                 e.get("source") or {}, pp)
        if viol:
            problems += [p.reason for p in viol]
            if status == "ok":
                status = "outside-policy"
        row["status"] = status
        row["problems"] = problems
        packs.append(row)
    return {"ok": all(p["status"] == "ok" for p in packs), "packs": packs,
            "index": str(store.index_path())}


def validate_dir(path: str | Path) -> dict[str, Any]:
    """`nable pack validate`: everything install checks except policy and
    approval, run in place. {"ok": bool, "problems": [...], ...}."""
    root = Path(path).expanduser()
    if not root.is_dir():
        return {"ok": False, "problems": [f"{root} is not a directory"]}
    try:
        files = store.hash_tree(root, skip_ignored=True)
        manifest = load_manifest(root)
    except PackError as err:
        return {"ok": False, "problems": [err.message] + [str(p) for p in err.problems]}
    content = load_content(root, manifest.provides)
    problems = [str(p) for p in content.problems]
    if manifest.integrity_files is not None:
        actual = {k: v for k, v in files.items() if k != MANIFEST_NAME}
        cmp = store.compare_files(manifest.integrity_files, actual)
        problems += [f"{p}: does not match its [integrity] sha256" for p in cmp["modified"]]
        problems += [f"{p}: is pinned in [integrity] but missing" for p in cmp["missing"]]
        problems += [f"{p}: is in the pack but not pinned in [integrity]" for p in cmp["added"]]
    return {"ok": not problems, "problems": problems, "id": manifest.id,
            "version": manifest.version, "tier": manifest.tier,
            "digest": store.content_digest(files), "files": len(files),
            "provides": content.counts(), "code": [c.to_dict() for c in manifest.code],
            "capabilities": manifest.to_dict()["capabilities"]}


# ── scaffold ──────────────────────────────────────────────────────────────────

_SCAFFOLD_MANIFEST = """\
[pack]
name        = "{name}"
namespace   = "{namespace}"
version     = "0.1.0"
description = "Describe what this pack does in one sentence"
license     = "Apache-2.0"
nable_api   = ">=1.0,<2.0"
maintainers = ["@your-handle"]
support     = "community"
status      = "active"

# What this pack may read or do. Empty means nothing. A data-only pack like
# this one needs no capabilities to be loaded; add them only for what you use.
[capabilities]
read_data    = []
guard        = "tighten-only"
max_autonomy = "L1"

[provides]
policies    = ["policies/*.yaml"]
guard_rules = ["guard/*.yaml"]
skills      = ["skills/{name}/SKILL.md"]

[compat]
harnesses = ["claude-code", "cursor", "codex"]
"""

_SCAFFOLD_POLICY = """\
version: 1
rules:
  - id: large-monthly-increase
    description: Flag a finding whose monthly cost increase is over 1000 USD.
    applies_to: finding
    match:
      all:
        - {field: monthly_delta_usd, op: gt, value: 1000}
    effect:
      action: flag
      severity: medium
      message: "This adds $$${monthly_delta_usd}/mo."
"""

_SCAFFOLD_GUARD = """\
version: 1
rules:
  - id: ask-before-nat-gateway
    target: command
    pattern: '\\baws\\s+ec2\\s+create-nat-gateway\\b'
    verdict: ask
    reason: NAT gateways bill hourly plus per GB processed; confirm this one is needed.
    price_hint:
      monthly_usd: 33
      note: One NAT gateway, before data processing charges.
"""

_SCAFFOLD_SKILL = """\
---
name: {name}
description: How a coding agent should use this pack. Replace with when it applies.
---

# {name}

Before a change that adds cloud resources, run `nable guard` or ask nable to
estimate it, and tell the user the monthly figure before applying.
"""


def new_pack(name: str, dest: str | Path | None = None, *,
             namespace: str = "io.github.your-org") -> Path:
    """Write a minimal valid data pack (one policy, one guard rule, one skill)."""
    why = check_name(name) or check_namespace(namespace)
    if why:
        raise PackError(f"cannot scaffold {namespace}/{name}: {why}")
    root = Path(dest).expanduser() if dest else Path.cwd() / name
    if root.exists() and any(root.iterdir()):
        raise PackError(f"{root} already exists and is not empty")
    files = {
        MANIFEST_NAME: _SCAFFOLD_MANIFEST.format(name=name, namespace=namespace),
        "policies/example.yaml": _SCAFFOLD_POLICY,
        "guard/example.yaml": _SCAFFOLD_GUARD,
        f"skills/{name}/SKILL.md": _SCAFFOLD_SKILL.format(name=name),
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root
