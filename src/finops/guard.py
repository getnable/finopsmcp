"""Seamless agent cost guardrail: a PreToolUse hook for AI coding agents.

`finops guard install` wires nable's advisory policy gate (policy.py) into
Claude Code so it runs automatically whenever an agent is about to execute an
infrastructure-mutating shell command (terraform destroy, kubectl delete,
aws ec2 terminate-instances, a commitment purchase, ...). The agent no longer
has to remember to call check_action_policy; the harness enforces the check.

Verdict mapping (advisory, propose-only stays intact):
  escalate -> "ask"   the human sees the command plus the policy reason
  block    -> "deny"  the agent is told why and proposes something else
  allow    -> silent  zero friction, the command runs as normal

The hook never executes anything itself and it fails open: any internal error
exits 0 so a guard bug can never break the user's agent.

Strict mode (FINOPS_GUARD_STRICT=1) additionally asks on reversible
mutations (terraform apply, helm upgrade, kubectl apply/scale,
aws ec2 run-instances) with a nudge to cost the change first.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from .policy import GATE_ALLOW, GATE_BLOCK, GATE_ESCALATE, evaluate_action_gate

# ── Command classification ─────────────────────────────────────────────────────
# Ordered: first match wins. Maps shell commands to the policy action types in
# policy.py. Over-matching is tolerable (worst case an unnecessary confirm);
# missing a one-way door is not, so patterns are deliberately broad.

_ONE_WAY_CLASSIFIERS: list[tuple[str, str]] = [
    (r"\bterraform\s+(?:\S+\s+)*destroy\b", "delete_resource"),
    (r"\btofu\s+(?:\S+\s+)*destroy\b", "delete_resource"),
    # destroy hidden behind the apply verb: `terraform apply -destroy` is destroy.
    # Must sit in the one-way list (checked first) or the two-way apply pattern
    # would classify it as a reversible mutation.
    (r"\b(?:terraform|tofu)\s+(?:\S+\s+)*apply\b[^|;&]*\s-destroy\b", "delete_resource"),
    (r"\bpulumi\s+(?:\S+\s+)*destroy\b", "delete_resource"),
    (r"\beksctl\s+delete\b", "delete_resource"),
    # bucket/object wipes: `aws s3 rb` removes a bucket, `aws s3 rm --recursive`
    # empties one; gsutil is the GCP equivalent. Data deletion is a one-way door.
    (r"\baws\s+s3\s+r[mb]\b", "delete_resource"),
    (r"\bgsutil\s+(?:-\S+\s+)*r[mb]\b", "delete_resource"),
    (r"\bhelm\s+(?:uninstall|delete)\b", "delete_resource"),
    (r"\bkubectl\s+(?:\S+\s+)*delete\b", "delete_resource"),
    (r"\baws\s+ec2\s+terminate-instances\b", "terminate_instance"),
    (r"\baws\s+ec2\s+release-address\b", "release_ip"),
    (r"\baws\s+ec2\s+delete-snapshot\b", "snapshot_delete"),
    (r"\baws\s+(?:savingsplans\s+create-savings-plan|"
     r"ec2\s+purchase-reserved-instances-offering|"
     r"ec2\s+purchase-host-reservation|"
     r"rds\s+purchase-reserved-db-instances-offering)", "purchase_commitment"),
    (r"\baws\s+\S+\s+delete-[a-z0-9-]+", "delete_resource"),
    (r"\bgcloud\s+(?:\S+\s+)*delete\b", "delete_resource"),
    (r"\baz\s+(?:\S+\s+)*delete\b", "delete_resource"),
]

_TWO_WAY_CLASSIFIERS: list[tuple[str, str]] = [
    (r"\baws\s+ec2\s+stop-instances\b", "stop_idle"),
    (r"\bterraform\s+(?:\S+\s+)*apply\b", "infra_apply"),
    (r"\btofu\s+(?:\S+\s+)*apply\b", "infra_apply"),
    (r"\bhelm\s+(?:install|upgrade)\b", "infra_apply"),
    (r"\bkubectl\s+(?:apply|scale)\b", "infra_apply"),
    (r"\baws\s+ec2\s+run-instances\b", "infra_apply"),
]


def classify_command(command: str) -> tuple[str, str] | None:
    """Classify a shell command as ("one_way"|"two_way", action_type), or None
    when it is not an infrastructure mutation nable cares about."""
    cmd = " ".join(command.split())  # normalize whitespace
    for pattern, action in _ONE_WAY_CLASSIFIERS:
        if re.search(pattern, cmd):
            return ("one_way", action)
    for pattern, action in _TWO_WAY_CLASSIFIERS:
        if re.search(pattern, cmd):
            return ("two_way", action)
    return None


def _strict() -> bool:
    return os.getenv("FINOPS_GUARD_STRICT", "").strip().lower() in ("1", "true", "yes")


def gate_command(command: str) -> dict[str, Any] | None:
    """Evaluate a shell command against the policy gate.

    Returns None when the guard has no opinion (not infra, or an in-policy
    reversible action), else {decision: "ask"|"deny", reason, action_type}.
    """
    # Budget Guard is a Pro agent. On the free tier the hook stays silent (fail
    # open): a lapsed or missing license must never block someone's terminal. A
    # license-check error counts as "unknown", and unknown also fails open.
    try:
        from .license import feature_available
        if not feature_available("agent_gate"):
            return None
    except Exception:
        return None

    hit = classify_command(command)
    if hit is None:
        return None
    door, action_type = hit

    if action_type == "infra_apply":
        # Reversible mutation. Zero friction by default; strict mode confirms.
        if _strict():
            return {
                "decision": "ask",
                "action_type": action_type,
                "reason": ("nable guard (strict): this changes infrastructure and "
                           "therefore the bill. Cost it first (ask nable to "
                           "estimate_change_cost) or confirm to proceed."),
            }
        return None

    verdict = evaluate_action_gate(action_type)
    gate = verdict.get("gate")
    if gate == GATE_ESCALATE:
        return {
            "decision": "ask",
            "action_type": action_type,
            "reason": f"nable guard: {verdict.get('reason', 'a human must review this action.')}",
        }
    if gate == GATE_BLOCK:
        return {
            "decision": "deny",
            "action_type": action_type,
            "reason": f"nable guard: {verdict.get('reason', 'this action is not in your policy allowlist.')}",
        }
    return None  # allow -> stay silent


# ── Claude Code hook protocol ──────────────────────────────────────────────────

def run_hook(stdin: Any = None, stdout: Any = None) -> int:
    """PreToolUse hook body: JSON in on stdin, optional JSON verdict on stdout.

    Fails open by design: any error, unknown payload, or non-Bash tool exits 0
    with no output so the guard can never break the user's agent.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        payload = json.load(stdin)
        if payload.get("tool_name") != "Bash":
            return 0
        command = (payload.get("tool_input") or {}).get("command") or ""
        if not command:
            return 0
        verdict = gate_command(command)
        if not verdict:
            return 0
        json.dump({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": verdict["decision"],
                "permissionDecisionReason": verdict["reason"],
            }
        }, stdout)
        return 0
    except Exception:
        return 0


