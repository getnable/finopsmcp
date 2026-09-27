"""One-time approvals for harnesses that cannot ask (guard_approvals).

What has to stay true:
  - in Codex CLI, Gemini CLI, Cline and the Copilot cloud agent an ask is a
    deny, and that deny carries an approval id and the exact command a
    person runs in their own terminal: `nable guard approve <id>`
  - a person's approval (a terminal, or --as) lets the identical call (same
    command, working directory and harness) through once within 15 minutes,
    recorded as approved_out_of_band with who approved; a second run is
    denied again, and a different command, directory or harness is not
    covered; an approval past its window lifts nothing
  - an agent running `nable guard approve` is refused on every entry point
    (the shell, an MCP call's command line, code calling approve(), a write
    to the approvals file), and the CLI refuses without a terminal or --as
  - a deny that comes from policy (on_budget_breach: deny, a freeze in deny
    mode, a pack's deny rule) is never approvable, and says so
  - harnesses that can ask (Claude Code, Cursor, Copilot CLI) are unchanged
"""
from __future__ import annotations

import io
import json
import os
import stat
from datetime import UTC, datetime, timedelta

import pytest

import finops.guard as g
import finops.guard_ledger as gl
from finops import ai_budget, guard_adapters, guard_approvals, guard_plugin, org, setup_wizard
from finops.org.cli import _who as human

DESTROY = "terraform destroy"
APPROVE_RE = "nable guard approve "


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("FINOPS_GUARD_STRICT", "FINOPS_POLICY_MAX_AUTO_USD",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET", "FINOPS_POLICY_VELOCITY_CAP_USD",
                "FINOPS_POLICY_LOOP_COUNT", "FINOPS_GUARD_TEAM", "FINOPS_GUARD_ACCOUNT",
                "FINOPS_POLICY_ON_BUDGET_BREACH", "FINOPS_POLICY_FILE",
                *guard_adapters._COPILOT_CLOUD_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})


