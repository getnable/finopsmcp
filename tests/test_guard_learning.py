"""Decisions become policy: the post hook, ask outcomes, and inferred thresholds.

What has to stay true:
  - the post hook records `ran`, linked to the ask it answers (tool_use_id,
    else session and command within ten minutes), once, on the fast path
    (no guard import), never on stdout, always exit 0
  - an ask with no `ran` reads as declined only once its session has been
    quiet 30 minutes and only where a post hook is known to work; otherwise
    unknown; nothing about a decline is ever written
  - `nable guard install` writes the post hook beside the pre hook, upgrades
    an existing install in place, and uninstall takes it away; the plugin's
    hooks.json carries it too; Cursor and Copilot get theirs
  - inference proposes one threshold after 5 approvals over 7 days and not
    before, never for a one-way door, not after a decline or a revert, and a
    tighter one after 3 declines; proposals are asked alone, with evidence,
    default no when they loosen and yes when they tighten
  - `nable learn rollback|restore` is a person's decision, and the guard asks
    before an agent runs it
  - end to end: repeated approvals lead to one proposed threshold; until a
    person confirms it nothing changes, and once they do the guard stops
    asking about that command
"""
from __future__ import annotations

import io
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import finops.guard as g
import finops.guard_adapters as ga
import finops.guard_ledger as gl
import finops.guard_outcome as go
import finops.guard_plugin as gp
from finops import ai_budget, org
from finops.org.cli import _who as human
from finops.recommendations.learning import policy_inference as pi
from finops.recommendations.learning.signal import guard_signal

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
LAUNCH = "aws ec2 run-instances --instance-type m5.2xlarge --count 4"   # ~$1,121/mo
SMALL = "aws ec2 run-instances --instance-type m5.2xlarge"              # ~$280/mo
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture(autouse=True)
def _machine(tmp_path, monkeypatch):
    home, proj = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(proj)
    for var in ("CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR", "FINOPS_DATA_DIR", "FINOPS_PROFILE",
                "FINOPS_GUARD", "FINOPS_GUARD_STRICT", "FINOPS_POLICY_ALLOWED_ACTIONS",
                "FINOPS_POLICY_MAX_AUTO_USD", "FINOPS_GUARD_PROD_PATTERNS", "FINOPS_GUARD_TEAM",
                "FINOPS_POLICY_VELOCITY_CAP_USD", "FINOPS_POLICY_LOOP_COUNT",
                "FINOPS_POLICY_FILE", "FINOPS_GUARD_ACCOUNT", "CURSOR_ADMIN_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_ACCOUNTS_FILE", str(tmp_path / "no-accounts.yaml"))
    monkeypatch.setattr(gp, "_data_root_override", None)
    monkeypatch.setattr(gp, "_user_dir_override", None)
    monkeypatch.setattr("finops.welcome._fire_telemetry", lambda e, p: None)
    monkeypatch.setattr(g, "check_budget_gate", lambda *a, **k: None)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})


@pytest.fixture
def clock(monkeypatch):
    """Set the time the ledger and the post hook see: clock(days_ago, minutes=0)."""
    def at(days_ago: float = 0, minutes: float = 0) -> datetime:
        when = NOW - timedelta(days=days_ago) + timedelta(minutes=minutes)

        class _At(datetime):
            @classmethod
            def now(cls, tz=None):
                return when if tz is None else when.astimezone(tz)
        monkeypatch.setattr(gl, "datetime", _At)
        monkeypatch.setattr(go, "datetime", _At)
        return when
    yield at
    monkeypatch.setattr(gl, "datetime", datetime)
    monkeypatch.setattr(go, "datetime", datetime)


def _hook(payload: dict, *, post: bool = False, via: str | None = "plugin",
          harness: str | None = None) -> str:
    out = io.StringIO()
    assert gp.run_hook(harness, via, io.StringIO(json.dumps(payload)), out, io.StringIO(),
                       post=post) == 0
    return out.getvalue()


def _pre(command: str = LAUNCH, *, session: str = "s1", tuid: str | None = "toolu_1") -> str:
    p = {"session_id": session, "hook_event_name": "PreToolUse", "tool_name": "Bash",
         "tool_input": {"command": command}, "cwd": str(Path.cwd())}
    if tuid:
        p["tool_use_id"] = tuid
    return _hook(p)


def _post(command: str = LAUNCH, *, session: str = "s1", tuid: str | None = "toolu_1",
          event: str = "PostToolUse") -> str:
    p = {"session_id": session, "hook_event_name": event, "tool_name": "Bash",
         "tool_input": {"command": command}, "tool_response": {"stdout": "ok"}}
    if tuid:
        p["tool_use_id"] = tuid
    return _hook(p, post=True)


