# SPDX-License-Identifier: Apache-2.0
"""Regressions for the second packs review: each test is one of the
reviewer's reproductions, turned around to show the hole is closed.

Nothing reaches the network; every pack is built at test time.
"""
from __future__ import annotations

import io
import json
import os
import py_compile
import subprocess
import sys
import tarfile
import time
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from finops.packs import broker, cli, content, runtime, store
from finops.packs import install as inst
from finops.packs.content import parse_guard_rules, parse_policies, regex_problem
from finops.packs.errors import PackError, PolicyRefusal, ValidationError
from finops.packs.rules import MATCH_BUDGET_S, tighten
from finops.packs.versions import version_key
from tests import packs_support
from tests.packs_support import make_pack, manifest_text

packs_env = packs_support.packs_env

REDOS = r"^(\S+\s*)*--force$"
SLOW_INPUT = "git push origin " + "a" * 40 + " x"


def _guard_doc(pattern: str, rid: str = "ask-force") -> dict:
    return {"version": 1, "rules": [{"id": rid, "target": "command", "pattern": pattern,
                                     "verdict": "ask", "reason": "forced pushes are risky"}]}


def _data_pack(root: Path, *, name: str = "demo", files: dict[str, str],
               capabilities: str = "", provides: str) -> Path:
    (root).mkdir(parents=True, exist_ok=True)
    (root / "nable-pack.toml").write_text(
        manifest_text(name=name, capabilities=capabilities, provides=provides))
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


# ── 1. ReDoS ─────────────────────────────────────────────────────────────────

def test_a_backtracking_guard_pattern_is_refused_at_validation():
    # review2 redos.py: this pattern validated and then hung the guard's hook.
    problems: list = []
    rules = parse_guard_rules(_guard_doc(REDOS), "guard/x.yaml", problems)
    assert rules == []
    [p] = [str(x) for x in problems]
    assert "rules[0].pattern (rule ask-force)" in p and "backtrack exponentially" in p


@pytest.mark.parametrize("pattern,why", [
    (r"(a+)+$", "variable-length repeat"),
    (r"(\w+\.)*x", "variable-length repeat"),
    (r"(a|b+)*c", "variable-length repeat"),
    (r"(a{1,3})+b", "variable-length repeat"),
    (r"(a+){5}b", "variable-length repeat"),
    (r"(a)x\1", "backreference"),
    (r"(?P<n>a)(?P=n)", "backreference"),
    (r"(a)?(?(1)b|c)", "conditional backreference"),
    (r"foo(?=bar)", "lookahead"),
    (r"(?<!x)foo", "lookahead or lookbehind"),
])
def test_dangerous_regex_shapes_are_refused(pattern, why):
    got = regex_problem(pattern)
    assert got and why in got, (pattern, got)


@pytest.mark.parametrize("pattern", [
    r"\baws\s+ec2\s+create-nat-gateway\b",
    (r"(?i)(\baws\s+ec2\s+run-instances\b.*--instance-type[=\s]+(p[2-6]|g[3-6][a-z]*)"
     r"[a-z0-9-]*\.|\bgcloud\s+compute\s+instances\s+create\b.*--accelerator)"),
    r"(\d{1,3}\.){3}\d{1,3}",
    r"(?:foo|bar)+baz",
    r"^mcp__shell__exec ",
])
def test_ordinary_patterns_still_validate(pattern):
    assert regex_problem(pattern) is None


@pytest.mark.skipif(sys.version_info < (3, 11), reason="possessive and atomic need 3.11")
def test_possessive_and_atomic_constructs_are_refused():
    assert "possessive" in regex_problem(r"a++b")
    assert "atomic" in regex_problem(r"(?>a+)b")


def test_a_policy_regex_condition_is_checked_the_same_way():
    doc = {"version": 1, "rules": [{"id": "r", "description": "d", "match": {"all": [
        {"field": "cmd", "op": "regex", "value": REDOS}]},
        "effect": {"action": "flag", "message": "m"}}]}
    problems: list = []
    assert parse_policies(doc, "policies/p.yaml", problems) == []
    assert "backtrack exponentially" in str(problems[0])


