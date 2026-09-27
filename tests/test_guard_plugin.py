"""The guard as a Claude Code plugin hook, the off switch, and two adapter
fixes that came out of review.

  - plugins/nable/hooks/hooks.json runs what `nable guard install` writes for
    Claude Code, pinned to the plugin's release, marked `--via plugin`, and
    fail-safe: a uvx that cannot start never blocks a tool call
  - with a working `nable guard install` hook in a settings file Claude Code
    applies, the plugin's hook stands aside, silently and unrecorded; without
    one it judges, so every call is judged exactly once
  - `nable guard off` / `nable guard on` and FINOPS_GUARD=off silence every
    guard hook, unrecorded
  - install, status, doctor and uninstall know about the plugin
  - Gemini uninstall only removes a BeforeTool list nable created
  - Cursor on Windows gets PowerShell's call operator in front of a quoted
    program
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import finops.guard as g
import finops.guard_adapters as ga
import finops.guard_ledger as gl
import finops.guard_plugin as gp
import finops.setup_wizard as sw

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PLUGIN = ROOT / "plugins" / "nable"
DESTROY = "terraform destroy -auto-approve"


@pytest.fixture(autouse=True)
def _machine(tmp_path, monkeypatch):
    """An empty home and project; the lookups conftest sandboxes read this
    home instead, since reading it is what these tests are about."""
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(proj)
    for var in ("CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR", "FINOPS_DATA_DIR", "FINOPS_PROFILE",
                "FINOPS_GUARD", "FINOPS_GUARD_STRICT", "FINOPS_POLICY_ALLOWED_ACTIONS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gp, "_data_root_override", None)
    monkeypatch.setattr(gp, "_user_dir_override", None)
    monkeypatch.setattr("finops.welcome._fire_telemetry", lambda e, p: None)
    monkeypatch.setattr(g, "check_budget_gate", lambda *a, **k: None)


def _records() -> int:
    path = gl.ledger_path()
    return len(path.read_text().splitlines()) if path.exists() else 0


def _payload(tool="Bash", command=DESTROY, **extra) -> dict:
    tool_input = {"command": command} if tool == "Bash" else extra.pop("tool_input")
    return {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": tool_input, "cwd": str(Path.cwd()), **extra}


def _plugin_hook(payload: dict) -> str:
    out = io.StringIO()
    assert gp.run_hook(None, "plugin", io.StringIO(json.dumps(payload)), out, io.StringIO()) == 0
    return out.getvalue()


def _program(tmp_path: Path, name="finops") -> str:
    exe = tmp_path / "bin" / name
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return str(exe)


def _settings(path: Path, cmd: str | None = None, matcher: str = g._HOOK_MATCHER,
              plugin: bool | None = None) -> Path:
    doc: dict = {}
    if plugin is not None:
        doc["enabledPlugins"] = {gp.PLUGIN_KEY: plugin}
    if cmd is not None:
        doc["hooks"] = {"PreToolUse": [{"matcher": matcher, "hooks": [
            {"type": "command", "command": cmd, "timeout": 10}]}]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    return path


def _user() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _cli(args, home, stdin="", **env_extra):
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(SRC),
           "PYTHONDONTWRITEBYTECODE": "1", "NABLE_NO_TELEMETRY": "1", **env_extra}
    for var in ("CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR", "FINOPS_DATA_DIR", "FINOPS_GUARD",
                "FINOPS_PROFILE"):
        if var not in env_extra:
            env.pop(var, None)
    return subprocess.run([sys.executable, "-m", "finops.entry", *args], input=stdin,
                          capture_output=True, text=True, env=env, timeout=60, check=False,
                          cwd=str(home))


# ── hooks.json ─────────────────────────────────────────────────────────────────

def _hooks_json() -> dict:
    return json.loads((PLUGIN / "hooks" / "hooks.json").read_text())


def _plugin_json() -> dict:
    return json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())


def test_hooks_json_is_what_the_adapter_says_for_the_plugins_release():
    """One source for the hook: a bump that moves plugin.json and not the
    hook (or the other way) fails here, as does a hand edit of either."""
    version = _plugin_json()["version"]
    assert _hooks_json() == ga.plugin_hooks(version)


def test_the_hook_is_pinned_to_the_plugins_release_and_its_interpreter():
    args = _plugin_json()["mcpServers"]["nable"]["args"]
    [group] = _hooks_json()["hooks"]["PreToolUse"]
    [hook] = group["hooks"]
    cmd = hook["command"]
    assert f"{args[-1]} " in cmd.replace("--from ", ""), "the hook runs another release"
    assert ga.uvx_release(cmd) == _plugin_json()["version"] == g.__version__
    assert ga.uvx_pin(cmd) == "pinned"
    python = args[args.index("--python") + 1]
    assert f"--python {python} " in cmd, "a second interpreter means a second uvx environment"


def test_the_hook_matches_bash_mcp_and_the_file_tools_exactly_as_the_installer_does():
    [group] = _hooks_json()["hooks"]["PreToolUse"]
    assert group["matcher"] == g._HOOK_MATCHER
    for tool in ("Bash", "mcp__aws-api__call_aws", "mcp__plugin_nable_nable__get_cost_summary",
                 *gp.EDITOR_TOOLS):
        assert gp.matcher_covers(group["matcher"], tool)
    for tool in ("BashOutput", "KillBash", "Read", "WebFetch", "NotebookRead", "Glob"):
        assert not gp.matcher_covers(group["matcher"], tool)


def test_the_hook_has_the_installers_timeout_and_a_marker_and_a_fail_safe():
    [hook] = _hooks_json()["hooks"]["PreToolUse"][0]["hooks"]
    assert hook["type"] == "command"
    assert hook["timeout"] == g._timeout_for(g._UVX_HOOK_CMD) == 30
    assert " guard hook --via plugin" in hook["command"]
    assert hook["command"].endswith("; exit 0")
    assert "args" not in hook, "exec form has no shell, so no fail-safe"
    assert not gp.is_cli_hook(hook["command"]), "it must never stand aside for itself"


@pytest.mark.skipif(sys.platform == "win32", reason="runs the hook command in sh")
@pytest.mark.parametrize("uvx_body", [None, "#!/bin/sh\nexit 2\n", "#!/bin/sh\nexit 1\n"])
def test_a_uvx_that_cannot_start_nable_never_blocks_a_call(uvx_body, tmp_path):
    """Claude Code blocks the tool call on exit 2, which is what uvx exits
    with when PyPI is out of reach. Missing, failing or refusing: exit 0."""
    [hook] = _hooks_json()["hooks"]["PreToolUse"][0]["hooks"]
    uvx = tmp_path / "uvx"
    if uvx_body is not None:
        uvx.write_text(uvx_body)
        uvx.chmod(0o755)
    cmd = hook["command"].replace("uvx", str(uvx), 1)
    r = subprocess.run(["sh", "-c", cmd], input="{}", capture_output=True, text=True, check=False)
    assert r.returncode == 0 and r.stdout == ""


def test_the_real_cli_accepts_the_plugins_command_line(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    r = _cli(["guard", "hook", "--via", "plugin"], home, json.dumps(_payload()))
    assert r.returncode == 0, r.stderr
    verdict = json.loads(r.stdout)["hookSpecificOutput"]
    assert verdict["permissionDecision"] == "ask"
    # And through argparse, the path an unusual spelling takes.
    r = _cli(["guard", "hook", "--harness", "claude", "--via", "plugin", "--global"], home,
             json.dumps(_payload()))
    assert r.returncode == 0 and json.loads(r.stdout)["hookSpecificOutput"]


@pytest.mark.parametrize("argv,expected", [
    ([], (None, None)),
    (["--via", "plugin"], (None, "plugin")),
    (["--via=plugin"], (None, "plugin")),
    (["--harness", "cursor", "--via", "plugin"], ("cursor", "plugin")),
    (["--harness=claude"], ("claude", None)),
    (["--via", "other"], None),
    (["--via"], None),
    (["--harness", "nope"], None),
    (["--global"], None),
])
def test_hook_arguments(argv, expected):
    assert gp.parse_hook_args(argv) == expected


# ── one verdict, not two ──────────────────────────────────────────────────────

def test_the_plugin_judges_when_no_settings_hook_does():
    before = _records()
    body = json.loads(_plugin_hook(_payload()))["hookSpecificOutput"]
    assert body["permissionDecision"] == "ask"
    assert _records() == before + 1


@pytest.mark.parametrize("where", ["user", "project", "local", "config_dir", "project_dir_env"])
def test_the_plugin_stands_aside_for_a_working_settings_hook(where, tmp_path, monkeypatch):
    finops = _program(tmp_path)
    cmd = f"{finops} guard hook"
    if where == "user":
        _settings(_user(), cmd)
    elif where == "project":
        _settings(Path.cwd() / ".claude" / "settings.json", cmd)
    elif where == "local":
        _settings(Path.cwd() / ".claude" / "settings.local.json", cmd)
    elif where == "config_dir":
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
        _settings(tmp_path / "cc" / "settings.json", cmd)
    else:
        # The payload's cwd is a subdirectory after a `cd`; the project's
        # settings are where Claude Code says the project is.
        root = tmp_path / "root"
        _settings(root / ".claude" / "settings.json", cmd)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    before = _records()
    assert _plugin_hook(_payload()) == ""
    assert _records() == before, "the settings hook records this call; the plugin must not"


def test_the_uvx_form_install_writes_counts_as_working(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: "/usr/bin/uvx" if n == "uvx" else None)
    _settings(_user(), g._UVX_HOOK_CMD)
    assert gp.cli_hook_covers("Bash") == _user()


@pytest.mark.parametrize("case", ["dead", "bash_only_mcp_call", "plugin_command_copied",
                                  "user_file_ignored_under_config_dir", "unparseable"])
def test_the_plugin_judges_when_the_settings_hook_would_not(case, tmp_path, monkeypatch):
    finops = _program(tmp_path)
    payload = _payload()
    if case == "dead":
        _settings(_user(), f"{tmp_path}/gone/finops guard hook")
    elif case == "bash_only_mcp_call":
        # The matcher every release before MCP coverage wrote.
        _settings(_user(), f"{finops} guard hook", matcher="Bash")
        payload = _payload("mcp__aws-api__call_aws", tool_input={
            "cli_command": "aws ec2 terminate-instances --instance-ids i-1"})
    elif case == "plugin_command_copied":
        _settings(_user(), ga.plugin_hook_command().replace("uvx", finops, 1))
    elif case == "user_file_ignored_under_config_dir":
        _settings(_user(), f"{finops} guard hook")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    else:
        _user().parent.mkdir(parents=True)
        _user().write_text('{"hooks": ')
    before = _records()
    body = json.loads(_plugin_hook(payload))["hookSpecificOutput"]
    assert body["permissionDecision"] == "ask"
    assert _records() == before + 1


def test_a_plain_hook_is_never_silenced_by_a_settings_hook(tmp_path):
    """Standing aside is the plugin hook's alone: the settings hook itself
    (no --via) always judges."""
    _settings(_user(), f"{_program(tmp_path)} guard hook")
    out = io.StringIO()
    assert gp.run_hook(None, None, io.StringIO(json.dumps(_payload())), out) == 0
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_matcher_rules_agree_with_the_guards():
    for matcher in (None, "", "*", "Bash", "Bash|Edit", "Bash, Edit", g._HOOK_MATCHER,
                    "Bash|mcp__.*", "mcp__.*", "^mcp__aws", "[", 5, "Edit"):
        for tool in ("Bash", "BashOutput", "mcp__aws-api__call_aws", "Edit"):
            ours, theirs = gp.matcher_covers(matcher, tool), g.matcher_covers(matcher, tool)
            assert ours == theirs, (matcher, tool)


def test_the_silent_path_does_not_import_the_guard(tmp_path):
    """The point of answering early: a call the plugin stands aside for, or
    one while the guard is off, costs a JSON parse, not the guard's import."""
    home = tmp_path / "h"
    (home / ".finops").mkdir(parents=True)
    (home / ".finops" / "guard-off").write_text("off\n")
    probe = ("import sys, io, json; sys.argv = ['finops', 'guard', 'hook', '--via', 'plugin'];"
             "sys.stdin = io.StringIO(json.dumps({'tool_name': 'Bash', 'tool_input': "
             "{'command': 'terraform destroy'}}));"
             "from finops.setup_wizard import main\n"
             "try:\n    main()\nexcept SystemExit as e:\n    assert e.code == 0\n"
             "print(sorted(m for m in sys.modules if m.startswith('finops.guard')))")
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(SRC), "NABLE_NO_TELEMETRY": "1"}
    env.pop("FINOPS_DATA_DIR", None)
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env,
                       timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "['finops.guard_plugin']"


