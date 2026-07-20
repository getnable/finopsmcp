"""The CLI's front door: grouped --help and did-you-mean errors.

--help used to print a flat wall of 45 subcommands with connectors first and
welcome/doctor buried; a typo'd command dumped every choice in one unreadable
argparse line. These lock in the grouped layout (start-here first, nothing falls
into the 'other' bucket) and the nearest-match error.
"""
from __future__ import annotations

import subprocess
import sys

FINOPS = [sys.executable, "-m", "finops.setup_wizard"]


def _run(args, **kw):
    return subprocess.run(
        [sys.executable, "-c", "from finops.setup_wizard import main; main()"] ,
        input=kw.pop("input", ""), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "NABLE_NO_TELEMETRY": "1", "HOME": kw.pop("home", "/tmp")},
        timeout=60,
    )


def _help_text():
    r = subprocess.run(
        [sys.executable, "-c", "import sys; sys.argv=['finops','--help']; from finops.setup_wizard import main; main()"],
        capture_output=True, text=True, timeout=60,
        env={"NABLE_NO_TELEMETRY": "1", "HOME": "/tmp"},
    )
    return r.stdout + r.stderr


def test_help_is_grouped_start_here_first():
    out = _help_text()
    # start-here group exists and precedes the cloud connectors
    assert "start here" in out
    assert out.index("start here") < out.index("clouds")
    # welcome (the on-ramp) renders before aws (a connector)
    assert out.index("welcome") < out.index("aws ")


def test_help_has_no_other_bucket():
    # Every registered subcommand is assigned to a group. A new command that
    # nobody groups auto-renders under "other", and this test flags it.
    out = _help_text()
    assert "\nother\n" not in out


def test_help_covers_all_commands():
    out = _help_text()
    for cmd in ("welcome", "doctor", "aws", "gcp", "azure", "openai", "slack",
                "login", "vault", "guard", "iam-template", "serve"):
        assert cmd in out, f"{cmd} missing from --help"


def _bad_cmd(cmd):
    return subprocess.run(
        [sys.executable, "-c", f"import sys; sys.argv=['finops','{cmd}']; from finops.setup_wizard import main; main()"],
        capture_output=True, text=True, timeout=60,
        env={"NABLE_NO_TELEMETRY": "1", "HOME": "/tmp"},
    )


def test_typo_gets_did_you_mean():
    r = _bad_cmd("welcom")
    assert r.returncode == 2
    assert "unknown command 'welcom'" in r.stderr
    assert "Did you mean: welcome" in r.stderr
    # and it does NOT dump the full 45-choice argparse wall
    assert "invalid choice" not in r.stderr


def test_garbage_points_at_help():
    r = _bad_cmd("frobnicate9000")
    assert r.returncode == 2
    assert "nable --help" in r.stderr