def _asked(out: str) -> bool:
    return bool(out) and json.loads(out).get("hookSpecificOutput", {}).get(
        "permissionDecision") == "ask"


def _all() -> list[dict]:
    return gl.read(outcomes=True)


def _outcomes() -> list[dict]:
    return [r for r in _all() if gl.is_outcome(r)]


# ── A. the post hook ──────────────────────────────────────────────────────────

def test_the_post_hook_records_ran_linked_to_the_ask_by_tool_use_id():
    assert _asked(_pre())
    assert _post() == ""                                   # nothing on stdout
    [ask] = [r for r in _all() if r.get("decision") == "ask"]
    assert ask["tool_use_id"] == "toolu_1" and ask["rule"] == "threshold"
    assert ask["scope"] == {"team": None, "envs": []}
    [out] = _outcomes()
    assert out["outcome"] == "ran" and out["verdict"] == ask["_hash"]
    assert out["linked_by"] == "tool_use_id" and out["harness"] == "claude-code"
    assert gl.verify()["ok"], "an outcome is chained like any record"
    assert gl.ask_outcomes(_all()) == {ask["_hash"]: "approved"}


def test_without_tool_use_id_the_link_is_session_and_command_within_ten_minutes(clock):
    clock(0)
    assert _asked(_pre(tuid=None))
    clock(0, minutes=11)
    _post(tuid=None)
    assert _outcomes() == [], "an identical command eleven minutes later is another decision"
    clock(0, minutes=12)
    assert _asked(_pre(tuid=None))
    clock(0, minutes=13)
    _post(tuid=None, session="other")
    assert _outcomes() == [], "another session's run answers nothing here"
    _post(tuid=None)
    [out] = _outcomes()
    assert out["linked_by"] == "command"
    assert out["command_digest"] == go.digest(gl.redact(LAUNCH))


def test_tool_use_id_links_within_the_same_session_only():
    assert _asked(_pre())
    _post(session="someone-else")
    assert _outcomes() == []
    _post()
    assert len(_outcomes()) == 1


def test_outcomes_are_exported_with_their_place_in_the_chain():
    assert _asked(_pre())
    _post()
    recs = gl.export_records()
    assert [r.get("kind") for r in recs] == [None, "outcome"]
    assert recs[1]["verdict"] == recs[0]["chain"]["hash"] and recs[1]["chain"]["ok"]
    assert "|outcome:ran|" in gl.to_cef(recs[1], "0")


def test_an_ask_is_answered_once_though_two_post_hooks_run():
    """The plugin's post hook and a settings one both run for one call."""
    assert _asked(_pre())
    _post()
    _post()
    assert len(_outcomes()) == 1


def test_no_ask_no_record_and_garbage_is_a_silent_exit_0():
    _post(command="ls -la", tuid="toolu_9")
    _hook({"hook_event_name": "PostToolUse", "tool_name": "Edit",
           "tool_input": {"file_path": "a.txt"}}, post=True)
    assert _all() == []
    for raw in ("", "not json", "[]", '{"hook_event_name": "PostToolUse"}'):
        out = io.StringIO()
        assert go.run_post(None, io.StringIO(raw), out) == 0 and out.getvalue() == ""


def test_the_off_switch_turns_the_post_hook_off_too(monkeypatch):
    assert _asked(_pre())
    monkeypatch.setenv("FINOPS_GUARD", "off")
    _post()
    assert _outcomes() == []


def test_copilot_and_cursor_post_payloads_link_by_session_and_command():
    g.gate_command(LAUNCH, session_id="cop-1", harness="copilot", cwd=None, tool="bash")
    g.gate_command(LAUNCH, session_id=ai_budget.NO_SESSION, harness="cursor", cwd=None,
                   tool="shell")
    _hook({"sessionId": "cop-1", "timestamp": 1, "cwd": "/w", "toolName": "bash",
           "toolArgs": json.dumps({"command": LAUNCH}),
           "toolResult": {"resultType": "success", "textResultForLlm": "ok"}}, post=True)
    out = _hook({"hook_event_name": "afterShellExecution", "conversation_id": "c1",
                 "command": LAUNCH, "output": "ok", "duration": 12}, post=True)
    assert out == "{}", "Cursor hears its own empty answer"
    got = {o["harness"]: o for o in _outcomes()}
    assert set(got) == {"copilot", "cursor"}
    assert all(o["linked_by"] == "command" for o in got.values())


