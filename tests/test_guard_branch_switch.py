# SPDX-License-Identifier: Apache-2.0
"""A git branch switch that rewrites the org model's files asks (D157).

`git checkout BRANCH`, `git checkout COMMIT` and `git switch [--detach] REF`
rewrite every tracked file that differs between HEAD and the target,
nable.org/authority.yaml among them. The guard reads the repo's own objects
and refs in process (guard_git), under its meter, with no subprocess and no
network, and compares the org directory's tree at HEAD with the target's:

  - a different tree asks, naming the files that differ
  - the same tree with no local changes under it stays silent
  - creating a branch at HEAD (checkout -b, switch -c, --detach) stays silent
  - when it cannot tell (an unresolvable ref, a missing object, a spent
    meter) it asks whenever the repo holds org files, as git rewrites do

Every repo here is built in pure Python, byte for byte as git writes one:
loose objects, a pack with OFS and REF deltas and its version 2 index,
packed-refs, and a worktree. git itself is never run.
"""
from __future__ import annotations

import hashlib
import os
import re
import socket
import subprocess
import time
import zlib
from pathlib import Path

import pytest

import finops.guard as g
import finops.guard_plugin as gp
from finops import ai_budget, guard_git

OFF = ('# nable org model v1\n- fact: authority\n  subject: {kind: action_class, id: rds_ri}\n'
       '  value: {rung: "off"}\n  source: human\n  status: confirmed\n')
WIDE = "# nable org model v1\n[]\n"
TEAMS = "# nable org model v1\n- fact: team\n  subject: {kind: team, id: payments}\n"
_ORG_FILE_NAMED = re.compile(r"(?<![\w.-])nable\.org/authority\.yaml(?![\w-])")
_TEAMS_NAMED = re.compile(r"(?<![\w.-])nable\.org/teams\.yaml(?![\w-])")


# ── a git repository, written as git writes one ──────────────────────────────

def _obj(kind: str, body: bytes) -> tuple[str, bytes]:
    raw = f"{kind} {len(body)}".encode() + b"\x00" + body
    return hashlib.sha1(raw).hexdigest(), raw