# ── Installer ──────────────────────────────────────────────────────────────────

# Marker used to find our hook in settings regardless of how the command is
# prefixed (bare, absolute path, or uvx wrapper).
_HOOK_MARKER = "guard hook"
_HOOK_CMD = "finops guard hook"


def _hook_command() -> str:
    """The command Claude Code should run for the hook, resolved to something
    that exists OUTSIDE this process. A uvx user has no `finops` on PATH, so the
    bare command would fail with command-not-found on every single Bash call and
    spam the agent with hook errors. Prefer a persistent binary; fall back to a
    uvx invocation (uv is guaranteed present for anyone who installed via uvx)."""
    import shutil
    found = shutil.which("finops")
    if found:
        # Quote in case the path has spaces (framework installs on macOS do not,
        # but user venvs can).
        return f'"{found}" guard hook' if " " in found else f"{found} guard hook"
    return "uvx --from finops-mcp finops guard hook"


def _settings_path(global_scope: bool) -> Path:
    if global_scope:
        return Path.home() / ".claude" / "settings.json"
    return Path.cwd() / ".claude" / "settings.json"


def _load_settings(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            raise SystemExit(
                f"  {path} exists but is not valid JSON; fix it first, nothing was changed.")
    return {}


def is_installed(path: Path) -> bool:
    try:
        s = json.loads(path.read_text())
    except Exception:
        return False
    for entry in (s.get("hooks", {}).get("PreToolUse") or []):
        for h in entry.get("hooks", []):
            cmd = h.get("command") or ""
            if _HOOK_MARKER in cmd and "finops" in cmd:
                return True
    return False


def install(global_scope: bool = False) -> Path:
    """Idempotently add the guard hook to Claude Code settings. Returns the path."""
    path = _settings_path(global_scope)
    settings = _load_settings(path)
    if is_installed(path):
        return path
    hooks = settings.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])
    cmd = _hook_command()
    pre.append({
        "matcher": "Bash",
        # uvx resolves an environment per call; give the cold-cache case room.
        # Timeouts fail open in Claude Code, so a slow first call cannot block.
        "hooks": [{"type": "command", "command": cmd,
                   "timeout": 30 if cmd.startswith("uvx") else 10}],
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    return path


def uninstall(global_scope: bool = False) -> bool:
    """Remove the guard hook. Returns True when something was removed."""
    path = _settings_path(global_scope)
    settings = _load_settings(path)
    pre = settings.get("hooks", {}).get("PreToolUse")
    if not pre:
        return False
    removed = False
    kept = []
    for entry in pre:
        inner = [h for h in entry.get("hooks", [])
                 if not (_HOOK_MARKER in (h.get("command") or "")
                         and "finops" in (h.get("command") or ""))]
        if len(inner) != len(entry.get("hooks", [])):
            removed = True
        if inner or not entry.get("hooks"):
            entry["hooks"] = inner
            if inner:
                kept.append(entry)
        else:
            removed = True
    if removed:
        settings["hooks"]["PreToolUse"] = kept
        if not kept:
            del settings["hooks"]["PreToolUse"]
        if not settings.get("hooks"):
            settings.pop("hooks", None)
        path.write_text(json.dumps(settings, indent=2) + "\n")
    return removed