def test_the_post_hook_never_imports_the_guard(tmp_path):
    code = ("import io, json, sys\n"
            "from finops import guard_plugin\n"
            "p = {'session_id': 's', 'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',"
            " 'tool_input': {'command': 'terraform apply'}, 'tool_use_id': 't1'}\n"
            "guard_plugin.hook_main(['--via', 'plugin', '--post'])\n"
            "print(json.dumps(sorted(m for m in sys.modules if m.startswith('finops'))))\n")
    env = {**os.environ, "HOME": str(tmp_path), "PYTHONPATH": str(SRC)}
    env.pop("FINOPS_DATA_DIR", None)
    r = subprocess.run([sys.executable, "-c", code], input=json.dumps(
        {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Bash",
         "tool_input": {"command": "ls"}}), capture_output=True, text=True, env=env,
        check=False, timeout=60)
    assert r.returncode == 0, r.stderr
    mods = set(json.loads(r.stdout.strip().splitlines()[-1]))
    assert "finops.guard" not in mods and "finops.guard_adapters" not in mods
    assert "finops.guard_outcome" in mods


def _cli(args, home: Path, stdin: str = "", **env_extra) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(SRC),
           "PYTHONDONTWRITEBYTECODE": "1", "NABLE_NO_TELEMETRY": "1", **env_extra}
    for var in ("CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR", "FINOPS_DATA_DIR", "FINOPS_GUARD",
                "FINOPS_PROFILE"):
        if var not in env_extra:
            env.pop(var, None)
    return subprocess.run([sys.executable, "-m", "finops.entry", *args], input=stdin,
                          capture_output=True, text=True, env=env, timeout=60, check=False,
                          cwd=str(home))