@pytest.fixture
def work(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


def _hook(payload: dict) -> dict | None:
    """One hook call, as the harness makes it: the payload on stdin."""
    out, err = io.StringIO(), io.StringIO()
    assert guard_adapters.run_hook(stdin=io.StringIO(json.dumps(payload)), stdout=out,
                                   stderr=err) == 0
    return json.loads(out.getvalue()) if out.getvalue() else None


def codex(command: str, cwd, session: str = "s1") -> dict | None:
    return _hook({"hook_event_name": "PreToolUse", "turn_id": "t1", "session_id": session,
                  "tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)})


def codex_reason(r: dict | None) -> str:
    assert r is not None
    out = r["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    return out["permissionDecisionReason"]


def approval_id(reason: str) -> str:
    assert APPROVE_RE in reason, reason
    return reason.split(APPROVE_RE, 1)[1].split("`", 1)[0]


def cli_approve(*args: str) -> int:
    try:
        setup_wizard.main(["guard", "approve", *args])
    except SystemExit as e:
        return int(e.code or 0)
    return 0


def _records():
    p = gl.ledger_path()
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


# ── end to end through the hook ───────────────────────────────────────────────

def test_codex_deny_carries_an_id_a_person_approves_and_it_runs_once(work, capsys):
    reason = codex_reason(codex(DESTROY, work))
    aid = approval_id(reason)
    assert f"`nable guard approve {aid}` in their own terminal (not the agent)" in reason
    assert "allowed once within 15 minutes" in reason
    assert _records()[-1]["approval_id"] == aid
    # Asked again before anyone approved: the same id, not a new one.
    assert approval_id(codex_reason(codex(DESTROY, work))) == aid

    assert cli_approve(aid, "--as", "maria") == 0
    assert "Approved once, by maria" in capsys.readouterr().out

    assert codex(DESTROY, work) is None                     # it runs
    rec = _records()[-1]
    assert rec["decision"] == "allow" and rec["harness"] == "codex"
    assert rec["approved_out_of_band"]["id"] == aid
    assert rec["approved_out_of_band"]["by"] == "maria"
    assert rec["command"] == DESTROY

    again = codex_reason(codex(DESTROY, work))              # once only
    assert approval_id(again) != aid


def test_an_approval_covers_only_the_identical_call(work, tmp_path):
    aid = approval_id(codex_reason(codex(DESTROY, work)))
    assert cli_approve(aid, "--as", "maria") == 0
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    # Another command, another directory, another harness: each is its own ask.
    assert approval_id(codex_reason(codex(f"{DESTROY} -target=aws_instance.x", work))) != aid
    assert approval_id(codex_reason(codex(DESTROY, other_dir))) != aid
    gem = _hook({"hook_event_name": "BeforeTool", "session_id": "s1",
                 "tool_name": "run_shell_command", "tool_input": {"command": DESTROY},
                 "cwd": str(work)})
    assert gem["decision"] == "deny" and APPROVE_RE in gem["reason"]
    # The approved call itself is still waiting for its one run.
    assert codex(DESTROY, work) is None


def test_an_approval_expires(work, monkeypatch):
    aid = approval_id(codex_reason(codex(DESTROY, work)))
    assert cli_approve(aid, "--as", "maria") == 0
    later = datetime.now(UTC) + timedelta(minutes=16)
    monkeypatch.setattr(guard_approvals, "_now", lambda: later)
    assert approval_id(codex_reason(codex(DESTROY, work))) != aid
    # And the expired entry is pruned from the store.
    rows = json.loads(guard_approvals.store_path().read_text())["approvals"]
    assert aid not in rows


def test_approving_an_expired_or_unknown_id_is_refused(work, monkeypatch, capsys):
    aid = approval_id(codex_reason(codex(DESTROY, work)))
    assert cli_approve("0000ffff", "--as", "maria") == 1
    assert "no approval '0000ffff' is waiting" in capsys.readouterr().err
    later = datetime.now(UTC) + timedelta(minutes=16)
    monkeypatch.setattr(guard_approvals, "_now", lambda: later)
    assert cli_approve(aid, "--as", "maria") == 1
    assert "has expired" in capsys.readouterr().err


def test_an_approval_is_given_once(work, capsys):
    aid = approval_id(codex_reason(codex(DESTROY, work)))
    assert cli_approve(aid, "--as", "maria") == 0
    assert cli_approve(aid, "--as", "bob") == 1
    assert "already given by maria" in capsys.readouterr().err
    assert codex(DESTROY, work) is None
    assert cli_approve(aid, "--as", "bob") == 1
    assert "already used" in capsys.readouterr().err


def test_approve_lists_what_is_waiting(work, capsys):
    aid = approval_id(codex_reason(codex(DESTROY, work)))
    capsys.readouterr()
    assert cli_approve() == 0
    out = capsys.readouterr().out
    assert aid in out and "codex" in out and "waiting" in out and DESTROY in out


def test_the_cli_is_a_human_decision(work, monkeypatch, capsys):
    from finops.org import cli as org_cli
    aid = approval_id(codex_reason(codex(DESTROY, work)))
    monkeypatch.setattr(org_cli, "_is_tty", lambda: False)
    assert cli_approve(aid) == 2
    assert "human decision" in capsys.readouterr().err
    assert approval_id(codex_reason(codex(DESTROY, work))) == aid       # still not approved
    # The Python API wants the decision the CLI makes, not a name.
    with pytest.raises(guard_approvals.ApprovalError, match="person's decision"):
        guard_approvals.approve(aid, "maria")
    # A terminal names the person (who says yes to what it shows).
    monkeypatch.setattr(org_cli, "_is_tty", lambda: True)
    monkeypatch.setattr(org_cli, "_git_email", lambda: "maria@example.com")
    monkeypatch.setattr("builtins.input", lambda _p="": "y")
    assert cli_approve(aid) == 0
    assert codex(DESTROY, work) is None
    assert _records()[-1]["approved_out_of_band"]["by"] == "maria@example.com"


def test_on_a_terminal_the_person_sees_what_they_approve_before_it_is_approved(
        work, monkeypatch, capsys):
    """The id reaches the person through the agent, which can say it is for
    anything. On a terminal the CLI shows the call, where, and why the guard
    stopped it, and approves only on a yes; before, it approved at once and
    showed the call afterwards."""
    from finops.org import cli as org_cli
    aid = approval_id(codex_reason(codex(DESTROY, work)))
    capsys.readouterr()
    monkeypatch.setattr(org_cli, "_is_tty", lambda: True)
    monkeypatch.setattr(org_cli, "_git_email", lambda: "maria@example.com")
    said: list[str] = []

    def answer(text):
        def ask(prompt=""):
            said.append(capsys.readouterr().out + prompt)
            return text
        return ask
    for reply in ("", "n", "no thanks"):
        monkeypatch.setattr("builtins.input", answer(reply))
        assert cli_approve(aid, "--as", "maria") == 1
        assert "Not approved" in capsys.readouterr().out
    shown = said[0]
    assert DESTROY in shown and str(work) in shown and "codex" in shown
    assert "deletes" in shown or "destroy" in shown.split(DESTROY, 1)[1], shown
    assert approval_id(codex_reason(codex(DESTROY, work))) == aid        # still waiting

    monkeypatch.setattr("builtins.input", answer("y"))
    assert cli_approve(aid) == 0
    assert codex(DESTROY, work) is None


def test_the_store_is_private_and_protected(work):
    codex(DESTROY, work)
    p = guard_approvals.store_path()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert p.parent == gl.ledger_path().parent
    for cmd in (f"echo '{{}}' > {p}", f"rm {p}", f"cp /tmp/x {p}"):
        v = g.gate_command(cmd, record=False)
        assert v is not None and v["action_type"] == "protected_write", cmd
    assert guard_plugin.editor_target("Write", {"file_path": str(p)}, str(work))


def test_end_to_end_in_real_processes(tmp_path):
    """The hook and the CLI as the harness and the person run them: separate
    processes, the payload on the hook's stdin."""
    import subprocess
    import sys
    from pathlib import Path
    src = Path(g.__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "COPILOT_"))}
    env.update(PYTHONPATH=str(src), HOME=str(tmp_path), FINOPS_DATA_DIR=str(tmp_path / "d"),
               FINOPS_ORG_DIR=str(tmp_path / "org"))
    work = tmp_path / "w"
    work.mkdir()
    payload = json.dumps({"hook_event_name": "PreToolUse", "turn_id": "t1",
                          "session_id": "s1", "tool_name": "Bash",
                          "tool_input": {"command": DESTROY}, "cwd": str(work)})

    def run(*argv, stdin=""):
        return subprocess.run([sys.executable, "-m", "finops.entry", *argv], input=stdin,
                              capture_output=True, text=True, env=env, cwd=str(work),
                              timeout=60, check=False)
    first = run("guard", "hook", stdin=payload)
    aid = approval_id(codex_reason(json.loads(first.stdout)))
    refused = run("guard", "approve", aid)          # no terminal, no --as
    assert refused.returncode == 2 and "human decision" in refused.stderr
    assert run("guard", "approve", aid, "--as", "maria").returncode == 0
    assert run("guard", "hook", stdin=payload).stdout == ""        # allowed, once
    again = json.loads(run("guard", "hook", stdin=payload).stdout)
    assert approval_id(codex_reason(again)) != aid
    ledger = [json.loads(line) for line in
              (tmp_path / "d" / "guard-ledger.jsonl").read_text().splitlines()]
    used = [r for r in ledger if r.get("approved_out_of_band")]
    assert len(used) == 1 and used[0]["approved_out_of_band"]["by"] == "maria"


# ── an agent approving for a person ───────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "nable guard approve 3f9c2a1b --as maria",
    "finops guard approve 3f9c2a1b",
    "uvx --from finops-mcp nable guard approve 3f9c2a1b --as maria",
    "python -m finops.setup_wizard guard approve 3f9c2a1b --as maria",
    "cd /tmp && nable guard approve",
    "python3 -c \"from finops import guard_approvals as a; a.approve('3f9c2a1b', x)\"",
    "python3 -c \"from finops.guard_approvals import approve; approve('3f9c2a1b', x)\"",
])
def test_an_agent_approving_is_asked_about(cmd):
    v = g.gate_command(cmd, record=False)
    assert v is not None and v["decision"] == "ask", cmd
    assert v["action_type"] == "guard_change"
    assert "approving, for a person, a call the guard stopped" in v["reason"]


