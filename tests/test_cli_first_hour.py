"""An engineer's first hour with the CLI: the commands the docs teach, run the
way a person runs them, in a fresh home with no cloud account.

Each test here started as a finding from walking that path:

  - `nable doctor --json` was refused ("unrecognized arguments") although
    `finops-doctor --json` and every other read command take --json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def _cli(args: list[str], home: Path, **env_extra: str) -> subprocess.CompletedProcess:
    """`nable <args>` in a fresh home, with no cloud credentials in reach."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AWS_", "AZURE_", "GOOGLE_", "FINOPS_", "CLAUDE"))}
    env.update(HOME=str(home), PYTHONPATH=str(SRC), NABLE_NO_TELEMETRY="1",
               PYTHONDONTWRITEBYTECODE="1", **env_extra)
    return subprocess.run([sys.executable, "-m", "finops.entry", *args], cwd=str(home),
                          capture_output=True, text=True, env=env, timeout=120, check=False)


# ── nable doctor --json ───────────────────────────────────────────────────────

def test_doctor_takes_json_like_finops_doctor(tmp_path):
    r = _cli(["doctor", "--json"], tmp_path)
    assert "unrecognized arguments" not in r.stderr, r.stderr
    checks = json.loads(r.stdout)
    assert isinstance(checks, list) and checks
    assert {"Python version", "License"} <= {c["name"] for c in checks}
    assert "Traceback" not in r.stderr


def test_doctor_audit_flags_reach_the_doctor(tmp_path):
    r = _cli(["doctor", "--audit", "--hours", "1", "--limit", "5", "--json"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert isinstance(json.loads(r.stdout), (list, dict))