def test_the_post_hook_is_fast_through_the_plugins_command_line(tmp_path):
    """The latency bound, through the plugin's command line (uvx's own launch
    aside). The target is under 50 ms on a developer machine; the bound here
    is the pre hook's own silent answer on the same machine plus a margin, so
    a slow CI box does not fail it and a regression that imports the guard
    (hundreds of ms) does."""
    home = tmp_path / "h"
    home.mkdir()
    pre = {"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": "Edit",
           "tool_input": {"file_path": str(home / "a.txt")}}
    post = {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": "ls"}, "tool_use_id": "t"}

    def median(args, payload) -> float:
        times = []
        for _ in range(7):
            t = time.perf_counter()
            r = _cli(args, home, json.dumps(payload))
            times.append(time.perf_counter() - t)
            assert r.returncode == 0 and r.stdout == "", r.stderr
        return statistics.median(times)

    base = median(["guard", "hook", "--via", "plugin"], pre)
    took = median(["guard", "hook", "--via", "plugin", "--post"], post)
    assert took < base * 1.5 + 0.05, f"post hook {took * 1000:.0f} ms, pre {base * 1000:.0f} ms"


def test_the_real_cli_takes_post_on_the_fast_path_and_through_argparse(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    payload = json.dumps({"session_id": "s", "hook_event_name": "PostToolUse",
                          "tool_name": "Bash", "tool_input": {"command": "ls"}})
    for args in (["guard", "hook", "--via", "plugin", "--post"],
                 ["guard", "hook", "--post"],
                 ["guard", "hook", "--harness", "claude", "--post", "--global"]):
        r = _cli(args, home, payload)
        assert r.returncode == 0 and r.stdout == "", (args, r.stderr)


@pytest.mark.skipif(sys.platform == "win32", reason="runs the hook command in sh")
def test_an_older_nable_that_rejects_post_never_disturbs_the_call(tmp_path):
    """A release before the post hook exits 2 on --post (argparse); the
    fail-safe wrapper makes that exit 0, which Claude Code ignores."""
    old = tmp_path / "finops"
    old.write_text("#!/bin/sh\necho 'unrecognized arguments: --post' >&2\nexit 2\n")
    old.chmod(0o755)
    cmd = ga.post_hook_command(f"{old} guard hook; exit 0")
    assert cmd == f"{old} guard hook --post; exit 0"
    r = subprocess.run(["sh", "-c", cmd], input="{}", capture_output=True, text=True,
                       check=False)
    assert r.returncode == 0 and r.stdout == ""


# ── A. derived declines and the report ───────────────────────────────────────

def _ask_at(clock, days_ago: float, *, answer: str = "approved", command: str = LAUNCH,
            usd: float = 1121.28, team: str | None = None, envs: tuple = (),
            door: str = "two_way", action: str = "infra_apply", harness: str = "claude-code",
            tool: str = "Bash", session: str | None = None, rule: str | None = "threshold",
            n: list = [0]) -> None:  # noqa: B006 - a counter shared by every call
    """One ask as the guard records it, `days_ago`, answered `answer`
    ("approved": the post hook ran; anything else: nothing followed)."""
    n[0] += 1
    clock(days_ago)
    tuid = f"toolu_{n[0]}"
    gl.append({"harness": harness, "session": session or f"s{n[0]}", "tool": tool,
               "command": gl.redact(command), "door": door, "action_type": action,
               "decision": "ask", "monthly_usd": usd,
               **({"rule": rule} if rule else {}),
               "scope": {"team": team, "envs": list(envs)}, "tool_use_id": tuid})
    if answer == "approved":
        clock(days_ago, minutes=1)
        _hook({"session_id": session or f"s{n[0]}", "hook_event_name": "PostToolUse",
               "tool_name": tool, "tool_input": {"command": command}, "tool_use_id": tuid},
              post=True)


def test_declined_is_derived_after_30_quiet_minutes_where_a_post_hook_works(clock):
    _ask_at(clock, 2)                                   # proves the post hook works here
    _ask_at(clock, 1, answer="none")
    _ask_at(clock, 0, answer="none", session="live")
    clock(0, minutes=10)
    got = sorted(gl.ask_outcomes(_all()).values())
    assert got == ["approved", "declined", "unknown"], "the live session's ask is pending"
    clock(0, minutes=45)
    assert sorted(gl.ask_outcomes(_all()).values()) == ["approved", "declined", "declined"]
    assert not any(r.get("outcome") == "declined" for r in _all()), "a decline is never written"


def test_without_a_working_post_hook_an_unanswered_ask_is_unknown(clock):
    _ask_at(clock, 3, answer="none")
    _ask_at(clock, 2, answer="none", harness="cursor", tool="mcp__aws__call_aws")
    clock(0)
    assert set(gl.ask_outcomes(_all()).values()) == {"unknown"}
    _ask_at(clock, 1, harness="cursor", tool="shell", command=LAUNCH)
    clock(0)
    answers = gl.ask_outcomes(_all())
    cursor_mcp = [r["_hash"] for r in _all() if r.get("tool") == "mcp__aws__call_aws"]
    assert answers[cursor_mcp[0]] == "unknown", "Cursor's post hook sees shell commands only"


def test_the_report_shows_ask_outcomes_per_action_class_and_scope(clock):
    _ask_at(clock, 3, team="payments", envs=("prod",))
    _ask_at(clock, 2, answer="none", team="payments", envs=("prod",))
    clock(0)
    s = gl.summarize(30)
    assert s["asks"]["approved"] == 1 and s["asks"]["declined"] == 1
    [row] = s["asks"]["by_class"]
    assert row["action_type"] == "infra_apply" and row["scope"] == "team:payments env:prod"
    assert s["by_decision"].get(None) is None and s["records"] == 2, "outcomes are not verdicts"
    assert all(not gl.is_outcome(r) for r in gl.read()), "verdict readers see verdicts only"
    assert all(not gl.is_outcome(r) for r in gl.recent(60 * 24 * 7))


def test_guard_report_prints_how_the_asks_were_answered(clock, capsys):
    from finops import setup_wizard as sw
    _ask_at(clock, 3)
    clock(0)

    class P:
        guard_days, guard_session, guard_json = 30, None, False
    sw._guard_report(P())
    out = capsys.readouterr().out
    assert "How the asks were answered" in out and "1 approved" in out


# ── A. install, upgrade, uninstall ────────────────────────────────────────────

def _settings_doc() -> dict:
    return json.loads((Path.cwd() / ".claude" / "settings.json").read_text())


def _ours(doc: dict, event: str) -> list[dict]:
    return [h for grp in (doc.get("hooks") or {}).get(event) or [] for h in grp["hooks"]
            if g._is_our_command(h.get("command"))]


def test_install_writes_the_post_hook_beside_the_pre_hook():
    g.install(False)
    doc = _settings_doc()
    [pre], [post] = _ours(doc, "PreToolUse"), _ours(doc, "PostToolUse")
    assert post["command"] == ga.post_hook_command(pre["command"])
    assert post["command"].endswith(" --post; exit 0") and g.is_fail_safe(post["command"])
    assert doc["hooks"]["PostToolUse"][0]["matcher"] == g._HOOK_MATCHER
    assert "PostToolUseFailure" not in doc["hooks"], "an older Claude Code would reject it"


def test_install_upgrades_an_existing_install_in_place(tmp_path):
    path = Path.cwd() / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    theirs = {"type": "command", "command": "echo post"}
    path.write_text(json.dumps({"hooks": {
        "PreToolUse": [{"matcher": g._HOOK_MATCHER, "hooks": [
            {"type": "command", "command": g._claude_hook_command(), "timeout": 30}]}],
        "PostToolUse": [{"matcher": "Bash", "hooks": [theirs]}]}}))
    assert ga.post_hook_missing(path)
    outcome, _ = ga.install("claude")
    assert outcome == "repaired", "an install from before the post hook gains it"
    doc = _settings_doc()
    assert doc["hooks"]["PostToolUse"][0] == {"matcher": "Bash", "hooks": [theirs]}
    assert len(_ours(doc, "PostToolUse")) == 1 and not ga.post_hook_missing(path)
    assert ga.install("claude")[0] == "already"
    # A pin that moves takes the post hook with it.
    stale = g._UVX_HOOK_CMD.replace(g.__version__, "0.0.1") + "; exit 0"
    doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = stale
    for h in _ours(doc, "PostToolUse"):
        h["command"] = ga.post_hook_command(stale)
    path.write_text(json.dumps(doc))
    g.install(False)
    doc = _settings_doc()
    [pre], [post] = _ours(doc, "PreToolUse"), _ours(doc, "PostToolUse")
    assert "0.0.1" not in pre["command"] and post["command"] == ga.post_hook_command(pre["command"])


def test_uninstall_removes_the_post_hook_and_nothing_else():
    path = Path.cwd() / ".claude" / "settings.json"
    g.install(False)
    doc = _settings_doc()
    doc["hooks"]["PostToolUse"].append({"matcher": "Bash", "hooks": [
        {"type": "command", "command": "echo mine"}]})
    path.write_text(json.dumps(doc))
    assert g.uninstall(False)
    doc = _settings_doc()
    assert doc["hooks"] == {"PostToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "echo mine"}]}]}
    g.install(False)
    g.uninstall(False)
    doc = _settings_doc()
    assert "PostToolUse" in doc["hooks"] and _ours(doc, "PostToolUse") == []


def test_uninstall_removes_a_post_hook_left_without_its_pre_hook():
    path = Path.cwd() / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": {"PostToolUse": [{"matcher": "x", "hooks": [
        {"type": "command", "command": "finops guard hook --post; exit 0"}]}]}}))
    assert g.uninstall(False)
    assert _settings_doc() == {}