# ── the off switch ─────────────────────────────────────────────────────────────

def test_guard_off_silences_the_plugin_and_every_other_hook_unrecorded():
    gp.set_off(True)
    assert gp.off_flag_path() == Path.home() / ".finops" / "guard-off"
    before = _records()
    assert _plugin_hook(_payload()) == ""
    out = io.StringIO()
    assert ga.run_hook(None, io.StringIO(json.dumps(_payload())), out) == 0
    assert out.getvalue() == ""
    cursor = {"hook_event_name": "beforeShellExecution", "command": DESTROY, "cwd": "/tmp"}
    out = io.StringIO()
    assert ga.run_hook(None, io.StringIO(json.dumps(cursor)), out) == 0
    assert json.loads(out.getvalue()) == {"permission": "allow"}
    assert _records() == before
    gp.set_off(False)
    gp.set_off(False)                   # on twice is fine
    assert json.loads(_plugin_hook(_payload()))["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("value,off", [("off", True), ("OFF", True), ("0", True), ("false", True),
                                        ("on", False), ("", False), ("1", False)])
def test_finops_guard_env(value, off, monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD", value)
    assert gp.is_off() is off
    assert (_plugin_hook(_payload()) == "") is off


def test_the_flag_lives_in_the_data_dir_not_a_profile(tmp_path, monkeypatch):
    assert gp.data_root() == Path.home() / ".finops"
    assert not gp.data_root().exists(), "reading the switch creates nothing"
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    assert gp.off_flag_path() == tmp_path / "data" / "guard-off"
    monkeypatch.setenv("FINOPS_PROFILE", "work")
    assert gp.data_root() == tmp_path / "data", "the hook's environment has no profile"


def test_guard_off_and_on_through_the_real_cli(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    r = _cli(["guard", "off"], home)
    assert r.returncode == 0 and "Guard off" in r.stdout and "nable guard on" in r.stdout
    assert (home / ".finops" / "guard-off").exists()
    r = _cli(["guard", "status"], home)
    assert "The guard is off (nable guard off)" in r.stdout
    r = _cli(["guard", "hook", "--via", "plugin"], home, json.dumps(_payload()))
    assert r.returncode == 0 and r.stdout == ""
    r = _cli(["guard", "hook"], home, json.dumps(_payload()))
    assert r.returncode == 0 and r.stdout == ""
    r = _cli(["guard", "on"], home)
    assert r.returncode == 0 and "Guard on" in r.stdout
    assert not (home / ".finops" / "guard-off").exists()
    r = _cli(["guard", "on"], home, FINOPS_GUARD="off")
    assert "FINOPS_GUARD=off is set" in r.stdout
    r = _cli(["guard", "hook", "--via", "plugin"], home, json.dumps(_payload()))
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


# ── install, status, doctor, uninstall ────────────────────────────────────────

def _run(args, capsys) -> str:
    try:
        sw.main(args)
    except SystemExit as e:
        assert not e.code, e.code
    return capsys.readouterr().out


@pytest.mark.parametrize("where", ["user", "project", "local"])
def test_plugin_detection_follows_settings_precedence(where):
    path = {"user": _user(), "project": Path.cwd() / ".claude" / "settings.json",
            "local": Path.cwd() / ".claude" / "settings.local.json"}[where]
    _settings(path, plugin=True)
    assert gp.plugin_enabled() == path
    assert (gp.plugin_enabled(user_only=True) == path) is (where == "user")
    # A later scope turns it off again.
    _settings(Path.cwd() / ".claude" / "settings.local.json",
              plugin=False if where != "local" else True)
    assert (gp.plugin_enabled() is not None) is (where == "local")


def test_other_plugins_and_odd_values_are_not_ours():
    _user().parent.mkdir(parents=True)
    _user().write_text(json.dumps({"enabledPlugins": {"nable@elsewhere": True,
                                                      "other@nable": True}}))
    assert gp.plugin_enabled() is None
    _user().write_text(json.dumps({"enabledPlugins": {gp.PLUGIN_KEY: "yes"}}))
    assert gp.plugin_enabled() is None
    _user().write_text("not json")
    assert gp.plugin_enabled() is None


@pytest.mark.parametrize("args", [["guard", "install"], ["guard", "install", "--global"],
                                  ["guard", "install", "--harness", "claude"]])
def test_install_leaves_settings_alone_when_the_plugin_runs_the_guard(args, capsys):
    _settings(_user(), plugin=True)
    before = _user().read_text()
    out = _run(args, capsys)
    assert "already on via the Claude Code plugin" in out and "--force" in out
    assert _user().read_text() == before
    assert not (Path.cwd() / ".claude" / "settings.json").exists()


def test_install_force_writes_the_hook_anyway(capsys, monkeypatch):
    monkeypatch.setattr(g, "_hook_command", lambda: g._UVX_HOOK_CMD)
    _settings(_user(), plugin=True)
    out = _run(["guard", "install", "--global", "--force"], capsys)
    assert "installed" in out
    assert g.is_installed(_user())
    assert json.loads(_user().read_text())["enabledPlugins"] == {gp.PLUGIN_KEY: True}


def test_install_all_skips_claude_code_when_the_plugin_runs_the_guard(capsys):
    _settings(_user(), plugin=True)
    (Path.home() / ".gemini").mkdir()
    out = _run(["guard", "install", "--all", "--global"], capsys)
    assert "Claude Code" in out and "already on via the Claude Code plugin" in out
    assert not g.is_installed(_user())
    assert ga.state("gemini", True) == "installed"


def test_install_writes_the_hook_when_a_project_turns_the_plugin_off(capsys, monkeypatch):
    monkeypatch.setattr(g, "_hook_command", lambda: g._UVX_HOOK_CMD)
    _settings(_user(), plugin=True)
    _settings(Path.cwd() / ".claude" / "settings.local.json", plugin=False)
    out = _run(["guard", "install"], capsys)
    assert "already on via" not in out
    assert g.is_installed(Path.cwd() / ".claude" / "settings.json")


def test_status_says_on_via_the_plugin(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: "/usr/bin/uvx" if n == "uvx" else None)
    _settings(_user(), plugin=True)
    out = _run(["guard", "status"], capsys)
    assert re.search(r"plugin\s+on \(via the Claude Code plugin\)", out)
    assert "standing aside" not in out
    # With a settings hook as well, the status says which one judges.
    _settings(_user(), g._UVX_HOOK_CMD, plugin=True)
    out = _run(["guard", "status"], capsys)
    assert "standing aside wherever the settings hook above judges" in out
    # No uv, no hook: the plugin's guard cannot start.
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: None)
    _settings(_user(), plugin=True)
    assert "uvx is not on PATH" in _run(["guard", "status"], capsys)


def test_status_without_the_plugin_has_no_plugin_row(capsys):
    assert "via the Claude Code plugin" not in _run(["guard", "status"], capsys)


def test_doctor_counts_the_plugin_as_claude_code_coverage(capsys, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: "/usr/bin/uvx" if n == "uvx" else None)
    before = ga.with_plugin(g.doctor())
    assert "Claude Code: no working guard hook" in before["not_covered"]
    assert before["plugin"]["enabled"] is False
    _settings(_user(), plugin=True)
    d = ga.with_plugin(g.doctor())
    assert "Claude Code: Bash commands (via the Claude Code plugin)" in d["covered"]
    assert "Claude Code: MCP tool calls (via the Claude Code plugin)" in d["covered"]
    assert "Claude Code: no working guard hook" not in d["not_covered"]
    assert not any(f.startswith("nable guard install  (this project") for f in d["recommendations"])
    assert d["ok"] is True
    [row] = [r for r in d["surfaces"] if r.get("via") == "plugin"]
    assert row["harness"] == "claude-code" and row["scope"] == "plugin" and row["runs"]
    out = _run(["guard", "doctor"], capsys)
    assert re.search(r"Claude Code\s+plugin\s+on \(via the Claude Code plugin\), "
                     r"sees Bash \+ MCP", out)
    doc = json.loads(_run(["guard", "doctor", "--json"], capsys))
    assert doc["plugin"]["enabled"] is True


def test_doctor_while_off_covers_nothing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: "/usr/bin/uvx" if n == "uvx" else None)
    _settings(_user(), plugin=True)
    gp.set_off(True)
    d = ga.with_plugin(g.doctor())
    assert d["covered"] == [] and d["ok"] is False
    assert d["not_covered"][0].startswith("everything: the guard is off")
    assert d["recommendations"][0].startswith("nable guard on")