def test_an_agent_approving_is_refused_on_every_entry_point(work):
    aid = approval_id(codex_reason(codex(DESTROY, work)))
    cmd = f"nable guard approve {aid} --as maria"
    # Codex: a deny, and no approval id to approve the approval with.
    reason = codex_reason(codex(cmd, work))
    assert "approving, for a person" in reason and "approve it once" not in reason
    assert [r["id"] for r in guard_approvals.waiting()] == [aid]
    # Claude Code asks.
    out = io.StringIO()
    g.run_hook(io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd},
                                       "cwd": str(work)})), out)
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "ask"
    # A command line inside an MCP call.
    v = g.gate_mcp_call("mcp__shell__run", {"command": cmd}, harness="codex", record=False)
    assert v["decision"] == "ask" and v["action_type"] == "guard_change"
    # Nothing was approved by any of it.
    assert not any(r.get("approved_by") for r in guard_approvals.waiting())


def test_reading_approvals_stays_silent():
    for cmd in ("nable guard status", "nable guard report", "git commit -m 'guard approve'",
                "grep -rn 'nable guard approve' docs/"):
        assert g.gate_command(cmd, record=False) is None, cmd


# ── the other harnesses ───────────────────────────────────────────────────────

def test_gemini_and_cline_denies_carry_an_id(work):
    gem = _hook({"hook_event_name": "BeforeTool", "session_id": "s1",
                 "tool_name": "run_shell_command", "tool_input": {"command": DESTROY},
                 "cwd": str(work)})
    assert gem["decision"] == "deny" and APPROVE_RE in gem["reason"]
    cline = _hook({"clineVersion": "3", "hookName": "PreToolUse", "taskId": "t",
                   "workspaceRoots": [str(work)],
                   "preToolUse": {"toolName": "execute_command",
                                  "parameters": {"command": DESTROY}}})
    assert cline["cancel"] is True and APPROVE_RE in cline["errorMessage"]
    assert cli_approve(approval_id(cline["errorMessage"]), "--as", "maria") == 0
    assert _hook({"clineVersion": "3", "hookName": "PreToolUse", "taskId": "t",
                  "workspaceRoots": [str(work)],
                  "preToolUse": {"toolName": "execute_command",
                                 "parameters": {"command": DESTROY}}}) == {"cancel": False}