def test_cursor_gets_after_shell_execution_and_loses_it_on_uninstall():
    ga.install("cursor")
    doc = json.loads(ga.hooks_path("cursor", False).read_text())
    [pre] = doc["hooks"]["beforeShellExecution"]
    [post] = doc["hooks"]["afterShellExecution"]
    assert post["command"] == ga.post_hook_command(pre["command"]) and post["failClosed"] is False
    assert "afterMCPExecution" not in doc["hooks"]
    # An install from before the post hook gains it in place.
    del doc["hooks"]["afterShellExecution"]
    ga.hooks_path("cursor", False).write_text(json.dumps(doc))
    assert ga.install("cursor")[0] == "repaired"
    assert ga.install("cursor")[0] == "already"
    assert ga.uninstall("cursor")[0]
    doc = json.loads(ga.hooks_path("cursor", False).read_text())
    assert not any(k in doc.get("hooks", {}) for k in
                   ("beforeShellExecution", "beforeMCPExecution", "afterShellExecution"))


def test_copilot_gets_post_tool_use_and_loses_it_on_uninstall():
    ga.install("copilot")
    path = ga.hooks_path("copilot", False)
    doc = json.loads(path.read_text())
    [pre], [post] = doc["hooks"]["preToolUse"], doc["hooks"]["postToolUse"]
    assert post["command"] == ga.post_hook_command(pre["command"])
    assert post["matcher"] == pre["matcher"] and "postToolUseFailure" not in doc["hooks"]
    assert ga.install("copilot")[0] == "already"
    assert ga.uninstall("copilot")[0]
    assert not path.exists(), "the file was ours alone"


@pytest.mark.parametrize("harness", ["codex", "gemini"])
def test_harnesses_that_cannot_ask_get_no_post_hook(harness):
    ga.install(harness)
    text = ga.hooks_path(harness, False).read_text()
    assert "--post" not in text


def test_the_plugin_hooks_json_carries_the_post_hook():
    hooks = json.loads((ROOT / "plugins/nable/hooks/hooks.json").read_text())
    version = json.loads((ROOT / "plugins/nable/.claude-plugin/plugin.json").read_text())[
        "version"]
    assert hooks == ga.plugin_hooks(version)
    [pre_grp], [post_grp] = hooks["hooks"]["PreToolUse"], hooks["hooks"]["PostToolUse"]
    assert post_grp["matcher"] == pre_grp["matcher"] == g._HOOK_MATCHER
    [pre], [post] = pre_grp["hooks"], post_grp["hooks"]
    assert post["command"] == ga.post_hook_command(pre["command"])
    assert ga.uvx_release(post["command"]) == version and post["timeout"] == pre["timeout"]
    assert " --via plugin --post; exit 0" in post["command"]
    assert set(hooks["hooks"]) == {"PreToolUse", "PostToolUse"}


# ── B. inference ──────────────────────────────────────────────────────────────