def test_doctor_names_a_missing_uv(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: None)
    _settings(_user(), plugin=True)
    d = ga.with_plugin(g.doctor())
    assert any("uv is not installed" in c for c in d["not_covered"])
    assert "Claude Code: no working guard hook" in d["not_covered"]


@pytest.mark.parametrize("args", [["guard", "uninstall", "--global"],
                                  ["guard", "uninstall", "--all", "--global"]])
def test_uninstall_says_how_to_turn_the_plugins_guard_off(args, capsys):
    _settings(_user(), g._UVX_HOOK_CMD, plugin=True)
    out = _run(args, capsys)
    assert not g.is_installed(_user())
    flat = " ".join(out.split())
    assert "plugin still runs the guard" in flat
    assert "nable guard off" in flat and f"/plugin disable {gp.PLUGIN_KEY}" in flat


def test_uninstall_without_the_plugin_says_nothing_about_it(capsys):
    assert "plugin" not in _run(["guard", "uninstall", "--global"], capsys)


# ── the plugin's other files ──────────────────────────────────────────────────

def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text()
    assert text.startswith("---\n"), path
    head = text.split("---\n", 2)[1]
    return dict(line.split(": ", 1) for line in head.strip().splitlines())


def test_the_guard_command_and_skill_are_well_formed():
    cmd = _frontmatter(PLUGIN / "commands" / "guard.md")
    assert cmd["description"]
    skill = _frontmatter(PLUGIN / "skills" / "cloud-costs" / "SKILL.md")
    assert skill["name"] == "cloud-costs" and len(skill["description"]) < 1024
    for path in (PLUGIN / "commands" / "guard.md", PLUGIN / "skills" / "cloud-costs" / "SKILL.md",
                 PLUGIN / "README.md"):
        text = path.read_text()
        assert "\u2014" not in text and "!" not in text.replace("<!--", ""), path


