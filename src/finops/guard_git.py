# SPDX-License-Identifier: Apache-2.0
"""What a git branch switch would do to the org model's files, read from the repo itself (D157).

`git checkout BRANCH`, `git checkout COMMIT` and `git switch [--detach] REF`
rewrite every tracked file whose content differs between the commit HEAD is
on and the target, nable.org/authority.yaml among them, without naming it.
The guard used to say so in `nable guard doctor` and stay silent. This reads
the repo's own objects (loose, and packed with OFS and REF deltas), its refs
(loose and packed-refs) and, for a worktree, its own HEAD and the common
directory's objects and refs, and compares the org directory's tree at HEAD
with the target's:

  the same tree     nothing under it changes: the guard stays silent, unless
                    the files there changed since HEAD last moved (a forced
                    switch would throw those changes away)
  another tree      the switch rewrites the files that differ, and the guard
                    asks, naming them
  cannot tell       an unresolvable ref, a missing object, a shape this does
                    not read, or the meter spent: the caller asks whenever
                    the repo holds org files, as for a merge (Unknown)

In process, standard library only: no subprocess, no network, nothing
written. Every read is charged to guard_paths' meter (a quarter of a
millisecond or so a unit), so a huge repo gets an answer, an ask, inside the
hook's time. Reads are bounded: OBJECT_MAX bytes an object inflated,
DELTA_DEPTH deltas a chain, a pack index searched by seeks, never read whole.
"""
from __future__ import annotations

import os
import re
import zlib
from pathlib import Path
from typing import Any

from . import guard_paths

OBJECT_MAX = 8 * 1024 * 1024
DELTA_DEPTH = 64
REF_DEPTH = 8
FILES_MAX = 512
_UNIT_BYTES = 64 * 1024
_HEX40 = re.compile(r"[0-9a-f]{40}")
_ABBREV = re.compile(r"[0-9a-f]{4,39}")
_REF_NAME = re.compile(r"[A-Za-z0-9._/@+-]{1,255}")
_TYPES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
_OFS_DELTA, _REF_DELTA = 6, 7
_PACKED_REFS_MAX = 16 * 1024 * 1024
_REFLOG_TAIL = 64 * 1024


class Unknown(Exception):
    """What the switch would do cannot be told; the message says why."""


class NotARepo(Unknown):
    """No repository git would recognise is there (its git directory holds
    no HEAD), so git refuses the switch and nothing changes."""