def _infer(**kw) -> dict:
    return pi.infer_guard_facts(guard_signal(), model=org.load(), **kw)


def test_clean_rounding():
    assert [pi.clean_up(x) for x in (840, 847, 1121.28, 12345, 47, 95, 100)] == \
        [840, 850, 1200, 13000, 50, 100, 100]
    assert [pi.clean_down(x) for x in (250, 1234, 95, 9)] == [250, 1200, 90, 0]


def test_five_approvals_over_seven_days_propose_one_threshold(clock):
    for day in (9, 7, 5, 3):
        _ask_at(clock, day)
    clock(0)
    got = _infer()
    assert got["proposals"] == [] and "4 approved of the 5 needed" in got["not_yet"][0]["why"]
    _ask_at(clock, 2, usd=840.0, command=SMALL)
    clock(0)
    [p] = _infer()["proposals"]
    assert p["subject"] == "org:org" and p["direction"] == "loosen"
    assert p["value"] == {"max_auto_monthly_usd": 1200.0} and p["current_usd"] == 500.0
    assert p["confidence"] == 0.75 and p["dollars_monthly"] == 1121.28
    assert "approved 5 times since" in p["note"] and "max $1,121/mo" in p["note"]
    f = p["fact"]
    assert f.source == "inference:guard-ledger" and f.extra["note"] == p["note"]
    assert f.extra["evidence"]["approved"] == 5


def test_five_approvals_inside_a_week_are_not_enough(clock):
    for day in (6, 5, 4, 2, 1):
        _ask_at(clock, day)
    clock(0)
    got = _infer()
    assert got["proposals"] == [] and "5 day(s) of the 7 needed" in got["not_yet"][0]["why"]


def test_retries_within_ten_minutes_are_one_decision(clock):
    for day in (9, 7, 5, 3):
        _ask_at(clock, day, session="same")
        _ask_at(clock, day - 0.001, session="same")    # the agent asked again
    clock(0)
    assert _infer()["proposals"] == []


def test_never_for_a_one_way_door(clock):
    for day in (12, 10, 8, 6, 4, 2):
        _ask_at(clock, day, command="terraform destroy -auto-approve", door="one_way",
                action="delete_resource")
    clock(0)
    got = _infer()
    assert got["proposals"] == []
    assert "one-way door" in got["not_yet"][0]["why"]


def test_not_after_a_revert(clock):
    for day in (10, 8, 6, 4, 2):
        _ask_at(clock, day)
    clock(4, minutes=60)
    gl.append({"harness": "claude-code", "session": "s", "tool": "Bash",
               "command": "aws ec2 terminate-instances --instance-ids i-1", "door": "one_way",
               "action_type": "terminate_instance", "decision": "allow",
               "scope": {"team": None, "envs": []}})
    clock(0)
    got = _infer()
    assert got["proposals"] == []
    assert any("reverted 1 time" in n["why"] for n in got["not_yet"])


def test_not_with_a_decline_in_the_key(clock):
    for day in (10, 8, 6, 4, 2):
        _ask_at(clock, day)
    _ask_at(clock, 1, answer="none")
    clock(0)
    got = _infer()
    assert got["proposals"] == [] and "declined 1 time" in got["not_yet"][0]["why"]


def test_three_declines_propose_asking_sooner(clock):
    _ask_at(clock, 9, team="payments", envs=("prod",))
    for day in (6, 4, 2):
        _ask_at(clock, day, answer="none", team="payments", envs=("prod",))
    clock(0)
    [p] = _infer()["proposals"]
    assert p["subject"] == "team:payments" and p["direction"] == "tighten"
    assert p["value"] == {"max_auto_monthly_usd": 250.0}
    assert "declined 3 times since" in p["note"]


def test_a_team_proposal_is_scoped_to_the_team_and_blocked_by_its_declines(clock):
    for day in (10, 8, 6, 4, 2):
        _ask_at(clock, day, team="payments")
    _ask_at(clock, 1, answer="none", team="search")
    clock(0)
    [p] = _infer()["proposals"]
    assert p["subject"] == "team:payments", "search's decline is not payments' business"
    _ask_at(clock, 1, answer="none", team="payments", action="k8s_apply", usd=600.0)
    clock(0)
    assert _infer()["proposals"] == [], "a decline the team threshold would cover blocks it"


def test_without_the_guards_team_the_confirmed_owner_scopes_the_key():
    from finops.recommendations.learning.signal import guard_key
    rec = {"action_type": "infra_apply", "door": "two_way",
           "scope": {"team": None, "envs": ["prod"]},
           "owner": {"team": "search", "confirmed": True}}
    assert guard_key(rec) == ("infra_apply", "two_way", "search", "prod")
    assert guard_key({**rec, "owner": {"team": "search", "confirmed": False}})[2] is None
    assert guard_key({**rec, "scope": {"team": "payments", "envs": []}})[2:] == ("payments", None)