def test_the_skill_names_only_tools_the_server_has():
    from finops import server
    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    text = (PLUGIN / "skills" / "cloud-costs" / "SKILL.md").read_text()
    named = set(re.findall(r"`([a-z_]+)`", text)) - {"root_cause"}
    assert named, "the skill names no tools"
    assert named <= registered, named - registered


def test_the_descriptions_lead_with_the_guard():
    lead = "Prices what your coding agent is about to do before it runs"
    assert _plugin_json()["description"].startswith(lead)
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert market["plugins"][0]["description"].startswith(lead)


# ── Gemini: an empty BeforeTool someone else wrote stays ──────────────────────

def _gemini_uvx(monkeypatch):
    uvx = Path.home() / "uv" / "bin" / "uvx"
    uvx.parent.mkdir(parents=True, exist_ok=True)
    uvx.touch(mode=0o755)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(Path.home() / "not-a-tmp"))
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: str(uvx) if n == "uvx" else None)


@pytest.mark.parametrize("theirs", [
    {"hooks": {"BeforeTool": []}},
    {"hooks": {"BeforeTool": []}, "model": {"name": "gemini-2.5-pro"}},
    {"hooks": {}},
    {"hooks": {"AfterTool": []}},
    {},
    {"model": {"name": "gemini-2.5-pro"}},
])
def test_gemini_uninstall_leaves_the_file_as_it_found_it(theirs, monkeypatch):
    _gemini_uvx(monkeypatch)
    path = ga.hooks_path("gemini", True)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(theirs))
    assert ga.install("gemini", True)[0] == "new"
    assert ga.uninstall("gemini", True)[0] is True
    assert json.loads(path.read_text()) == theirs
    assert ga._created(path) == set(), "the marker is cleared with the hook"


