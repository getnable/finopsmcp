"""The CLI with nobody at a terminal: CI, cron, scripts and agent pipelines.

Every test here runs the real `nable` entry point in a subprocess with stdin
closed (no TTY, nothing to answer a prompt) and a timeout, against a fresh
HOME and data dir, the way a pipeline step runs it. What a headless caller
needs from each command: no prompt that waits, one JSON document on stdout
when it asks for one (logs and banners on stderr), and an exit code that
says what happened. docs/HEADLESS.md documents what these pin.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
_DROP = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
         "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
         "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "FINOPS_DB_PATH", "DATABASE_URL",
         "FINOPS_PROFILE", "FINOPS_LICENSE_KEY", "NABLE_BRIEF_DELIVER", "SLACK_WEBHOOK_URL",
         "SLACK_BOT_TOKEN", "TEAMS_WEBHOOK_URL", "FINOPS_AIRGAP", "FINOPS_DATA_DIR")


def nable(tmp_path: Path, *argv: str, env: dict[str, str] | None = None,
          timeout: float = 120) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    full = {k: v for k, v in os.environ.items() if k not in _DROP and not k.startswith("NABLE_")}
    full.update(HOME=str(home), PYTHONPATH=SRC, FINOPS_NO_KEYRING="1",
                AWS_CONFIG_FILE=str(home / "no-aws-config"),
                AWS_SHARED_CREDENTIALS_FILE=str(home / "no-aws-credentials"),
                AWS_EC2_METADATA_DISABLED="true")
    full.update(env or {})
    return subprocess.run([sys.executable, "-m", "finops.entry", *argv],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=timeout, env=full, cwd=str(home))


# ── doctor ────────────────────────────────────────────────────────────────────

def test_nable_doctor_json_is_one_document_on_stdout(tmp_path):
    """`nable doctor --json` was "unrecognized arguments: --json" (exit 2):
    only the separate finops-doctor script took the flag."""
    r = nable(tmp_path, "doctor", "--json")
    assert r.returncode in (0, 1), r.stderr[-800:]
    checks = json.loads(r.stdout)
    assert isinstance(checks, list) and checks
    assert all({"name", "ok", "detail"} <= set(c) for c in checks)
    assert r.returncode == (1 if any(c["ok"] is False for c in checks) else 0)


# ── guard check ───────────────────────────────────────────────────────────────

def test_guard_check_json_is_the_verdict(tmp_path):
    """`guard check --json` printed the human verdict line: the flag was
    accepted and ignored, so a pipeline vetting a command had to scrape text."""
    r = nable(tmp_path, "guard", "check", "--json", "--command", "terraform destroy")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["command"] == "terraform destroy"
    assert doc["decision"] == "ask" and doc["reason"]
    r = nable(tmp_path, "guard", "check", "--json", "--command", "ls -la")
    doc = json.loads(r.stdout)
    assert doc["decision"] == "allow" and doc["infra"] is False


# ── setup and serve: the commands with nothing to do here ─────────────────────

def test_provider_setup_that_stores_nothing_exits_1(tmp_path):
    """`nable setup openai < /dev/null` read no key, stored nothing, said so,
    and exited 0, so a provisioning script took it for a success."""
    r = nable(tmp_path, "setup", "openai")
    assert "Nothing stored" in r.stdout
    assert r.returncode == 1


def test_serve_without_the_dashboard_fails_on_stderr(tmp_path):
    """The container's default command. In the open package the dashboard is
    not there: `serve` said so on stdout and exited 0, so `docker run` and any
    supervisor read "finished successfully" from a server that never started."""
    import importlib.util
    if importlib.util.find_spec("finops.server_web") is not None:
        import pytest
        pytest.skip("the hosted dashboard is installed here; serve would start it")
    r = nable(tmp_path, "serve", "--host", "0.0.0.0", "--port", "8080", timeout=60)
    assert r.returncode == 1
    assert "hosted" in r.stderr and r.stdout.strip() == ""


# ── license ───────────────────────────────────────────────────────────────────

def test_license_status_fails_on_an_invalid_key_and_prints_no_escapes(tmp_path):
    """A pipeline checking its FINOPS_LICENSE_KEY secret got exit 0 and
    "Plan:    \\x1b[31mInvalid\\x1b[0m" in its log."""
    r = nable(tmp_path, "license-status", env={"FINOPS_LICENSE_KEY": "FINOPS-2-bogus"})
    assert "Invalid" in r.stdout
    assert "\x1b[" not in r.stdout
    assert r.returncode == 1
    ok = nable(tmp_path, "license-status")
    assert ok.returncode == 0 and "\x1b[" not in ok.stdout


# ── brief --deliver (the report a cron job sends) ─────────────────────────────

def _brief(tmp_path, channels: str | None):
    env = {"NABLE_BRIEF_DELIVER": channels} if channels is not None else {}
    r = nable(tmp_path, "brief", "--json", "--deliver", "--no-save", env=env)
    return r, json.loads(r.stdout)


def test_brief_deliver_says_a_push_failed_and_exits_1(tmp_path):
    """NABLE_BRIEF_DELIVER=slack with no Slack webhook: the push failed, and
    the run exited 0 with no word of delivery anywhere in its JSON."""
    r, doc = _brief(tmp_path, "slack")
    assert doc["delivered"] == {"slack": False}
    assert r.returncode == 1


def test_brief_deliver_with_no_valid_channel_exits_1(tmp_path):
    """--deliver asked for a push; a misspelled or empty channel list sent
    nothing, and that is not a success."""
    for channels in ("slakc", None):
        r, doc = _brief(tmp_path, channels)
        assert doc["delivered"] == {}
        assert r.returncode == 1, channels