def test_a_rejected_figure_is_not_proposed_again_at_or_above_it(clock):
    for day in (10, 8, 6, 4, 2):
        _ask_at(clock, day)
    clock(0)
    got = pi.propose_guard_facts()
    [p] = got["proposals"]
    assert p["result"] == "added"
    org.reject(p["key"], human("maria"))
    again = pi.propose_guard_facts()
    assert again["proposals"] == []
    assert any("rejected $1,200/mo" in n["why"] for n in again["not_yet"])


def test_a_waiting_proposal_is_not_proposed_twice(clock):
    for day in (10, 8, 6, 4, 2):
        _ask_at(clock, day)
    clock(0)
    assert pi.propose_guard_facts()["proposals"][0]["result"] == "added"
    assert pi.propose_guard_facts()["proposals"] == []
    assert len(org.load().proposals("threshold")) == 1


def test_dry_run_writes_nothing(clock):
    for day in (10, 8, 6, 4, 2):
        _ask_at(clock, day)
    clock(0)
    got = pi.propose_guard_facts(dry_run=True)
    assert len(got["proposals"]) == 1 and "result" not in got["proposals"][0]
    assert org.load().proposals() == []


# ── B. the interview ──────────────────────────────────────────────────────────

def _propose(subject: str, figure: float, way: str) -> str:
    f = org.Fact.from_dict({"fact": "threshold", "subject": subject,
                            "value": {"max_auto_monthly_usd": figure},
                            "source": pi.GUARD_SOURCE, "confidence": 0.8,
                            "status": "proposed", "note": f"{way} note, approved 7 times",
                            "evidence": {"direction": way}})
    org.propose(f)
    return f.key


def test_inferred_proposals_are_asked_alone_with_evidence_and_the_right_default():
    loosen = _propose("team:payments", 900, "loosen")
    tighten = _propose("team:search", 250, "tighten")
    # A bulk-able neighbour, to show the inferred ones never join a group.
    org.propose(org.make_fact("owner", "aws_account:111111111111", {"team": "payments"},
                              source="x"))
    org.propose(org.make_fact("owner", "aws_account:222222222222", {"team": "payments"},
                              source="x"))
    qs = {q.key: q for q in org.questions(20, include_spend=False)}
    assert qs[loosen].default == "n" and qs[tighten].default == "y"
    assert "loosen note, approved 7 times" in qs[loosen].text
    assert qs[loosen].text.startswith("loosen note")
    assert "inference:guard-ledger" in qs[loosen].text
    assert qs[tighten].text.startswith("tighten note")
    bulk = [q for q in org.questions(20, include_spend=False) if q.kind == "bulk"]
    assert bulk and all(loosen not in q.keys and tighten not in q.keys for q in bulk)


def test_pressing_enter_rejects_a_loosening_and_confirms_a_tightening(monkeypatch):
    from finops.org import cli as org_cli
    loosen = _propose("team:payments", 900, "loosen")
    tighten = _propose("team:search", 250, "tighten")
    qs = [q for q in org.questions(20, include_spend=False) if q.key in (loosen, tighten)]
    monkeypatch.setattr("builtins.input", lambda _p="": "")
    org_cli._interview(qs, org, None, human("maria"))
    m = org.load()
    assert m.find(loosen)[0].status == "rejected"
    assert m.find(tighten)[0].status == "confirmed"


# ── B. nable learn ────────────────────────────────────────────────────────────

@pytest.fixture
def lessons_db(monkeypatch, tmp_path):
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "t.db"))
    import finops.storage.db as db_mod
    monkeypatch.setattr(db_mod, "_ENGINE", None)
    from finops.recommendations.learning.ledger import learning_lessons
    from finops.storage.db import get_engine
    with get_engine().begin() as conn:
        conn.execute(learning_lessons.insert().values(
            key="source:commitment", kind="verdict", to_verdict="suppress",
            lesson="Commitment recs rank lower for you.", evidence='{"acted": 0}',
            first_seen=datetime.now(UTC), last_confirmed=datetime.now(UTC), status="active"))
    yield db_mod
    db_mod._ENGINE = None


def _learn(argv: list[str]) -> int:
    from finops import cli_learn
    return cli_learn.main(argv)


