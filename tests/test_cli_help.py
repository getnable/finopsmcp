"""The CLI's front door: grouped --help and did-you-mean errors.

--help used to print a flat wall of 45 subcommands with connectors first and
welcome/doctor buried; a typo'd command dumped every choice in one unreadable
argparse line. These lock in the grouped layout (start-here first, nothing falls
into the 'other' bucket) and the nearest-match error.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FINOPS = [sys.executable, "-m", "finops.setup_wizard"]
# This checkout's package, whatever the interpreter has installed.
SRC = str(Path(__file__).resolve().parents[1] / "src")


def _run(args, **kw):
    return subprocess.run(
        [sys.executable, "-c", "from finops.setup_wizard import main; main()"] ,
        input=kw.pop("input", ""), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "NABLE_NO_TELEMETRY": "1", "HOME": kw.pop("home", "/tmp"),
             "PYTHONPATH": SRC},
        timeout=60,
    )


def _help_text():
    r = subprocess.run(
        [sys.executable, "-c", "import sys; sys.argv=['finops','--help']; from finops.setup_wizard import main; main()"],
        capture_output=True, text=True, timeout=60,
        env={"NABLE_NO_TELEMETRY": "1", "HOME": "/tmp", "PYTHONPATH": SRC},
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
                "login", "vault", "guard", "iam-template", "serve", "cloud"):
        assert cmd in out, f"{cmd} missing from --help"


def _bad_cmd(cmd):
    return subprocess.run(
        [sys.executable, "-c", f"import sys; sys.argv=['finops','{cmd}']; from finops.setup_wizard import main; main()"],
        capture_output=True, text=True, timeout=60,
        env={"NABLE_NO_TELEMETRY": "1", "HOME": "/tmp", "PYTHONPATH": SRC},
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


def test_a_subcommand_help_lists_its_own_actions(tmp_path):
    """`nable org --help`, `learn --help` and `pack --help` printed the
    top-level layout: their actions under "other", plus the top level's
    "setup  Pick providers from the full menu" and "--version", neither of
    which the subcommand takes."""
    src = SRC
    for cmd, action in (("org", "status"), ("learn", "infer"), ("pack", "validate")):
        r = subprocess.run(
            [sys.executable, "-m", "finops.entry", cmd, "--help"], check=False,
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
            env={"NABLE_NO_TELEMETRY": "1", "HOME": str(tmp_path), "PYTHONPATH": src,
                 "PATH": "/usr/bin:/bin"})
        assert r.returncode == 0, r.stderr
        assert f"usage: nable {cmd}" in r.stdout, cmd
        assert action in r.stdout, cmd
        assert "Pick providers from the full menu" not in r.stdout, cmd
        assert "--version" not in r.stdout, cmd


def test_cloud_is_listed_with_the_account_commands():
    out = _help_text()
    group = out.split("account & billing", 1)[1].split("\n\n", 1)[0]
    assert "\n  cloud " in group, group