class GitRepo:
    """A repo on disk, written without git: objects (loose, or packed with
    deltas), refs, packed-refs, HEAD's reflog and a working tree."""

    def __init__(self, root: Path):
        self.root = root
        self.git = root / ".git"
        (self.git / "objects" / "pack").mkdir(parents=True)
        (self.git / "refs" / "heads").mkdir(parents=True)
        (self.git / "refs" / "tags").mkdir(parents=True)
        (self.git / "logs").mkdir()
        self.bodies: dict[str, tuple[str, bytes]] = {}

    def put(self, kind: str, body: bytes, *, loose: bool = True) -> str:
        sha, raw = _obj(kind, body)
        self.bodies[sha] = (kind, body)
        if loose:
            d = self.git / "objects" / sha[:2]
            d.mkdir(exist_ok=True)
            (d / sha[2:]).write_bytes(zlib.compress(raw))
        return sha

    def blob(self, text: str, **kw) -> str:
        return self.put("blob", text.encode(), **kw)

    def tree(self, entries: dict[str, tuple[str, str]], **kw) -> str:
        """{name: (mode, sha)}; mode 40000 for a tree."""
        def key(item):
            name, (mode, _sha) = item
            return name + ("/" if mode == "40000" else "")
        body = b"".join(f"{mode} {name}".encode() + b"\x00" + bytes.fromhex(sha)
                        for name, (mode, sha) in sorted(entries.items(), key=key))
        return self.put("tree", body, **kw)

    def commit(self, tree: str, parent: str | None = None, msg: str = "c", **kw) -> str:
        body = (f"tree {tree}\n" + (f"parent {parent}\n" if parent else "")
                + "author T <t@example.com> 1700000000 +0000\n"
                  "committer T <t@example.com> 1700000000 +0000\n\n" + msg + "\n").encode()
        return self.put("commit", body, **kw)

    def snapshot(self, files: dict[str, str], parent: str | None = None, *,
                 loose: bool = True) -> str:
        """A commit of `files` ({path: text}, one level of directories)."""
        top: dict[str, tuple[str, str]] = {}
        dirs: dict[str, dict[str, tuple[str, str]]] = {}
        for path, text in files.items():
            if "/" in path:
                d, name = path.split("/", 1)
                dirs.setdefault(d, {})[name] = ("100644", self.blob(text, loose=loose))
            else:
                top[path] = ("100644", self.blob(text, loose=loose))
        for d, entries in dirs.items():
            top[d] = ("40000", self.tree(entries, loose=loose))
        return self.commit(self.tree(top, loose=loose), parent, loose=loose)

    def ref(self, name: str, sha: str) -> None:
        p = self.git / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(sha + "\n")

    def head(self, branch: str) -> None:
        (self.git / "HEAD").write_text(f"ref: refs/heads/{branch}\n")

    def packed_refs(self, refs: dict[str, str]) -> None:
        lines = ["# pack-refs with: peeled fully-peeled sorted"]
        lines += [f"{sha} {name}" for name, sha in sorted(refs.items())]
        (self.git / "packed-refs").write_text("\n".join(lines) + "\n")

    def checkout_files(self, files: dict[str, str], reflog: str = "") -> None:
        """The working tree as HEAD's commit holds it, then HEAD's reflog,
        written after it (git_changed reads the reflog's time)."""
        old = time.time() - 3600
        for path, text in files.items():
            p = self.root / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            os.utime(p, (old, old))
            os.utime(p.parent, (old, old))
        log = self.git / "logs" / "HEAD"
        log.write_text(reflog or (f"{'0' * 40} {'1' * 40} T <t@example.com> 1700000000 +0000"
                                  "\tcommit (initial): c\n"))
        os.utime(log, (old + 60, old + 60))

    def pack(self, objects: list[tuple[str, str | None, str | None]]) -> None:
        """A pack and its version 2 index: each (sha, OFS base sha or None,
        REF base sha or None) of objects already put. A delta copies the
        base's first half and inserts the rest of the object."""
        out = bytearray(b"PACK" + (2).to_bytes(4, "big") + len(objects).to_bytes(4, "big"))
        where: dict[str, int] = {}
        crcs: dict[str, int] = {}
        for sha, ofs_base, ref_base in objects:
            kind, body = self.bodies[sha]
            start = len(out)
            base = ofs_base or ref_base
            if base:
                data = _delta(self.bodies[base][1], body)
                t = 6 if ofs_base else 7
            else:
                data = body
                t = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}[kind]
            size = len(data)
            head = bytearray([(t << 4) | (size & 15) | (0x80 if size > 15 else 0)])
            size >>= 4
            while size:
                head.append((size & 0x7F) | (0x80 if size > 0x7F else 0))
                size >>= 7
            if ofs_base:
                rel = start - where[ofs_base]
                enc = [rel & 0x7F]
                rel >>= 7
                while rel:
                    rel -= 1
                    enc.insert(0, 0x80 | (rel & 0x7F))
                    rel >>= 7
                head += bytes(enc)
            elif ref_base:
                head += bytes.fromhex(ref_base)
            entry = bytes(head) + zlib.compress(data)
            out += entry
            where[sha] = start
            crcs[sha] = zlib.crc32(entry)
        out += hashlib.sha1(bytes(out)).digest()
        name = hashlib.sha1(bytes(out)).hexdigest()
        d = self.git / "objects" / "pack"
        (d / f"pack-{name}.pack").write_bytes(bytes(out))
        shas = sorted(where)
        idx = bytearray(b"\xfftOc" + (2).to_bytes(4, "big"))
        for b in range(256):
            idx += sum(1 for s in shas if int(s[:2], 16) <= b).to_bytes(4, "big")
        for s in shas:
            idx += bytes.fromhex(s)
        for s in shas:
            idx += crcs[s].to_bytes(4, "big")
        for s in shas:
            idx += where[s].to_bytes(4, "big")
        idx += bytes(out[-20:])
        idx += hashlib.sha1(bytes(idx)).digest()
        (d / f"pack-{name}.idx").write_bytes(bytes(idx))


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _delta(base: bytes, target: bytes) -> bytes:
    """Copy the base's first half (as much of it as the target shares),
    insert the rest."""
    keep = 0
    while keep < min(len(base) // 2, len(target)) and base[keep] == target[keep]:
        keep += 1
    out = bytearray(_varint(len(base)) + _varint(len(target)))
    if keep:
        op, args = 0x80, bytearray()             # offset 0: no offset bytes
        for k in range(3):
            byte = (keep >> (8 * k)) & 0xFF
            if byte:
                op |= 1 << (4 + k)
                args.append(byte)
        out += bytes([op]) + args
    rest = target[keep:]
    for i in range(0, len(rest), 127):
        chunk = rest[i:i + 127]
        out += bytes([len(chunk)]) + chunk
    return bytes(out)


MAIN = {"nable.org/authority.yaml": OFF, "nable.org/teams.yaml": TEAMS, "app.py": "x = 1\n",
        "README.md": "hello\n"}
EVIL = {**MAIN, "nable.org/authority.yaml": WIDE}
DOCS = {**MAIN, "README.md": "hello, docs\n"}


@pytest.fixture
def machine(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("FINOPS_DATA_DIR", "FINOPS_PROFILE", "FINOPS_ORG_DIR", "XDG_CONFIG_HOME",
                "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "CLAUDE_CONFIG_DIR",
                "CLAUDE_PROJECT_DIR", "GIT_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("PATH", "")                  # git is never what answers
    monkeypatch.setattr(gp, "_data_root_override", None)
    monkeypatch.setattr(gp, "_user_dir_override", None)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})
    return tmp_path


@pytest.fixture
def repo(machine, monkeypatch):
    """main: the org model with authority off. evil: authority widened.
    docs: only the README differs. All loose."""
    r = GitRepo(machine / "repo")
    main = r.snapshot(MAIN)
    evil = r.snapshot(EVIL, main)
    docs = r.snapshot(DOCS, main)
    r.ref("refs/heads/main", main)
    r.ref("refs/heads/evil", evil)
    r.ref("refs/heads/docs", docs)
    r.head("main")
    r.checkout_files(MAIN)
    r.shas = {"main": main, "evil": evil, "docs": docs}
    monkeypatch.chdir(r.root)
    return r


def _ask(cmd: str, cwd) -> dict | None:
    return g.gate_command(cmd, "s1", cwd=str(cwd), record=False)


# ── a different tree asks, naming the files ──────────────────────────────────

@pytest.mark.parametrize("cmd", ["git checkout evil", "git switch evil",
                                 "git checkout --detach evil", "git switch --detach evil",
                                 "git checkout -b mine evil", "git switch -c mine evil",
                                 "git -C . checkout evil", "git checkout evil --"])
def test_a_switch_to_a_tree_whose_org_files_differ_asks_naming_them(repo, cmd):
    v = _ask(cmd, repo.root)
    assert v is not None and v["decision"] == "ask", (cmd, v)
    assert _ORG_FILE_NAMED.search(v["reason"]), v["reason"]
    assert not _TEAMS_NAMED.search(v["reason"]), "an unchanged file is not named"


def test_a_switch_from_a_subdirectory_reads_the_repo_root(repo):
    (repo.root / "src").mkdir()
    v = _ask("git checkout evil", repo.root / "src")
    assert v is not None and _ORG_FILE_NAMED.search(v["reason"])


def test_an_annotated_tag_an_abbreviation_and_ancestry_are_followed(repo):
    tag = repo.put("tag", (f"object {repo.shas['evil']}\ntype commit\ntag v2\n"
                           "tagger T <t@example.com> 1700000000 +0000\n\nv2\n").encode())
    repo.ref("refs/tags/v2", tag)
    assert _ask("git checkout v2", repo.root)["decision"] == "ask"
    assert _ask(f"git checkout {repo.shas['evil'][:10]}", repo.root)["decision"] == "ask"
    assert _ask(f"git checkout {repo.shas['evil']}", repo.root)["decision"] == "ask"
    # evil's parent is main: the same tree.
    assert _ask("git checkout evil~1", repo.root) is None
    assert _ask("git checkout evil^", repo.root) is None


def test_the_previous_branch_is_read_from_heads_reflog(repo):
    repo.checkout_files(MAIN, reflog=(
        f"{'0' * 40} {repo.shas['evil']} T <t@example.com> 1700000000 +0000\tcommit: c\n"
        f"{repo.shas['evil']} {repo.shas['main']} T <t@example.com> 1700000100 +0000"
        "\tcheckout: moving from evil to main\n"))
    v = _ask("git checkout -", repo.root)
    assert v is not None and _ORG_FILE_NAMED.search(v["reason"])


def test_a_switch_to_an_orphan_branch_rewrites_every_org_file(repo):
    v = _ask("git switch --orphan fresh", repo.root)
    assert v is not None and _ORG_FILE_NAMED.search(v["reason"])
    assert _TEAMS_NAMED.search(v["reason"])


# ── the same tree stays silent ───────────────────────────────────────────────

@pytest.mark.parametrize("cmd", ["git checkout docs", "git switch docs", "git checkout main",
                                 "git switch --detach docs", "git checkout HEAD"])
def test_the_same_org_tree_with_no_local_changes_stays_silent(repo, cmd):
    assert _ask(cmd, repo.root) is None, cmd


def test_the_same_tree_with_local_changes_under_it_asks(repo):
    """A forced switch would throw the changes away; the guard cannot tell
    which switch it is, so it asks."""
    (repo.root / "nable.org" / "teams.yaml").write_text(TEAMS + "# edited\n")
    v = _ask("git checkout docs", repo.root)
    assert v is not None and v["decision"] == "ask"
    assert "changed since HEAD last moved" in v["reason"]


@pytest.mark.parametrize("cmd", ["git checkout -b feature", "git switch -c feature",
                                 "git checkout --detach", "git switch --detach",
                                 "git checkout -B feature", "git checkout --orphan fresh",
                                 "git checkout"])
def test_creating_a_branch_at_head_stays_silent(repo, cmd):
    (repo.root / "nable.org" / "teams.yaml").write_text(TEAMS + "# edited\n")
    assert _ask(cmd, repo.root) is None, cmd


@pytest.mark.parametrize("cmd", ["git checkout -- nable.org/authority.yaml",
                                 "git checkout evil -- nable.org/authority.yaml",
                                 "git checkout evil nable.org/authority.yaml"])
def test_checking_out_paths_is_judged_as_before(repo, cmd):
    """A path checked out is the protected file itself, as before: never
    read as a switch."""
    v = _ask(cmd, repo.root)
    assert v is not None and v["decision"] == "ask"
    assert _ORG_FILE_NAMED.search(v["protected_path"]) and "a switch to" not in v["reason"]


# ── packs, packed-refs and worktrees ─────────────────────────────────────────

def test_a_pack_with_ofs_and_ref_deltas_is_read(machine, monkeypatch):
    r = GitRepo(machine / "packed")
    main = r.snapshot(MAIN)
    # evil's objects in a pack: its authority.yaml an OFS delta of main's
    # (copied into the pack first), its org tree a REF delta of main's
    # (loose).
    main_auth = r.blob(OFF)
    main_org = r.tree({"authority.yaml": ("100644", main_auth),
                       "teams.yaml": ("100644", r.blob(TEAMS))})
    evil_auth = r.blob(WIDE, loose=False)
    evil_org = r.tree({"authority.yaml": ("100644", evil_auth),
                       "teams.yaml": ("100644", r.blob(TEAMS))}, loose=False)
    top = r.tree({"nable.org": ("40000", evil_org), "app.py": ("100644", r.blob("x = 1\n")),
                  "README.md": ("100644", r.blob("hello\n"))}, loose=False)
    evil = r.commit(top, main, loose=False)
    r.pack([(main_auth, None, None), (evil_auth, main_auth, None),
            (evil_org, None, main_org), (top, None, None), (evil, None, None)])
    assert not (r.git / "objects" / evil[:2] / evil[2:]).exists()
    r.packed_refs({"refs/heads/main": main, "refs/heads/evil": evil})
    r.head("main")
    r.checkout_files(MAIN)
    got = guard_git.compare(r.root, "evil", ["nable.org"])
    assert got == {"same": False, "files": ["nable.org/authority.yaml"], "head": main,
                   "target": evil}
    v = _ask("git switch evil", r.root)
    assert v is not None and _ORG_FILE_NAMED.search(v["reason"])


def test_a_worktree_reads_its_own_head_and_the_common_objects(repo, machine):
    wt = machine / "wt"
    wt.mkdir()
    meta = repo.git / "worktrees" / "wt"
    (meta / "logs").mkdir(parents=True)
    (meta / "HEAD").write_text(f"{repo.shas['docs']}\n")
    (meta / "commondir").write_text("../..\n")
    (meta / "gitdir").write_text(str(wt / ".git") + "\n")
    (wt / ".git").write_text(f"gitdir: {meta}\n")
    old = time.time() - 3600
    for path, text in DOCS.items():
        p = wt / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        os.utime(p, (old, old))
        os.utime(p.parent, (old, old))
    (meta / "logs" / "HEAD").write_text("x\n")
    os.utime(meta / "logs" / "HEAD", (old + 60, old + 60))
    assert guard_git.Repo(wt).resolve("HEAD") == repo.shas["docs"]
    v = _ask("git checkout evil", wt)
    assert v is not None and _ORG_FILE_NAMED.search(v["reason"])
    assert _ask("git checkout main", wt) is None


# ── when it cannot tell ──────────────────────────────────────────────────────

def test_an_unresolvable_ref_asks_when_the_repo_holds_org_files(repo, machine):
    v = _ask("git switch no-such-branch", repo.root)
    assert v is not None and v["decision"] == "ask"
    assert "could not tell" in v["reason"]
    plain = GitRepo(machine / "plain")
    sha = plain.snapshot({"app.py": "x = 1\n"})
    plain.ref("refs/heads/main", sha)
    plain.head("main")
    plain.checkout_files({"app.py": "x = 1\n"})
    assert _ask("git switch no-such-branch", plain.root) is None
    assert _ask("git checkout evil", plain.root) is None


def test_a_missing_object_asks(repo):
    repo.ref("refs/heads/gone", "0123456789abcdef0123456789abcdef01234567")
    v = _ask("git checkout gone", repo.root)
    assert v is not None and v["decision"] == "ask" and "could not tell" in v["reason"]


def test_a_spent_meter_asks(repo, monkeypatch):
    monkeypatch.setattr(g, "_GIT_UNITS", 2)
    v = _ask("git checkout docs", repo.root)
    assert v is not None and v["decision"] == "ask" and "could not tell" in v["reason"]


def test_a_corrupt_object_asks(repo):
    sha = repo.shas["evil"]
    (repo.git / "objects" / sha[:2] / sha[2:]).write_bytes(b"not zlib")
    v = _ask("git checkout evil", repo.root)
    assert v is not None and v["decision"] == "ask"


# ── nothing runs, nothing leaves ─────────────────────────────────────────────

def test_no_subprocess_runs_and_no_connection_opens(repo, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the branch switch check ran a process or opened a connection")
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    assert _ask("git checkout evil", repo.root)["decision"] == "ask"
    assert _ask("git checkout docs", repo.root) is None


def test_the_reader_is_standard_library_only():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(guard_git))
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not names & {"subprocess", "socket", "urllib", "urllib.request", "dulwich",
                        "pygit2", "git"}


def test_doctor_no_longer_lists_the_branch_switch_as_a_gap(repo):
    gaps = " ".join(g.doctor()["not_covered"])
    assert "branch switch" not in gaps


def test_the_hook_asks_for_the_switch_through_the_plugin(repo):
    import io
    import json
    out = io.StringIO()
    payload = {"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": "git switch evil"}, "cwd": str(repo.root)}
    assert gp.run_hook(None, "plugin", io.StringIO(json.dumps(payload)), out,
                       io.StringIO()) == 0
    said = json.loads(out.getvalue())["hookSpecificOutput"]
    assert said["permissionDecision"] == "ask"
    assert _ORG_FILE_NAMED.search(said["permissionDecisionReason"])


def test_a_git_directory_git_would_not_recognise_changes_nothing(machine):
    """No HEAD in .git: git refuses every switch there, so nothing is written."""
    root = machine / "broken"
    (root / ".git").mkdir(parents=True)
    (root / "nable.org").mkdir()
    (root / "nable.org" / "authority.yaml").write_text(OFF)
    assert _ask("git checkout main", root) is None
    assert _ask("git switch main", root) is None