def test_learn_rollback_and_restore_are_a_persons_decision(lessons_db, capsys, monkeypatch):
    from finops.org import OrgError
    from finops.org import cli as org_cli
    from finops.recommendations.learning import ledger
    monkeypatch.setattr(org_cli, "_is_tty", lambda: False)
    monkeypatch.setattr("finops.recommendations.learning.ledger.sync_lessons", lambda *a: {})
    lid = ledger.lessons()[0]["id"]
    assert _learn(["rollback", str(lid)]) == 2
    assert "human decision" in capsys.readouterr().err
    assert ledger.lessons()[0]["status"] == "active"
    with pytest.raises(OrgError):
        ledger.rollback(lid, by="an agent")
    with pytest.raises(OrgError):
        ledger.restore(lid)
    assert _learn(["rollback", str(lid), "--as", "maria", "--note", "ramping up"]) == 0
    row = ledger.lesson(lid)
    assert row["status"] == "rolled_back" and row["rollback_note"] == "by maria: ramping up"
    assert _learn(["restore", str(lid)]) == 2
    assert _learn(["restore", str(lid), "--as", "maria"]) == 0
    assert ledger.lesson(lid)["status"] == "superseded"
    capsys.readouterr()
    assert _learn(["show", str(lid), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == lid
    assert _learn(["list", "--all"]) == 0 and "source:commitment" in capsys.readouterr().out


def test_learn_infer_dry_run_explains_and_writes_nothing(clock, capsys):
    for day in (10, 8, 6, 4, 2):
        _ask_at(clock, day)
    clock(0)
    assert _learn(["infer", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Would propose: a higher threshold for org:org, $1,200/mo (now $500/mo)" in out
    assert "approved 5 times since" in out and "Nothing was written" in out
    assert org.load().proposals() == []
    assert _learn(["infer"]) == 0
    assert "(added)" in capsys.readouterr().out
    assert len(org.load().proposals("threshold")) == 1


def _gate(command: str) -> dict | None:
    return g.gate_command(command, harness="claude-code", tool="Bash", record=False)


ENTRY_POINTS = ["nable", "finops", "uvx finops-mcp@1.2.3", "python -m finops.entry",
                "uvx --from finops-mcp==0.9.0 finops", "~/.local/bin/nable"]


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("verb", ["rollback 3", "restore 3", "rollback 3 --as maria"])
def test_the_guard_asks_when_an_agent_runs_learn_rollback_or_restore(entry, verb):
    v = _gate(f"{entry} learn {verb}")
    assert v is not None and v["decision"] == "ask" and v["action_type"] == "learning_change"


@pytest.mark.parametrize("command", [
    "python -m finops.cli_learn rollback 3 --as me",
    "python3 -c 'from finops.recommendations.learning import ledger; ledger.rollback(3)'",
    "python3 -c 'from finops.recommendations.learning.ledger import restore; restore(3)'",
])
def test_the_guard_asks_for_the_module_and_the_python_api(command):
    v = _gate(command)
    assert v is not None and v["action_type"] == "learning_change"


@pytest.mark.parametrize("command", ["nable learn list", "nable learn show 3",
                                     "nable learn infer --dry-run", "nable learn infer"])
def test_reading_what_was_learned_stays_silent(command):
    assert _gate(command) is None


# ── end to end: it stops asking the same thing twice ─────────────────────────

def test_repeated_approvals_become_one_threshold_that_stops_the_ask_once_confirmed(clock):
    """The person approves the same launch on six days over a week, through
    the real hooks. nable proposes one threshold; the guard still asks until
    a person confirms it; then the same command runs without a prompt."""
    for i, day in enumerate((8, 7, 5, 4, 2, 1)):
        clock(day)
        assert _asked(_pre(session=f"s{i}", tuid=f"toolu_{i}")), "asked, as before"
        clock(day, minutes=2)
        _post(session=f"s{i}", tuid=f"toolu_{i}")
    clock(0)
    assert gl.summarize(30)["asks"]["approved"] == 6

    got = pi.propose_guard_facts()
    [p] = got["proposals"]
    assert (p["subject"], p["value"]["max_auto_monthly_usd"], p["result"]) == \
        ("org:org", 1200.0, "added")
    # A proposal is a guess, and a guess never loosens the guard.
    assert _asked(_pre(session="s9", tuid="toolu_9"))

    [q] = [q for q in org.questions(10, include_spend=False) if q.key == p["key"]]
    assert q.default == "n" and "approved 6 times since" in q.text
    org.confirm(p["key"], human("maria"))

    out = _pre(session="s10", tuid="toolu_10")
    assert not _asked(out), "the same command no longer asks"
    assert json.loads(out).get("systemMessage"), "it says the figure, and does not stop"
    last = [r for r in _all() if not gl.is_outcome(r)][-1]
    assert last["decision"] == "warn" and last["org_thresholds"]["max_auto_monthly_usd"] == 1200
    # And nothing more is proposed: the threshold in force covers what was approved.
    assert pi.propose_guard_facts()["proposals"] == []