def test_the_deny_says_why_then_offers_the_approval_without_saying_run_it_yourself(work):
    """The harness's reason for blocking comes before the offer, and its
    advice to run the command by hand is dropped: the approval is the way."""
    reason = codex_reason(codex(DESTROY, work))
    assert "run the command yourself" not in reason
    assert reason.index("Codex hooks cannot pause") < reason.index(APPROVE_RE)
    assert reason.rstrip().endswith("within 15 minutes.")
    gem = _hook({"hook_event_name": "BeforeTool", "session_id": "s2",
                 "tool_name": "run_shell_command", "tool_input": {"command": DESTROY},
                 "cwd": str(work)})
    assert "run the command yourself" not in gem["reason"]
    assert gem["reason"].index("so nable blocked it.") < gem["reason"].index(APPROVE_RE)


def test_copilot_only_under_the_cloud_agent(work, monkeypatch):
    payload = {"toolName": "bash", "toolArgs": json.dumps({"command": DESTROY}),
               "cwd": str(work), "sessionId": "s"}
    r = _hook(payload)
    assert r["permissionDecision"] == "ask" and APPROVE_RE not in r["permissionDecisionReason"]
    monkeypatch.setenv("COPILOT_AGENT_PROMPT", "fix it")
    r = _hook(payload)
    assert r["permissionDecision"] == "deny" and APPROVE_RE in r["permissionDecisionReason"]


def test_harnesses_that_can_ask_get_no_id(work):
    v = g.gate_command(DESTROY, harness="claude-code", cwd=str(work))
    assert v["decision"] == "ask" and APPROVE_RE not in v["reason"]
    v = g.gate_command(DESTROY, harness="cursor", cwd=str(work))
    assert v["decision"] == "ask" and APPROVE_RE not in v["reason"]
    assert not guard_approvals.store_path().exists()


def test_a_codex_mcp_call_gets_an_id_too(work):
    args = {"cli_command": "aws ec2 terminate-instances --instance-ids i-1"}
    r = _hook({"hook_event_name": "PreToolUse", "turn_id": "t1", "session_id": "s1",
               "tool_name": "mcp__aws__call_aws", "tool_input": args, "cwd": str(work)})
    aid = approval_id(codex_reason(r))
    assert "identical call" in codex_reason(r)
    assert cli_approve(aid, "--as", "maria") == 0
    assert _hook({"hook_event_name": "PreToolUse", "turn_id": "t1", "session_id": "s1",
                  "tool_name": "mcp__aws__call_aws", "tool_input": args,
                  "cwd": str(work)}) is None
    assert _records()[-1]["approved_out_of_band"]["id"] == aid


def test_an_mcp_approval_is_bound_to_its_directory_too(work, tmp_path):
    """An MCP shell server runs its command where the agent works: approving
    `terraform destroy` through it in one project must not let the same call
    through in another. The MCP door recorded no directory, so it did."""
    args = {"command": DESTROY}
    other = tmp_path / "other"
    other.mkdir()

    def call(cwd):
        return _hook({"hook_event_name": "PreToolUse", "turn_id": "t1", "session_id": "s1",
                      "tool_name": "mcp__shell__run", "tool_input": args, "cwd": str(cwd)})
    aid = approval_id(codex_reason(call(work)))
    assert cli_approve(aid, "--as", "maria") == 0
    assert approval_id(codex_reason(call(other))) != aid
    assert call(work) is None                              # where it was approved, once