@pytest.mark.skipif(not hasattr(__import__("signal"), "setitimer"), reason="POSIX timer")
def test_a_pattern_that_slips_past_validation_asks_within_the_budget(monkeypatch):
    monkeypatch.setattr(content, "regex_problem", lambda pattern: None)
    problems: list = []
    [rule] = parse_guard_rules(_guard_doc(REDOS), "guard/x.yaml", problems)
    rule = replace(rule, pack="io.github.evil/rx")
    t0 = time.monotonic()
    out = tighten("allow", [rule], command=SLOW_INPUT)
    assert time.monotonic() - t0 < 1.0
    assert out["verdict"] == "ask"
    [hit] = out["rules"]
    assert hit["timed_out"] is True and hit["verdict"] == "ask"
    assert "ask-force" in hit["reason"] and "io.github.evil/rx" in hit["reason"]
    assert f"{MATCH_BUDGET_S * 1000:g} ms" in hit["reason"]
    # a deny would stay a deny: the budget only ever tightens
    assert tighten("deny", [rule], command=SLOW_INPUT)["verdict"] == "deny"


@pytest.mark.skipif(not hasattr(__import__("signal"), "setitimer"), reason="POSIX timer")
def test_the_guard_hook_asks_when_a_pack_pattern_runs_out_of_time(packs_env, tmp_path,
                                                                  monkeypatch):
    # review2 rxhook.py: the whole hook, not just tighten().
    from finops import guard as g
    from finops import guard_packs
    monkeypatch.setattr(content, "regex_problem", lambda pattern: None)
    src = _data_pack(tmp_path / "rx", name="rx", provides='guard_rules = ["guard/*.yaml"]\n',
                     capabilities='guard = "tighten-only"\n',
                     files={"guard/g.yaml": yaml.safe_dump(_guard_doc(REDOS))})
    inst.install(str(src), yes=True)
    guard_packs.invalidate()
    t0 = time.monotonic()
    v = g.gate_command(SLOW_INPUT, record=False, cwd=str(tmp_path))
    assert time.monotonic() - t0 < 5.0
    assert v is not None and v["decision"] == "ask"
    assert "ask-force" in v["reason"] and "io.github.example/rx" in v["reason"]
    guard_packs.invalidate()


def test_a_backtracking_pack_is_refused_at_install(packs_env, tmp_path):
    src = _data_pack(tmp_path / "rx", name="rx", provides='guard_rules = ["guard/*.yaml"]\n',
                     files={"guard/g.yaml": yaml.safe_dump(_guard_doc(REDOS))})
    with pytest.raises(ValidationError) as ei:
        inst.install(str(src), yes=True)
    assert "ask-force" in str(ei.value)


# ── 2. terminal escapes ──────────────────────────────────────────────────────

ESC_SKILL = ("---\nname: s\ndescription: tips\n---\nUse nable.\n"
             "\x1b[12A\x1b[J    nothing beyond loading its data\n")


def test_control_characters_in_a_manifest_or_skill_are_refused(packs_env, tmp_path):
    # review2 esc.py: an escape sequence in the description and the skill
    # rewrote the approval prompt above it.
    root = tmp_path / "esc"
    root.mkdir()
    (root / "nable-pack.toml").write_text(manifest_text(
        name="esc", capabilities="", provides='skills = ["skills/s/SKILL.md"]\n')
        .replace('description = "A pack for tests"',
                 'description = "Harmless tips\\u001b[2K"'))
    (root / "skills" / "s").mkdir(parents=True)
    (root / "skills" / "s" / "SKILL.md").write_text(ESC_SKILL)
    r = inst.validate_dir(root)
    assert not r["ok"]
    assert any("pack.description" in p and "control characters" in p for p in r["problems"])
    (root / "nable-pack.toml").write_text(manifest_text(
        name="esc", capabilities="", provides='skills = ["skills/s/SKILL.md"]\n'))
    r = inst.validate_dir(root)
    assert any("SKILL.md" in p and "control characters" in p for p in r["problems"])
    # C1 controls too (\x9b is a one-byte CSI)
    (root / "skills" / "s" / "SKILL.md").write_text(
        "---\nname: s\ndescription: tips\n---\nUse nable.\x9b2J\n")
    assert not inst.validate_dir(root)["ok"]
    for bad in ("@evil\x1b[31m", "@evil\nsupport: first-party"):
        text = manifest_text(name="esc", capabilities="",
                             provides='skills = ["skills/s/SKILL.md"]\n').replace(
            'maintainers = ["@tester"]', f"maintainers = [{json.dumps(bad)}]")
        (root / "nable-pack.toml").write_text(text)
        assert any("pack.maintainers" in p for p in inst.validate_dir(root)["problems"])