def test_gemini_without_a_marker_keeps_the_empty_list(monkeypatch):
    """An install from before the marker: nable cannot tell who made the
    list, so it stays (an empty list is harmless; deleting theirs is not)."""
    _gemini_uvx(monkeypatch)
    path = ga.hooks_path("gemini", True)
    ga.install("gemini", True)
    ga._created_path().unlink()
    ga.uninstall("gemini", True)
    assert json.loads(path.read_text()) == {"hooks": {"BeforeTool": []}}


def test_gemini_marker_is_keyed_by_settings_file(monkeypatch):
    _gemini_uvx(monkeypatch)
    user, proj = ga.hooks_path("gemini", True), ga.hooks_path("gemini", False)
    proj.parent.mkdir(parents=True)
    proj.write_text(json.dumps({"hooks": {"BeforeTool": []}}))
    ga.install("gemini", True)
    ga.install("gemini", False)
    assert ga._created(user) == {"hooks", "hooks.BeforeTool"}
    assert ga._created(proj) == set()
    ga.uninstall("gemini", False)
    assert json.loads(proj.read_text()) == {"hooks": {"BeforeTool": []}}
    ga.uninstall("gemini", True)
    assert json.loads(user.read_text()) == {}


# ── Cursor on Windows: PowerShell's call operator ─────────────────────────────