def _charge(n_bytes: int) -> None:
    guard_paths.charge(1 + n_bytes // _UNIT_BYTES)


class Repo:
    """One repository's objects and refs, read in place."""

    def __init__(self, root: str | os.PathLike) -> None:
        self.root = Path(root)
        gd = guard_paths._git_dir(str(root))
        if gd is None or not gd.is_dir():
            raise Unknown("the repo's git directory could not be read")
        if not (gd / "HEAD").is_file():
            raise NotARepo("the git directory holds no HEAD")
        self.git_dir = gd
        common = gd
        try:
            raw = (gd / "commondir").read_text(encoding="utf-8", errors="replace")[:4096]
            c = Path(raw.strip())
            common = c if c.is_absolute() else gd / c
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise Unknown(f"the worktree's common directory could not be read "
                          f"({exc.strerror or type(exc).__name__})") from None
        self.common = common
        self.objects = common / "objects"
        self._packs: list[tuple[Path, Path]] | None = None
        self._packed: dict[str, str] | None = None

    # ── refs ────────────────────────────────────────────────────────────────

    def _loose_ref(self, name: str) -> str | None:
        # Per-worktree refs (HEAD, refs/worktree, refs/bisect) live in the
        # worktree's own directory; every other ref in the common one.
        dirs = (self.git_dir, self.common) if name == "HEAD" or name.startswith(
            ("refs/worktree/", "refs/bisect/")) else (self.common,)
        for d in dict.fromkeys(dirs):
            p = d / name
            try:
                if p.is_file():
                    _charge(0)
                    return p.read_text(encoding="utf-8", errors="replace")[:512].strip()
            except OSError:
                return None
        return None

    def packed_refs(self) -> dict[str, str]:
        if self._packed is None:
            out: dict[str, str] = {}
            p = self.common / "packed-refs"
            try:
                size = p.stat().st_size
            except OSError:
                size = -1
            if size > _PACKED_REFS_MAX:
                raise Unknown("packed-refs is larger than the guard reads")
            if size >= 0:
                _charge(size)
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith(("#", "^")) or " " not in line:
                        continue
                    sha, name = line.split(" ", 1)
                    if _HEX40.fullmatch(sha):
                        out[name.strip()] = sha
            self._packed = out
        return self._packed

    def ref(self, name: str, depth: int = 0) -> str | None:
        """The object a ref names, following symbolic refs; None when there
        is no such ref."""
        if depth > REF_DEPTH:
            raise Unknown("a symbolic ref points too deep")
        raw = self._loose_ref(name)
        if raw is None:
            return self.packed_refs().get(name)
        if raw.startswith("ref:"):
            return self.ref(raw[4:].strip(), depth + 1)
        if _HEX40.fullmatch(raw):
            return raw
        raise Unknown(f"the ref {name} does not read as an object name")

    def head_branch(self) -> str | None:
        raw = self._loose_ref("HEAD")
        return raw[4:].strip() if raw and raw.startswith("ref:") else None

    def previous_branch(self) -> str:
        """`-` and `@{-1}`: the branch HEAD was on before the last checkout,
        from HEAD's reflog."""
        p = self.git_dir / "logs" / "HEAD"
        try:
            with p.open("rb") as fh:
                size = fh.seek(0, os.SEEK_END)
                fh.seek(max(0, size - _REFLOG_TAIL))
                tail = fh.read(_REFLOG_TAIL)
        except OSError:
            raise Unknown("HEAD's reflog could not be read") from None
        _charge(len(tail))
        for line in reversed(tail.decode("utf-8", "replace").splitlines()):
            m = re.search(r"\tcheckout: moving from (\S+) to \S+$", line)
            if m:
                return m.group(1)
        raise Unknown("HEAD's reflog names no earlier branch")

    def resolve(self, rev: str, *, branch_first: bool = True) -> str:
        """The commit a revision names: HEAD, a branch, a tag, a remote
        branch, a full or abbreviated object name, `-` or @{-1}, and ~N or ^N
        after any of them. Unknown for anything else."""
        m = re.fullmatch(r"(.*?)((?:[~^]\d*)*)", rev)
        base, walk = (m.group(1), m.group(2)) if m else (rev, "")
        if base in ("-", "@{-1}"):
            base = self.previous_branch()
        sha = self._base(base or "HEAD", branch_first=branch_first)
        for step in re.findall(r"([~^])(\d*)", walk):
            n = int(step[1]) if step[1] else 1
            if step[0] == "~":
                for _ in range(n):
                    sha = self._parent(sha, 1)
            elif n:
                sha = self._parent(sha, n)
        return self.commit(sha)

    def _base(self, name: str, *, branch_first: bool) -> str:
        if name in ("HEAD", "@"):
            sha = self.ref("HEAD")
            if sha is None:
                raise Unknown("HEAD names no commit yet")
            return sha
        if not _REF_NAME.fullmatch(name) or ".." in name:
            raise Unknown(f"{name!r} is not a revision the guard reads")
        if branch_first:
            sha = self.ref(f"refs/heads/{name}")
            if sha:
                return sha
        if _HEX40.fullmatch(name) and self.has(name):
            return name
        for cand in (name, f"refs/{name}", f"refs/tags/{name}", f"refs/heads/{name}",
                     f"refs/remotes/{name}", f"refs/remotes/{name}/HEAD"):
            if cand.startswith("refs/") or cand in ("FETCH_HEAD", "ORIG_HEAD"):
                sha = self.ref(cand)
                if sha:
                    return sha
        if _ABBREV.fullmatch(name):
            return self.abbreviated(name)
        # git checkout's guess: a remote branch of that name on one remote.
        hits = sorted({sha for ref, sha in self._remote_refs().items()
                       if ref.split("/", 3)[-1] == name})
        if len(hits) == 1:
            return hits[0]
        raise Unknown(f"{name!r} names no branch, tag or commit the guard can find")

    def _remote_refs(self) -> dict[str, str]:
        out = {k: v for k, v in self.packed_refs().items() if k.startswith("refs/remotes/")}
        base = self.common / "refs" / "remotes"
        seen = 0
        for dirpath, _dirs, files in os.walk(base):
            for f in files:
                seen += 1
                if seen > FILES_MAX:
                    raise Unknown("too many remote branches to look through")
                name = os.path.relpath(os.path.join(dirpath, f), self.common).replace(os.sep, "/")
                sha = self.ref(name)
                if sha:
                    out[name] = sha
        return out

    # ── objects ─────────────────────────────────────────────────────────────

    def has(self, sha: str) -> bool:
        if (self.objects / sha[:2] / sha[2:]).is_file():
            return True
        return any(self._find_in_idx(idx, sha) is not None for idx, _pack in self.packs())

    def abbreviated(self, prefix: str) -> str:
        found: set[str] = set()
        d = self.objects / prefix[:2]
        if len(prefix) >= 2:
            try:
                names = os.listdir(d)
            except OSError:
                names = []
            _charge(len(names) * 64)
            found |= {prefix[:2] + n for n in names if (prefix[:2] + n).startswith(prefix)
                      and _HEX40.fullmatch(prefix[:2] + n)}
        for idx, _pack in self.packs():
            found |= self._prefix_in_idx(idx, prefix)
            if len(found) > 1:
                break
        if len(found) != 1:
            raise Unknown(f"{prefix} names no single object")
        return found.pop()

    def packs(self) -> list[tuple[Path, Path]]:
        if self._packs is None:
            d = self.objects / "pack"
            try:
                names = sorted(os.listdir(d))
            except OSError:
                names = []
            _charge(len(names) * 64)
            self._packs = [(d / n, d / (n[:-4] + ".pack")) for n in names
                           if n.endswith(".idx") and (d / (n[:-4] + ".pack")).is_file()]
        return self._packs

    @staticmethod
    def _idx_header(fh: Any) -> list[int]:
        head = fh.read(8)
        if head != b"\xfftOc\x00\x00\x00\x02":
            raise Unknown("a pack index is not version 2")
        fan = fh.read(1024)
        if len(fan) != 1024:
            raise Unknown("a pack index is cut short")
        return [int.from_bytes(fan[i:i + 4], "big") for i in range(0, 1024, 4)]

    def _find_in_idx(self, idx: Path, sha: str) -> int | None:
        """The object's offset in the pack, by binary search over the index's
        sorted names (each probe a seek and a 20-byte read)."""
        want = bytes.fromhex(sha)
        try:
            with idx.open("rb") as fh:
                fan = self._idx_header(fh)
                total = fan[255]
                lo = fan[want[0] - 1] if want[0] else 0
                hi = fan[want[0]]
                _charge(1024)
                while lo < hi:
                    mid = (lo + hi) // 2
                    fh.seek(8 + 1024 + 20 * mid)
                    got = fh.read(20)
                    guard_paths.charge(0)
                    if got == want:
                        fh.seek(8 + 1024 + 24 * total + 4 * mid)
                        off = int.from_bytes(fh.read(4), "big")
                        if off & 0x80000000:
                            fh.seek(8 + 1024 + 28 * total + 8 * (off & 0x7FFFFFFF))
                            off = int.from_bytes(fh.read(8), "big")
                        return off
                    if got < want:
                        lo = mid + 1
                    else:
                        hi = mid
        except OSError:
            raise Unknown("a pack index could not be read") from None
        return None

    def _prefix_in_idx(self, idx: Path, prefix: str) -> set[str]:
        out: set[str] = set()
        first = int(prefix[:2], 16)
        try:
            with idx.open("rb") as fh:
                fan = self._idx_header(fh)
                lo = fan[first - 1] if first else 0
                hi = fan[first]
                _charge(1024 + 20 * (hi - lo))
                if hi - lo > 1 << 16:
                    raise Unknown("an abbreviated object name matches too much to look through")
                fh.seek(8 + 1024 + 20 * lo)
                for i in range(hi - lo):
                    h = fh.read(20).hex()
                    if h.startswith(prefix):
                        out.add(h)
                        if len(out) > 1:
                            break
        except OSError:
            raise Unknown("a pack index could not be read") from None
        return out

    def read(self, sha: str, depth: int = 0) -> tuple[str, bytes]:
        """(type, content) of one object."""
        loose = self.objects / sha[:2] / sha[2:]
        try:
            raw = loose.read_bytes()
        except FileNotFoundError:
            raw = None
        except OSError:
            raise Unknown("an object could not be read") from None
        if raw is not None:
            _charge(len(raw))
            data = _inflate(raw, OBJECT_MAX + 64)
            head, _, body = data.partition(b"\x00")
            kind, _, size = head.decode("ascii", "replace").partition(" ")
            if kind not in _TYPES.values() or not size.isdigit() or int(size) != len(body):
                raise Unknown("a loose object does not read as one")
            return kind, body
        for idx, pack in self.packs():
            off = self._find_in_idx(idx, sha)
            if off is not None:
                return self._from_pack(pack, off, depth)
        raise Unknown(f"the object {sha[:12]} is not in this repo (a shallow clone, an "
                      "alternate store, or a partial clone)")

    def _from_pack(self, pack: Path, off: int, depth: int) -> tuple[str, bytes]:
        if depth > DELTA_DEPTH:
            raise Unknown("a delta chain is deeper than the guard reads")
        try:
            with pack.open("rb") as fh:
                fh.seek(off)
                b = fh.read(1)
                if not b:
                    raise Unknown("a pack is cut short")
                c = b[0]
                kind = (c >> 4) & 7
                size = c & 15
                shift = 4
                while c & 0x80:
                    c = fh.read(1)[0]
                    size |= (c & 0x7F) << shift
                    shift += 7
                if size > OBJECT_MAX:
                    raise Unknown("an object is larger than the guard reads")
                base_off = base_sha = None
                if kind == _OFS_DELTA:
                    c = fh.read(1)[0]
                    rel = c & 0x7F
                    while c & 0x80:
                        c = fh.read(1)[0]
                        rel = ((rel + 1) << 7) | (c & 0x7F)
                    base_off = off - rel
                    if base_off < 0:
                        raise Unknown("a pack delta points before the pack")
                elif kind == _REF_DELTA:
                    base_sha = fh.read(20).hex()
                elif kind not in _TYPES:
                    raise Unknown("a pack holds an object type the guard does not read")
                data = _inflate_stream(fh, size)
        except (OSError, IndexError):
            raise Unknown("a pack could not be read") from None
        _charge(len(data))
        if kind in _TYPES:
            return _TYPES[kind], data
        if base_off is not None:
            base_kind, base = self._from_pack(pack, base_off, depth + 1)
        else:
            base_kind, base = self.read(str(base_sha), depth + 1)
        return base_kind, _apply_delta(base, data)

    def commit(self, sha: str) -> str:
        """`sha` peeled to a commit (an annotated tag to what it tags)."""
        for _ in range(REF_DEPTH):
            kind, body = self.read(sha)
            if kind == "commit":
                return sha
            if kind != "tag":
                raise Unknown("the revision names no commit")
            m = re.match(rb"object ([0-9a-f]{40})\n", body)
            if not m:
                raise Unknown("a tag does not read as one")
            sha = m.group(1).decode()
        raise Unknown("tags point too deep")

    def _parent(self, sha: str, n: int) -> str:
        _kind, body = self.read(self.commit(sha))
        parents = re.findall(rb"^parent ([0-9a-f]{40})$", body.split(b"\n\n", 1)[0],
                             re.MULTILINE)
        if not 1 <= n <= len(parents):
            raise Unknown("the revision walks past the commit's parents")
        return parents[n - 1].decode()

    def tree_of(self, commit: str) -> str:
        kind, body = self.read(commit)
        m = re.match(rb"tree ([0-9a-f]{40})\n", body)
        if kind != "commit" or not m:
            raise Unknown("a commit does not read as one")
        return m.group(1).decode()

    def entries(self, tree: str) -> list[tuple[str, str, str]]:
        """(mode, name, sha) of each entry of a tree."""
        kind, body = self.read(tree)
        if kind != "tree":
            raise Unknown("a tree does not read as one")
        out = []
        i = 0
        while i < len(body):
            sp = body.index(b" ", i)
            nul = body.index(b"\x00", sp)
            out.append((body[i:sp].decode(), body[sp + 1:nul].decode("utf-8", "surrogateescape"),
                        body[nul + 1:nul + 21].hex()))
            i = nul + 21
        return out

    def at(self, commit: str, rel: str) -> tuple[str, str] | None:
        """(mode, sha) of the entry at `rel` (a /-separated path from the
        repo root) in a commit, or None when the commit has none there."""
        tree = self.tree_of(commit)
        mode = "40000"
        for part in [p for p in rel.split("/") if p]:
            if mode != "40000":
                return None
            hit = next(((m, s) for m, n, s in self.entries(tree) if n == part), None)
            if hit is None:
                return None
            mode, tree = hit
        return mode, tree

    def files(self, entry: tuple[str, str] | None, prefix: str) -> dict[str, tuple[str, str]]:
        """{path: (mode, sha)} of every file under an entry, at most FILES_MAX."""
        if entry is None:
            return {}
        mode, sha = entry
        if mode != "40000":
            return {prefix: (mode, sha)}
        out: dict[str, tuple[str, str]] = {}
        stack = [(prefix, sha)]
        while stack:
            where, tree = stack.pop()
            for m, n, s in self.entries(tree):
                path = f"{where}/{n}"
                if m == "40000":
                    stack.append((path, s))
                else:
                    out[path] = (m, s)
                if len(out) + len(stack) > FILES_MAX:
                    raise Unknown("the org directory holds more files than the guard compares")
        return out


def _inflate(raw: bytes, most: int) -> bytes:
    d = zlib.decompressobj()
    try:
        out = d.decompress(raw, most)
    except zlib.error:
        raise Unknown("an object does not inflate") from None
    if d.unconsumed_tail:
        raise Unknown("an object is larger than the guard reads")
    return out


def _inflate_stream(fh: Any, size: int) -> bytes:
    d = zlib.decompressobj()
    out = b""
    while len(out) < size or not d.eof:
        chunk = d.unconsumed_tail or fh.read(16 * 1024)
        if not chunk:
            break
        try:
            out += d.decompress(chunk, size + 1 - len(out))
        except zlib.error:
            raise Unknown("a pack object does not inflate") from None
        if len(out) > size:
            raise Unknown("a pack object is larger than it says")
        if d.eof:
            break
    if len(out) != size:
        raise Unknown("a pack object is cut short")
    return out


def _varint(data: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        c = data[i]
        i += 1
        n |= (c & 0x7F) << shift
        shift += 7
        if not c & 0x80:
            return n, i


def _apply_delta(base: bytes, delta: bytes) -> bytes:
    """git's delta format: the base's size, the result's, then copy and
    insert instructions."""
    try:
        src, i = _varint(delta, 0)
        dst, i = _varint(delta, i)
        if src != len(base) or dst > OBJECT_MAX:
            raise Unknown("a delta does not fit its base")
        out = bytearray()
        while i < len(delta):
            op = delta[i]
            i += 1
            if op & 0x80:
                off = size = 0
                for k in range(4):
                    if op & (1 << k):
                        off |= delta[i] << (8 * k)
                        i += 1
                for k in range(3):
                    if op & (1 << (4 + k)):
                        size |= delta[i] << (8 * k)
                        i += 1
                size = size or 0x10000
                if off + size > len(base):
                    raise Unknown("a delta copies past its base")
                out += base[off:off + size]
            elif op:
                out += delta[i:i + op]
                i += op
            else:
                raise Unknown("a delta holds an instruction git does not write")
        if len(out) != dst:
            raise Unknown("a delta does not make the size it says")
        return bytes(out)
    except IndexError:
        raise Unknown("a delta is cut short") from None


def compare(root: str | os.PathLike, target: str | None, rels: list[str], *,
            branch_first: bool = True, empty: bool = False) -> dict[str, Any]:
    """What switching the repo at `root` to `target` (None: HEAD) does to the
    files under each of `rels` (paths from the repo root): {"same": True}
    or {"same": False, "files": [paths that differ, sorted]}. `empty` is a
    switch to an orphan branch, whose tree holds nothing. Unknown when that
    cannot be told."""
    repo = Repo(root)
    head = repo.resolve("HEAD")
    if empty:
        to = None
    elif target is None:
        to = head
    else:
        to = repo.resolve(target, branch_first=branch_first)
    if to == head:
        return {"same": True, "files": [], "head": head, "target": to}
    changed: set[str] = set()
    for rel in rels:
        before = repo.files(repo.at(head, rel), rel)
        after = repo.files(repo.at(to, rel), rel) if to is not None else {}
        changed |= {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    return {"same": not changed, "files": sorted(changed), "head": head, "target": to}