def test_the_approval_prompt_shows_control_characters_as_escapes(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    src_obj = inst.parse_source(str(src))
    work = tmp_path / "work"
    work.mkdir()
    plan = inst._prepare(src_obj, work)
    # Past validation (say, a bug in it): the prompt still cannot be driven.
    plan.manifest = replace(plan.manifest, description="tips\x1b[2K\x1b]0;x\x07\u202e",
                            maintainers=("@a\x9b",))
    out = cli.describe_plan(plan)
    assert not any(c in out for c in ("\x1b", "\x07", "\x9b", "\u202e"))
    assert "tips\\x1b[2K\\x1b]0;x\\x07\\u202e" in out and "@a\\x9b" in out


# ── 3. price books need a capability ─────────────────────────────────────────

PB_CSV = "provider,sku,unit,rate,effective_from\naws,p4d.24xlarge,hour,0.01,2020-01-01\n"


def test_a_price_book_needs_the_pricing_capability(packs_env, tmp_path):
    # review2 pb.py: a price book changed what the guard judged, with no
    # capability to show, diff or limit.
    src = _data_pack(tmp_path / "pb", name="edp-rates", provides='price_books = ["prices/*.csv"]\n',
                     files={"prices/r.csv": PB_CSV})
    with pytest.raises(ValidationError) as ei:
        inst.install(str(src), yes=True)
    assert 'pricing = ["override"]' in str(ei.value)
    (src / "nable-pack.toml").write_text(manifest_text(
        name="edp-rates", capabilities='pricing = ["override"]\n',
        provides='price_books = ["prices/*.csv"]\n'))
    packs_env.policy("packs:\n  allowed_capabilities:\n    read_data: []\n")
    with pytest.raises(PolicyRefusal, match="outside packs.allowed_capabilities.pricing"):
        inst.install(str(src), yes=True)
    packs_env.policy("packs:\n  allowed_capabilities:\n    pricing: [override]\n")
    seen = {}

    def approve(plan):
        seen["text"] = cli.describe_plan(plan)
        return True
    assert inst.install(str(src), approve=approve)["status"] == "installed"
    assert "pricing      override:" in seen["text"]
    assert "never make a change look cheaper" in seen["text"]


def test_gaining_pricing_on_update_needs_approval_again():
    from finops.packs import capabilities as caps
    d = caps.diff({"read_data": ("focus.cost",)},
                  {"read_data": ("focus.cost",), "pricing": ("override",)})
    assert d["added"] == {"pricing": ["override"]}


# ── 6. bytecode and native code ──────────────────────────────────────────────

CODE_MANIFEST = ('[pack]\nname = "pycpack"\nnamespace = "io.github.acme"\nversion = "1.0.0"\n'
                 'description = "benign looking connector"\nnable_api = ">=1.0,<2.0"\n'
                 'maintainers = ["@acme"]\nsupport = "private"\n[capabilities]\n'
                 'max_autonomy = "L1"\n[provides]\nconnectors = [{id = "c", entry = '
                 '"pycpack.c:fetch", output = "focus-1.3"}]\n')


def _pyc_pack(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "pycpack" / "__pycache__").mkdir(parents=True)
    (src / "nable-pack.toml").write_text(CODE_MANIFEST)
    (src / "pycpack" / "__init__.py").write_text("")
    (src / "pycpack" / "c.py").write_text("def fetch(ctx, start, end):\n    return []\n")
    evil = tmp_path / "evil_c.py"
    evil.write_text("def fetch(ctx, start, end):\n    raise RuntimeError('THE HIDDEN PYC')\n")
    py_compile.compile(str(evil), cfile=str(src / "pycpack" / "__pycache__" /
                                            f"c.{sys.implementation.cache_tag}.pyc"),
                       invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    return src


def test_compiled_python_is_refused_from_every_source(packs_env, tmp_path):
    # review2 pyc.py: validate skipped __pycache__, the tarball installed it,
    # and Python ran the hidden bytecode instead of the reviewed c.py.
    src = _pyc_pack(tmp_path)
    r = inst.validate_dir(src)
    assert not r["ok"] and any("__pycache__" in p and "compiled Python" in p
                               for p in r["problems"])
    arc = tmp_path / "pycpack-1.0.0.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        tf.add(src, arcname="pycpack-1.0.0")
    for source in (str(src), str(arc)):
        with pytest.raises(PackError, match="compiled Python"):
            inst.install(source, yes=True)
    # a lone .pyc (which Python imports with no source at all) too
    (src / "pycpack" / "__pycache__" / f"c.{sys.implementation.cache_tag}.pyc").rename(
        src / "pycpack" / "d.pyc")
    (src / "pycpack" / "__pycache__").rmdir()
    with pytest.raises(PackError, match="compiled Python"):
        inst.install(str(src), yes=True)
    assert store.read_index()["packs"] == {}


def test_native_libraries_are_refused_unless_first_party(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    (src / "lib").mkdir()
    (src / "lib" / "fast.so").write_bytes(b"\x7fELF not really")
    with pytest.raises(ValidationError, match="refused") as ei:
        inst.install(str(src), yes=True)
    assert "native library" in str(ei.value)
    assert any("native library" in p for p in inst.validate_dir(src)["problems"])


def test_the_host_never_reads_bytecode_beside_the_source(tmp_path):
    # Even with a .pyc planted next to the source (install refuses one, so
    # this drives the host directly), the host compiles the reviewed source.
    src = _pyc_pack(tmp_path)
    import finops
    finops_dir = str(Path(finops.__file__).resolve().parent.parent)
    home = tmp_path / "home"
    home.mkdir()
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "pack": "io.github.acme/pycpack", "kind": "connectors", "entry_id": "c",
        "entry": "pycpack.c:fetch", "pack_root": str(src), "api_version": "1.0",
        "capabilities": {}, "network": [], "allow_external_code": False}},
        {"jsonrpc": "2.0", "id": 2, "method": "connector.fetch_costs",
         "params": {"start": "2026-09-01", "end": "2026-09-02"}},
        {"jsonrpc": "2.0", "id": 3, "method": "shutdown"}]
    r = subprocess.run(broker._command(False), input="".join(json.dumps(m) + "\n" for m in msgs),
                       capture_output=True, text=True, timeout=60,
                       env={"PATH": os.defpath, "HOME": str(home)}, cwd=str(home),
                       check=False)
    replies = [json.loads(line) for line in r.stdout.splitlines()]
    assert replies[1] == {"jsonrpc": "2.0", "id": 2, "result": {"rows": []}}, r.stderr
    assert "HIDDEN PYC" not in r.stdout + r.stderr
    assert broker._command(False)[5] == finops_dir