SPACED = r"C:\Program Files\uv\uvx.exe"


def _windows(monkeypatch, uvx=SPACED):
    monkeypatch.setattr(ga.sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: uvx if n == "uvx" else None)
    monkeypatch.setattr(g, "_is_ephemeral", lambda p: False)
    monkeypatch.setattr(g, "_hook_command", lambda: g._UVX_HOOK_CMD)
    monkeypatch.setattr(ga, "_runnable", lambda c: True)


def test_cursor_on_windows_calls_a_quoted_program(monkeypatch):
    _windows(monkeypatch)
    cmd = ga.hook_command("cursor", True)
    assert cmd == f'& "{SPACED}"' + g._UVX_HOOK_CMD[len("uvx"):]
    assert ga.uvx_pin(cmd) == "pinned"
    assert ga.uvx_release(ga._fail_safe(cmd)) == g.__version__


@pytest.mark.parametrize("harness", ["codex", "copilot", "gemini", "cline"])
def test_only_cursor_gets_the_call_operator(harness, monkeypatch):
    _windows(monkeypatch)
    assert not ga.hook_command(harness, True).startswith("&")


def test_a_path_without_spaces_and_project_scope_stay_bare(monkeypatch):
    _windows(monkeypatch, uvx=r"C:\uv\uvx.exe")
    assert ga.hook_command("cursor", True) == r"C:\uv\uvx.exe" + g._UVX_HOOK_CMD[len("uvx"):]
    _windows(monkeypatch)
    assert ga.hook_command("cursor", False) == g._UVX_HOOK_CMD


