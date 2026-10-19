# SPDX-License-Identifier: Apache-2.0
"""`nable cloud connect` and `nable cloud status` on the command line.

    nable cloud connect --box https://HOST [--org-dir DIR] [--out PATH] [--json]
                        [--send --confirm CODE [--key-file PATH] [--as WHO]]
    nable cloud status  --box https://HOST [--key-file PATH] [--json]

A dry run unless --send. The workspace key is never an argument: a --key on
the command line is refused without being echoed or used, and so is anything
left over that could be one. Exit codes: 0 done, 1 refused or not reached (the
payload changed, the box said no, air-gap), 2 a usage error (an http box,
--send without --confirm, no key), 3 nothing to send.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from finops import cli_cloud
from finops import cloud_connect as cc
from tests.test_cloud_connect import BOX, KEY, ORG, Box, no_opener, records, write_ledger

SRC = str(Path(__file__).resolve().parents[1] / "src")


@pytest.fixture
def machine(tmp_path, monkeypatch):
    d = tmp_path / "org"
    d.mkdir()
    for name, text in ORG.items():
        (d / name).write_text(text)
    monkeypatch.setenv("FINOPS_ORG_DIR", str(d))
    monkeypatch.delenv(cc.KEY_ENV, raising=False)
    monkeypatch.delenv("FINOPS_AIRGAP", raising=False)
    write_ledger(records(4))
    return tmp_path


def _parse(*argv):
    p = argparse.ArgumentParser(prog="nable")
    sub = p.add_subparsers(dest="cmd")
    cli_cloud.add_parser(sub)
    return p.parse_args(["cloud", *argv])


def _run(*argv, opener=no_opener, environ=None, prompt=None, terminal=True):
    """`terminal`: whether a person is at a terminal (stdin and stdout)."""
    out, err = io.StringIO(), io.StringIO()
    code = cli_cloud.run(_parse(*argv), opener=opener, environ=environ or {},
                         prompt=prompt, stdout=out, stderr=err, terminal=terminal)
    return code, out.getvalue(), err.getvalue()


def _sha(machine, *argv, box=BOX) -> str:
    """The confirm code a dry run for `box` prints."""
    code, out, _ = _run("connect", "--box", box, "--json",
                        "--out", str(machine / "p.jsonl"), *argv)
    assert code == 0
    doc = json.loads(out)
    assert doc["confirm_sha256"] == cc.confirm_code(cc.build(), box)
    return doc["confirm_sha256"]


# ── the dry run ──────────────────────────────────────────────────────────────

def test_a_dry_run_shows_and_saves_and_opens_nothing(machine):
    out_file = machine / "payload.jsonl"
    code, out, err = _run("connect", "--box", BOX, "--out", str(out_file))
    assert code == cli_cloud.EXIT_OK, err
    payload = cc.build()
    for shown in ("Nothing was sent", "box.example.com", "owners.yaml", "teams.yaml",
                  "4 records", payload.sha256, str(out_file)):
        assert shown in out, shown
    assert out_file.read_bytes() == payload.data


def test_a_json_dry_run_is_one_document(machine):
    code, out, _err = _run("connect", "--box", BOX, "--json", "--out",
                           str(machine / "p.jsonl"))
    doc = json.loads(out)
    assert code == 0 and doc["dry_run"] is True and doc["sent"] is False
    assert doc["destination"]["host"] == "box.example.com"
    assert doc["ledger"]["records"] == 4 and set(doc["org_model"]["files"]) == set(ORG)


def test_nothing_to_send_exits_3(tmp_path, monkeypatch):
    monkeypatch.setenv("FINOPS_ORG_DIR", str(tmp_path / "none"))
    code, out, _err = _run("connect", "--box", BOX, "--out", str(tmp_path / "p"))
    assert code == cli_cloud.EXIT_NOTHING and "Nothing to send" in out


def test_air_gap_still_shows_the_dry_run_and_refuses_the_send(machine, monkeypatch):
    monkeypatch.setenv("FINOPS_AIRGAP", "1")
    code, out, _err = _run("connect", "--box", BOX, "--out", str(machine / "p"))
    assert code == 0 and "FINOPS_AIRGAP" in out
    sha = cc.confirm_code(cc.build(), BOX)
    code, _out, err = _run("connect", "--box", BOX, "--send", "--confirm", sha,
                           environ={cc.KEY_ENV: KEY})
    assert code == cli_cloud.EXIT_REFUSED and "FINOPS_AIRGAP" in err
    assert _run("status", "--box", BOX, environ={cc.KEY_ENV: KEY})[0] == 1


# ── the send ─────────────────────────────────────────────────────────────────

def test_a_send_needs_the_sha256_it_was_shown(machine):
    code, _out, err = _run("connect", "--box", BOX, "--send", environ={cc.KEY_ENV: KEY})
    assert code == cli_cloud.EXIT_USAGE and "--confirm" in err
    code, _out, err = _run("connect", "--box", BOX, "--confirm", "a" * 64)
    assert code == cli_cloud.EXIT_USAGE and "--send" in err


def test_a_send_to_another_box_than_the_dry_run_showed_is_refused(machine):
    """The confirm code covers where the bytes go: a dry run read for one box
    never sends them, or the key, to another."""
    sha = _sha(machine)
    box = Box()
    code, _out, err = _run("connect", "--box", "https://box-b.attacker.example", "--send",
                           "--confirm", sha, opener=box, environ={cc.KEY_ENV: KEY})
    assert code == cli_cloud.EXIT_REFUSED and box.requests == []
    assert "another box" in err and KEY not in err
    assert _run("connect", "--box", BOX, "--send", "--confirm", sha, opener=box,
                environ={cc.KEY_ENV: KEY})[0] == 0


def test_a_send_is_a_persons_decision_made_at_a_terminal(machine):
    """Like `nable org confirm`: without a terminal, and with no person named
    by --as, --send refuses and nothing is opened."""
    sha = _sha(machine)
    code, _out, err = _run("connect", "--box", BOX, "--send", "--confirm", sha,
                           environ={cc.KEY_ENV: KEY}, terminal=False)
    assert code == cli_cloud.EXIT_USAGE and "--as" in err
    box = Box()
    code, out, err = _run("connect", "--box", BOX, "--send", "--confirm", sha, "--as", "maria",
                          opener=box, environ={cc.KEY_ENV: KEY}, terminal=False)
    assert code == 0, err
    assert "maria" in out and b"maria" not in b"".join(box.posted)


def test_out_at_the_guards_off_switch_is_refused_and_the_guard_stays_on(machine):
    from finops import guard_plugin
    off = guard_plugin.off_flag_path()
    code, _out, err = _run("connect", "--box", BOX, "--out", str(off))
    assert code == cli_cloud.EXIT_USAGE and "guard" in err
    assert not off.exists() and not guard_plugin.is_off()


def test_a_payload_that_changed_since_the_dry_run_is_refused(machine):
    sha = _sha(machine)
    write_ledger(records(1, start=99))
    code, _out, err = _run("connect", "--box", BOX, "--send", "--confirm", sha,
                           environ={cc.KEY_ENV: KEY})
    assert code == cli_cloud.EXIT_REFUSED
    assert "the payload changed since you looked" in err


def test_a_confirmed_send_sends_exactly_the_payload(machine):
    sha = _sha(machine)
    box = Box()
    code, out, err = _run("connect", "--box", BOX, "--send", "--confirm", sha,
                          opener=box, environ={cc.KEY_ENV: KEY})
    assert code == 0, err
    assert b"".join(box.posted) == cc.build().data
    assert KEY not in out + err
    assert "Sent" in out and "holds" in out


def test_the_confirm_may_be_given_in_any_case(machine):
    sha = _sha(machine)
    box = Box()
    assert _run("connect", "--box", BOX, "--send", "--confirm", sha.upper(), opener=box,
                environ={cc.KEY_ENV: KEY})[0] == 0


def test_an_http_box_is_a_usage_error_and_nothing_is_built_or_sent(machine):
    for url in ("http://box.example.com", "box.example.com", "ftp://box.example.com"):
        code, _out, err = _run("connect", "--box", url, "--send", "--confirm", "a" * 64,
                               environ={cc.KEY_ENV: KEY})
        assert code == cli_cloud.EXIT_USAGE and "https" in err.lower(), url


def test_a_refusal_from_the_box_exits_1_and_says_why(machine):
    sha = _sha(machine)
    code, _out, err = _run("connect", "--box", BOX, "--send", "--confirm", sha,
                           opener=Box(refuse_batch=1), environ={cc.KEY_ENV: KEY})
    assert code == cli_cloud.EXIT_REFUSED and "chain_conflict" in err and KEY not in err


# ── the key ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("argv", [
    ["--key", KEY], [f"--key={KEY}"], ["--workspace-key", KEY], [KEY],
])
def test_a_key_on_the_command_line_is_refused_unechoed_and_unused(machine, argv):
    sha = cc.confirm_code(cc.build(), BOX)
    box = Box()
    code, out, err = _run("connect", "--box", BOX, "--send", "--confirm", sha, *argv,
                          opener=box, environ={})
    assert code == cli_cloud.EXIT_USAGE
    assert KEY not in out + err, "the key was echoed"
    assert box.requests == [], "a key from argv was used"
    assert "never taken from the command line" in err


@pytest.mark.parametrize("argv", [
    ["--key", KEY],
    # Flags nable does not have: argparse would name each one it did not
    # recognize, value and all, so `nable cloud` names none of them.
    [f"--token={KEY}"], [f"--bearer={KEY}"], [f"-k{KEY}"], ["--token", KEY],
    [f"--workspace-token={KEY}", "--json"],
])
def test_a_key_on_the_real_command_line_is_not_echoed(machine, tmp_path, argv):
    r = _nable(tmp_path, "cloud", "connect", "--box", BOX, *argv)
    assert r.returncode == cli_cloud.EXIT_USAGE, r.stderr
    assert KEY not in r.stdout + r.stderr, "the key was echoed"
    assert "never taken from the command line" in r.stderr
    if "--json" in argv:
        assert json.loads(r.stdout)["error"]["exit_code"] == cli_cloud.EXIT_USAGE


@pytest.mark.parametrize("argv", [
    # argparse prints the value of a flag that takes none, and the whole
    # word of an abbreviation two flags share, before nable sees either.
    [f"--send={KEY}"], [f"--json={KEY}"], [f"--ke={KEY}"], [f"--key-f={KEY}"],
    [f"--ou={KEY}"], ["--confirm"],
])
def test_a_flag_argparse_would_quote_back_is_not_echoed(machine, tmp_path, argv):
    r = _nable(tmp_path, "cloud", "connect", "--box", BOX, *argv)
    assert r.returncode == cli_cloud.EXIT_USAGE, r.stderr
    assert KEY not in r.stdout + r.stderr, "the key was echoed"
    r = _nable(tmp_path, "cloud", "status", "--box", BOX, *argv)
    assert KEY not in r.stdout + r.stderr, "the key was echoed"
    r = _nable(tmp_path, "cloud", KEY)
    assert r.returncode == cli_cloud.EXIT_USAGE and KEY not in r.stdout + r.stderr


def test_an_unknown_flag_elsewhere_still_names_itself(machine, tmp_path):
    """Only `nable cloud` keeps an unknown flag's text back; every other
    command still says which flag it did not know."""
    r = _nable(tmp_path, "brief", "--jsn")
    assert r.returncode == 2 and "--jsn" in r.stderr


def test_a_key_file_must_be_0600(machine, tmp_path):
    sha = _sha(machine)
    f = tmp_path / "key"
    f.write_text(KEY)
    f.chmod(0o644)
    code, _out, err = _run("connect", "--box", BOX, "--send", "--confirm", sha,
                           "--key-file", str(f))
    assert code == cli_cloud.EXIT_USAGE and "0600" in err
    f.chmod(0o600)
    box = Box()
    assert _run("connect", "--box", BOX, "--send", "--confirm", sha, "--key-file", str(f),
                opener=box)[0] == 0
    assert box.requests[0]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_no_key_and_no_terminal_is_a_usage_error(machine):
    sha = _sha(machine)
    code, _out, err = _run("connect", "--box", BOX, "--send", "--confirm", sha, prompt=None)
    assert code == cli_cloud.EXIT_USAGE and cc.KEY_ENV in err


def test_the_prompt_is_used_when_there_is_one(machine):
    sha = _sha(machine)
    box = Box()
    code, out, err = _run("connect", "--box", BOX, "--send", "--confirm", sha, opener=box,
                          prompt=lambda _text: KEY)
    assert code == 0 and KEY not in out + err


# ── status ───────────────────────────────────────────────────────────────────

def test_status_says_what_the_workspace_holds(machine):
    box = Box()
    cc.send(cc.build(), BOX, KEY, opener=box)
    code, out, _err = _run("status", "--box", BOX, opener=box, environ={cc.KEY_ENV: KEY})
    assert code == 0 and "4" in out and "chain" in out.lower()
    code, out, _err = _run("status", "--box", BOX, "--json", opener=box,
                           environ={cc.KEY_ENV: KEY})
    assert json.loads(out)["totals"]["lines"] == 4


def test_status_refused_exits_1(machine):
    def refuse(req, timeout):
        return 403, b'{"error": "forbidden"}'
    code, _out, err = _run("status", "--box", BOX, opener=refuse, environ={cc.KEY_ENV: KEY})
    assert code == 1 and "403" in err


# ── the wiring ───────────────────────────────────────────────────────────────

def _nable(tmp_path, *argv):
    env = {k: v for k, v in os.environ.items() if k != cc.KEY_ENV}
    env.update(PYTHONPATH=SRC, HOME=str(tmp_path / "home"), NABLE_NO_TELEMETRY="1",
               FINOPS_DATA_DIR=str(tmp_path / "data"))
    return subprocess.run([sys.executable, "-c",
                           "from finops.setup_wizard import main; main()", *argv],
                          capture_output=True, text=True, env=env, timeout=60,
                          stdin=subprocess.DEVNULL, check=False)


def test_the_command_is_registered_and_says_it_sends(machine, tmp_path):
    r = _nable(tmp_path, "cloud", "--help")
    assert r.returncode == 0 and "connect" in r.stdout and "status" in r.stdout
    r = _nable(tmp_path, "cloud", "connect", "--help")
    assert "--send" in r.stdout and "--confirm" in r.stdout and "--key-file" in r.stdout
    assert "\n  --key " not in r.stdout, "a --key option is offered"


def test_the_headless_dry_run_prints_no_local_only_banner(machine, tmp_path):
    r = _nable(tmp_path, "cloud", "connect", "--box", BOX)
    assert r.returncode == 0, r.stderr
    assert "credentials stay on your machine" not in r.stdout + r.stderr
    assert "Nothing was sent" in r.stdout


# ── the suite itself ─────────────────────────────────────────────────────────

def test_the_suite_never_reads_this_machines_transcripts_or_git_identity(tmp_path, monkeypatch):
    """A dry run names a developer and reads Claude Code's transcripts. Under
    the suite neither may be the developer's own (conftest): Claude Code's
    config dir is an empty sandbox whatever HOME is, and git's email is
    never read from PATH's git."""
    from finops import ai_budget
    home = tmp_path / "home"
    (home / ".claude" / "projects" / "-x").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    projects = ai_budget._claude_projects_dir()
    assert not str(projects).startswith(str(home))
    assert not projects.exists() or not any(projects.iterdir())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text("#!/bin/sh\necho someone@this-machine.example\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    assert cli_cloud._git_email() is None