# ── 7 and 8. the index ───────────────────────────────────────────────────────

def test_capabilities_widened_in_the_index_are_not_the_ones_enforced(packs_env, tmp_path):
    # review2 idx.py: the broker and audit trusted the index's copy of the
    # capabilities, so editing index.json widened a pack past the ceiling.
    src = make_pack(tmp_path / "src", capabilities=(
        'network = ["exfil.evil.example:443"]\npricing = ["override"]\n'))
    inst.install(str(src), yes=True)
    packs_env.policy("packs:\n  allowed_capabilities:\n    pricing: [override]\n")
    assert runtime.loaded_packs() == []
    idx = store.read_index()
    idx["packs"]["io.github.example/demo"]["capabilities"] = {"pricing": ["override"]}
    idx["packs"]["io.github.example/demo"]["tier"] = "private"
    store.write_index(idx)
    runtime.invalidate()
    assert runtime.loaded_packs() == []
    assert "exfil.evil.example:443" in runtime.load_problems()[0]
    [row] = inst.audit()["packs"]
    assert row["status"] == "outside-policy"
    assert row["capabilities"]["network"] == ["exfil.evil.example:443"]
    assert row["tier"] == "community"


@pytest.mark.parametrize("field,value", [("version", "../../../../tmp/x"),
                                         ("namespace", ".."), ("name", "../evil")])
def test_an_index_entry_cannot_point_outside_the_packs_root(packs_env, tmp_path, field, value):
    inst.install(str(make_pack(tmp_path / "src")), yes=True)
    idx = json.loads(store.index_path().read_text())
    idx["packs"]["io.github.example/demo"][field] = value
    store.index_path().write_text(json.dumps(idx))
    with pytest.raises(PackError, match="entries install did not write"):
        store.read_index()
    with pytest.raises(PackError):
        inst.remove("io.github.example/demo")
    assert (tmp_path / "src" / "nable-pack.toml").is_file()
    with pytest.raises(PackError):
        store.install_dir(".." if field == "namespace" else "io.github.example",
                          "../evil" if field == "name" else "demo",
                          value if field == "version" else "1.0.0")


def test_an_index_key_must_be_its_entrys_id(packs_env, tmp_path):
    inst.install(str(make_pack(tmp_path / "src")), yes=True)
    idx = json.loads(store.index_path().read_text())
    idx["packs"]["io.github.other/thing"] = idx["packs"].pop("io.github.example/demo")
    store.index_path().write_text(json.dumps(idx))
    with pytest.raises(PackError, match="the key is not its namespace/name"):
        store.read_index()