def test_posix_keeps_the_quoted_form(monkeypatch):
    monkeypatch.setattr(ga.sys, "platform", "darwin")
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: "/Users/a b/.local/bin/uvx"
                        if n == "uvx" else None)
    monkeypatch.setattr(g, "_is_ephemeral", lambda p: False)
    monkeypatch.setattr(g, "_hook_command", lambda: g._UVX_HOOK_CMD)
    assert ga.hook_command("cursor", True).startswith('"/Users/a b/.local/bin/uvx" --from')


def test_cursor_on_windows_repairs_the_quoted_form_earlier_releases_wrote(monkeypatch):
    _windows(monkeypatch)
    path = ga.hooks_path("cursor", True)
    path.parent.mkdir(parents=True)
    old = ga._fail_safe(f'"{SPACED}"' + g._UVX_HOOK_CMD[len("uvx"):])
    path.write_text(json.dumps({"version": 1, "hooks": {
        e: [{"command": old, "timeout": 30, "failClosed": False}] for e in ga._CURSOR_EVENTS}}))
    assert ga.install("cursor", True)[0] == "repaired"
    doc = json.loads(path.read_text())
    assert {c for c in ga._cursor_commands(doc)} == {ga._fail_safe(ga.hook_command("cursor", True))}
    assert ga.install("cursor", True)[0] == "already"


def test_a_call_operator_command_reads_as_runnable_and_pinned(tmp_path):
    exe = tmp_path / "a b" / "uvx"
    exe.parent.mkdir()
    exe.touch(mode=0o755)
    cmd = f'& "{exe}" --from finops-mcp=={g.__version__} finops guard hook; exit 0'
    assert ga._runnable(cmd) and gp._runs(cmd)
    assert ga.uvx_pin(cmd) == "pinned"
    assert not ga._runnable(f'& "{tmp_path}/gone/uvx" --from finops-mcp finops guard hook')
