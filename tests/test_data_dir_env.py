"""FINOPS_DATA_DIR, read by one helper (D139).

Every module that keeps a file in nable's data directory reads the variable
through finops._datadir.env_data_dir, so they agree, and the guard protects
the directory the others write. A leading ~ expands; a relative value is
refused: each reader uses its own default instead, a CLI says so once on
stderr, the guard hook never fails over it and says nothing, and `nable
guard doctor` names the value and the directory used.
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys
from pathlib import Path

import pytest

from finops import _datadir

SRC = Path(__file__).resolve().parents[1] / "src" / "finops"
_READS = {"get", "getenv", "pop", "setdefault", "__getitem__"}


def _reads_the_variable(tree: ast.AST) -> list[int]:
    """Line numbers where FINOPS_DATA_DIR is read from an environment:
    os.environ.get/pop/setdefault("..."), os.getenv("..."), environ["..."]
    and "..." in os.environ. A mention in text or a table is not a read."""
    out: list[int] = []

    def named(node: ast.AST) -> bool:
        return isinstance(node, ast.Constant) and node.value == _datadir.ENV

    for node in ast.walk(tree):
        call = isinstance(node, ast.Call) and any(named(a) for a in node.args) and (
            getattr(node.func, "attr", None) or getattr(node.func, "id", "")) in _READS
        item = isinstance(node, ast.Subscript) and named(node.slice)
        member = isinstance(node, ast.Compare) and named(node.left) and any(
            isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
        if call or item or member:
            out.append(node.lineno)
    return out


def test_no_module_but_the_helper_reads_finops_data_dir():
    found = {}
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "_datadir.py" and path.parent == SRC:
            continue
        lines = _reads_the_variable(ast.parse(path.read_text(encoding="utf-8")))
        if lines:
            found[str(path.relative_to(SRC))] = lines
    assert found == {}, f"read FINOPS_DATA_DIR through finops._datadir: {found}"


def test_the_ratchet_sees_each_way_of_reading_it():
    code = ("import os\nos.environ.get('FINOPS_DATA_DIR')\nos.getenv('FINOPS_DATA_DIR', 'x')\n"
            "os.environ['FINOPS_DATA_DIR']\n'FINOPS_DATA_DIR' in os.environ\n"
            "DOC = {'FINOPS_DATA_DIR': 'a mention'}\n")
    assert _reads_the_variable(ast.parse(code)) == [2, 3, 4, 5]


# ── the helper ───────────────────────────────────────────────────────────────

def test_unset_or_blank_is_the_default(tmp_path):
    d = tmp_path / "default"
    assert _datadir.env_data_dir(d, environ={}) == (d, None)
    assert _datadir.env_data_dir(d, environ={"FINOPS_DATA_DIR": "  "}) == (d, None)


def test_a_leading_tilde_expands(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    got, why = _datadir.env_data_dir(Path("/unused"), environ={"FINOPS_DATA_DIR": "~/nd"})
    assert (got, why) == (tmp_path / "home" / "nd", None)
    assert _datadir.env_data_dir(Path("/x"), environ={"FINOPS_DATA_DIR": "/abs/d"}) == (
        Path("/abs/d"), None)


@pytest.mark.parametrize("value", ["rel/dir", "./d", "d", "~nosuchuser-nable-test/d", "../d"])
def test_a_relative_value_is_refused_and_the_default_used(tmp_path, value):
    d = tmp_path / "default"
    got, why = _datadir.env_data_dir(d, environ={"FINOPS_DATA_DIR": value})
    assert got == d and why is not None
    assert "not an absolute path" in why and str(d) in why and value in why


@pytest.mark.parametrize("value", [" {d}", "{d} ", "\t{d}", "{d}\n", " {d} "])
def test_a_value_with_whitespace_around_it_is_refused_never_stripped(tmp_path, value):
    """The shell splits `$FINOPS_DATA_DIR/guard-off` on that whitespace, so
    a stripped value would be the directory a command writes unasked while
    the guard reads the value as written: refused, the default used."""
    d = tmp_path / "default"
    v = value.format(d=tmp_path / "nd")
    got, why = _datadir.env_data_dir(d, environ={"FINOPS_DATA_DIR": v})
    assert got == d and why is not None and "whitespace" in why and str(d) in why


@pytest.mark.parametrize("value", [" {d}", "{d} ", "\t{d}"])
@pytest.mark.parametrize("cmd", ["touch $FINOPS_DATA_DIR/guard-off",
                                 "touch ${FINOPS_DATA_DIR}/guard-off",
                                 "rm $FINOPS_DATA_DIR/guard-ledger.jsonl"])
def test_a_shell_reading_a_spaced_value_never_reaches_the_guards_own_files(
        tmp_path, monkeypatch, value, cmd):
    """With FINOPS_DATA_DIR=' /abs/nd' the shell writes /abs/nd/guard-off.
    That is not where nable keeps its switch (the default is used), so the
    guard is not turned off; the real switch, under the default, still asks."""
    import finops.guard as g
    import finops.guard_plugin as gp
    from finops import ai_budget

    home = tmp_path / "home"
    home.mkdir()
    nd = tmp_path / "nd"
    nd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    for var in ("FINOPS_PROFILE", "FINOPS_ORG_DIR", "CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR",
                "XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                "GIT_CONFIG_NOSYSTEM"):
        monkeypatch.delenv(var, raising=False)
    from finops.storage import db
    monkeypatch.setattr(gp, "_data_root_override", None)
    monkeypatch.setattr(db, "_DATA_DIR", None)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})
    monkeypatch.setenv("FINOPS_DATA_DIR", value.format(d=nd))
    assert gp.off_flag_path() == home / ".finops" / "guard-off"
    (nd / "guard-off").touch()                 # what the shell's command would write
    assert gp.is_off() is False
    real = cmd.replace("$FINOPS_DATA_DIR", str(home / ".finops")).replace(
        "${FINOPS_DATA_DIR}", str(home / ".finops"))
    verdict = g.gate_command(real, "s1", cwd=str(tmp_path), record=False)
    assert verdict is not None and verdict["decision"] in ("ask", "deny"), (real, verdict)


# ── every reader ─────────────────────────────────────────────────────────────

@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.chdir(work)
    for var in ("FINOPS_PROFILE", "XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                "GIT_CONFIG_NOSYSTEM"):
        monkeypatch.delenv(var, raising=False)
    import finops.guard_plugin as gp
    from finops.storage import db
    monkeypatch.setattr(gp, "_data_root_override", None)
    monkeypatch.setattr(db, "_DATA_DIR", None)
    return h


def _readers() -> dict[str, tuple]:
    """Each reader: (what it returns for its data dir, its default under HOME)."""
    from finops import ai_budget, cache, guard_ledger, guard_paths, guard_plugin, harness_usage
    from finops import setup_wizard as wizard
    from finops.security import vault
    from finops.storage import db

    return {
        "guard_ledger": (lambda: guard_ledger._data_dir(), ".finops"),
        "storage.db": (lambda: db.data_dir(), ".finops"),
        "guard_plugin": (lambda: guard_plugin.data_root(), ".finops"),
        "guard_paths": (lambda: guard_paths.data_dir(), ".finops"),
        "vault": (lambda: vault._data_dir(), ".finops"),
        "ai_budget": (lambda: ai_budget._data_dir(), ".nable"),
        "harness_usage": (lambda: harness_usage._cursor_cache_path().parent, ".nable"),
        "cache": (lambda: Path(cache._cache_dir()), ".nable"),
        "wizard._nable_dirs": (lambda: wizard._nable_dirs()[0], ".finops"),
        "wizard._state_dirs": (lambda: [d for d in wizard._state_dirs()
                                        if d.name in ("nd", ".finops")][-1], ".finops"),
    }


def test_every_reader_expands_a_tilde(home, monkeypatch):
    monkeypatch.setenv("FINOPS_DATA_DIR", "~/nd")
    from finops.storage import db
    for name, (read, _default) in _readers().items():
        monkeypatch.setattr(db, "_DATA_DIR", None)
        assert read() == home / "nd", name


def test_every_reader_refuses_a_relative_value_and_uses_its_default(home, monkeypatch):
    monkeypatch.setenv("FINOPS_DATA_DIR", "rel/dir")
    from finops.storage import db
    for name, (read, default) in _readers().items():
        monkeypatch.setattr(db, "_DATA_DIR", None)
        assert read() == home / default, name
    assert not (Path.cwd() / "rel").exists(), "a reader made the relative directory"


def test_the_guard_protects_the_directory_actually_written(home, monkeypatch):
    from finops import guard_ledger, guard_paths
    monkeypatch.setenv("FINOPS_DATA_DIR", "rel/dir")
    written = guard_ledger._data_dir()
    assert written == home / ".finops" == guard_paths.data_dir()
    for name in ("guard-approvals.json", "guard-person.json"):
        hit = guard_paths.match(str(written / name), str(Path.cwd()))
        assert hit is not None, name
    assert guard_paths.match(str(Path.cwd() / "rel" / "dir" / "guard-approvals.json"),
                             str(Path.cwd())) is None
    ai = guard_paths.match(str(home / ".nable" / "ai-budget.json"), str(Path.cwd()))
    assert ai is not None


# ── said once by a CLI, never by the hook, named by the doctor ───────────────

def test_a_cli_says_it_once_on_stderr(home, monkeypatch, capsys):
    from finops import setup_wizard
    monkeypatch.setenv("FINOPS_DATA_DIR", "rel/dir")
    monkeypatch.setenv("FINOPS_ORG_DIR", str(home / "org"))
    monkeypatch.setattr(_datadir, "_SAID", set())
    for _ in range(2):
        try:
            setup_wizard.main(["org", "status"])
        except SystemExit as e:
            assert not e.code
    err = capsys.readouterr().err
    assert err.count("FINOPS_DATA_DIR='rel/dir' is not an absolute path") == 1
    assert str(home / ".finops") in err


def test_the_guard_hook_never_fails_and_says_nothing(home, monkeypatch):
    import finops.guard_plugin as gp
    monkeypatch.setenv("FINOPS_DATA_DIR", "rel/dir")
    monkeypatch.setattr(_datadir, "_SAID", set())
    target = home / ".finops" / "guard-approvals.json"
    payload = {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Write",
               "tool_input": {"file_path": str(target), "content": "{}"},
               "cwd": str(Path.cwd())}
    out, err = io.StringIO(), io.StringIO()
    assert gp.run_hook(None, "plugin", io.StringIO(json.dumps(payload)), out, err) == 0
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert err.getvalue() == ""
    out = io.StringIO()
    bash = {**payload, "tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert gp.run_hook(None, "plugin", io.StringIO(json.dumps(bash)), out, io.StringIO()) == 0
    assert _datadir._SAID == set()


def test_the_doctor_names_the_value_and_the_directory_used(home, monkeypatch):
    from finops import guard
    monkeypatch.setenv("FINOPS_DATA_DIR", "rel/dir")
    d = guard.doctor()
    assert d["data_dir"]["value"] == "rel/dir"
    assert d["data_dir"]["dir"] == str(home / ".finops")
    assert "not an absolute path" in d["data_dir"]["refused"]
    assert any("FINOPS_DATA_DIR='rel/dir'" in g for g in d["not_covered"])
    monkeypatch.setenv("FINOPS_DATA_DIR", str(home / "abs"))
    assert guard._doctor_data_dir([], lambda *a: None) == {
        "value": str(home / "abs"), "dir": str(home / "abs"), "refused": None}


def test_the_helper_is_standard_library_only():
    """The guard hook reads it on every call."""
    tree = ast.parse((SRC / "_datadir.py").read_text(encoding="utf-8"))
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
            for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree)
             if isinstance(n, ast.ImportFrom) and n.module and not n.level}
    assert mods <= set(sys.stdlib_module_names) | {"__future__"}, mods
    assert os.sep  # the module is importable on every platform the guard runs on