# ── never for a deny that is policy ───────────────────────────────────────────

POLICY_NOTE = "cannot let it through"


def _no_id(reason: str) -> None:
    assert APPROVE_RE + " " not in reason and "approve it once" not in reason
    assert POLICY_NOTE in reason
    assert guard_approvals.waiting() == []


def test_a_freeze_in_deny_mode_is_not_approvable(work):
    now = datetime.now(UTC)
    org.set_fact(org.make_fact("freeze", "org:org",
                               {"start": (now - timedelta(hours=1)).isoformat(),
                                "end": (now + timedelta(hours=1)).isoformat(),
                                "reason": "Black Friday", "mode": "deny"}, source="human"),
                 human("maria"))
    _no_id(codex_reason(codex(DESTROY, work)))
    # A freeze in ask mode is a question for a person, so it is approvable.
    org.set_fact(org.make_fact("freeze", "org:org",
                               {"start": (now - timedelta(hours=2)).isoformat(),
                                "end": (now + timedelta(hours=1)).isoformat(),
                                "reason": "quiet week", "mode": "ask"}, source="human"),
                 human("maria"))
    reason = codex_reason(codex(DESTROY, work))
    assert "Black Friday" in reason and POLICY_NOTE in reason


def test_a_freeze_in_ask_mode_is_approvable(work):
    now = datetime.now(UTC)
    org.set_fact(org.make_fact("freeze", "org:org",
                               {"start": (now - timedelta(hours=1)).isoformat(),
                                "end": (now + timedelta(hours=1)).isoformat(),
                                "reason": "quiet week", "mode": "ask"}, source="human"),
                 human("maria"))
    launch = "aws ec2 run-instances --instance-type m5.large"
    reason = codex_reason(codex(launch, work))
    assert "quiet week" in reason
    assert cli_approve(approval_id(reason), "--as", "maria") == 0
    assert codex(launch, work) is None


def test_on_budget_breach_deny_is_not_approvable(work, tmp_path, monkeypatch):
    from datetime import date

    from finops.budget import summary as bs
    today = datetime.now().astimezone().date()
    start = today.replace(day=1)
    nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    bs.write_summary([{"name": "AWS Total", "scope_type": "total", "scope_value": "*",
                       "period": "monthly", "period_start": start.isoformat(),
                       "period_end": (nxt - timedelta(days=1)).isoformat(),
                       "spent": 49_999.0, "limit": 50_000.0, "pct_used": 99.9,
                       "status": "ok"}],
                     spend_through=today.isoformat(),
                     now=datetime.now(UTC) - timedelta(hours=1))
    policy = tmp_path / "nable.policy.yaml"
    policy.write_text("on_budget_breach: deny\n")
    monkeypatch.setenv("FINOPS_POLICY_FILE", str(policy))
    reason = codex_reason(codex("aws ec2 run-instances --instance-type m5.2xlarge", work))
    assert "on_budget_breach: deny" in reason
    _no_id(reason)


def test_a_pack_deny_rule_is_not_approvable(work, monkeypatch):
    from finops import guard_packs
    from finops.packs.content import parse_guard_rules
    problems: list = []
    rules = parse_guard_rules({"rules": [{"id": "no-destroy", "pattern": r"terraform destroy",
                                          "verdict": "deny", "reason": "never here"}]},
                              "g.yaml", problems)
    assert not problems
    monkeypatch.setattr(guard_packs, "state", lambda: {"rules": rules, "guard_problems": []})
    reason = codex_reason(codex(DESTROY, work))
    assert "never here" in reason
    _no_id(reason)


def test_a_self_change_is_not_approvable_either(work):
    reason = codex_reason(codex("nable guard off", work))
    assert "turning the guard off" in reason and APPROVE_RE not in reason
    assert guard_approvals.waiting() == []


def test_the_hook_stays_fast_when_the_store_is_locked(work, monkeypatch):
    """A lock held elsewhere costs the approval id, never the verdict."""
    import contextlib

    @contextlib.contextmanager
    def held(_d):
        yield False
    monkeypatch.setattr(guard_approvals, "_locked", held)
    reason = codex_reason(codex(DESTROY, work))
    assert APPROVE_RE not in reason and "Codex hooks cannot pause" in reason