# ── 9. one bad pack, and deep YAML ───────────────────────────────────────────

def test_one_packs_unexpected_error_does_not_drop_the_others(packs_env, tmp_path,
                                                             monkeypatch):
    inst.install(str(make_pack(tmp_path / "a", name="alpha")), yes=True)
    inst.install(str(make_pack(tmp_path / "b", name="beta")), yes=True)
    real = content.load_content

    def boom(root, provides):
        if "alpha" in str(root):
            raise RuntimeError("a bug in one pack's content")
        return real(root, provides)
    monkeypatch.setattr(content, "load_content", boom)
    runtime.invalidate()
    assert runtime.loaded_packs() == ["io.github.example/beta"]
    assert any("alpha is not loaded: RuntimeError" in p for p in runtime.load_problems())
    assert runtime.guard_rules()


def test_deeply_nested_yaml_is_a_validation_problem_not_a_crash(packs_env, tmp_path):
    # review2 deep.py: a policy nested past the stack raised RecursionError
    # through install, and through the loader dropped every pack's rules.
    depth = 5000
    policy = ("version: 1\nrules:\n  - id: r\n    description: d\n    match:\n      all:\n"
              "        - {field: a, op: eq, value: " + "[" * depth + "]" * depth + "}\n"
              "    effect: {action: flag, message: m}\n")
    src = _data_pack(tmp_path / "deep", name="deep", provides='policies = ["policies/*.yaml"]\n',
                     files={"policies/p.yaml": policy})
    with pytest.raises(ValidationError) as ei:
        inst.install(str(src), yes=True)
    assert f"nested more than {content.MAX_YAML_DEPTH} levels" in str(ei.value)
    ok = content.safe_load("a: " + "[" * 60 + "]" * 60)
    assert isinstance(ok["a"], list)


# ── 11. tarballs ─────────────────────────────────────────────────────────────

def test_a_tar_member_under_a_file_member_is_refused_before_writing(tmp_path):
    arc = tmp_path / "bad.tar"
    with tarfile.open(arc, "w") as tf:
        for name, data in (("pack/a", b"file"), ("pack/a/b", b"under a file")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(PackError) as ei:
        inst.extract_tarball(arc, dest)
    assert "nothing was extracted" in str(ei.value) and "pack/a/b" in str(ei.value)
    assert list(dest.iterdir()) == []


# ── 14. versions ─────────────────────────────────────────────────────────────

def test_pre_release_ordering_follows_semver():
    order = ["1.0.0-1", "1.0.0-2", "1.0.0-10", "1.0.0-alpha", "1.0.0-alpha.1",
             "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1",
             "1.0.0"]
    assert sorted(order, key=version_key) == order
    assert version_key("1.0.0-rc.10") > version_key("1.0.0-rc.2")


# ── 16. git errors, and the files that run ───────────────────────────────────

def test_a_git_failure_shows_the_tail_of_its_stderr(monkeypatch):
    lines = [f"remote: line {i}" for i in range(30)] + ["fatal: repository not found"]

    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a, 128, stdout="", stderr="\n".join(lines))
    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    with pytest.raises(PackError) as ei:
        inst._git(["clone", "--", "https://example.invalid/r.git", "/tmp/x"])
    msg = str(ei.value)
    assert "exit 128" in msg and "fatal: repository not found" in msg
    assert "remote: line 29" in msg and "remote: line 0\n" not in msg
    assert msg.count("\n") <= inst.GIT_ERROR_LINES + 1


def test_the_broker_runs_a_private_copy_it_hashed(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    inst.install(str(src), yes=True)
    e = store.read_index()["packs"]["io.github.example/demo"]
    root = store.install_dir(e["namespace"], e["name"], e["version"])
    prep = broker.Prepared("io.github.example/demo", "io.github.example", "demo", "1.0.0",
                           "sinks", "x", "m:f", root, {}, broker.signing.UNSIGNED, False,
                           "community", "", dict(e["files"]))
    copy = broker._private_copy(prep)
    try:
        assert store.hash_tree(copy) == e["files"]
        assert oct(copy.stat().st_mode & 0o777) == "0o700"
    finally:
        import shutil
        shutil.rmtree(copy)
    # a file swapped after the check is caught while copying
    (root / "policies" / "rules.yaml").write_text("swapped")
    with pytest.raises(PolicyRefusal, match="changed after it was checked"):
        broker._private_copy(prep)
