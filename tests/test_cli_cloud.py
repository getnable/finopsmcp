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
import urllib.request
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
    assert ai_budget._read_git_email(str(tmp_path)) is None
    monkeypatch.delenv("FINOPS_GUARD_PERSON", raising=False)
    assert cli_cloud._person("") == (None, "")


# ── --delete-org-file (D97) ──────────────────────────────────────────────────

class _DeleteBox:
    """A box that takes org_file_delete lines and answers per file."""

    def __init__(self, *, accepts=("org_model", "ledger_line", "agent_usage",
                                   cc.DELETE_KIND)):
        from tests.test_cloud_connect import Box
        self.box = Box()
        self.accepts = list(accepts)

    @property
    def requests(self):
        return self.box.requests

    def __call__(self, req, timeout):
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        if req.get_method() == "GET" and path == "api/free-tool/status":
            self.box.requests.append({"method": "GET", "url": req.full_url,
                                      "headers": dict(req.header_items()), "body": b""})
            return 200, json.dumps({**self.box.status(), "accepts": self.accepts}).encode()
        if req.get_method() == "POST" and path == "api/free-tool/connect":
            docs = [json.loads(x) for x in (req.data or b"").split(b"\n") if x]
            deleted = [d["file"] for d in docs if d["kind"] == cc.DELETE_KIND]
            files = next((d["files"] for d in docs if d["kind"] == "org_model"), {})
            rest = b"".join(x + b"\n" for x in (req.data or b"").split(b"\n")
                            if x and json.loads(x)["kind"] != cc.DELETE_KIND)
            self.box.requests.append({"method": "POST", "url": req.full_url,
                                      "headers": dict(req.header_items()), "body": req.data})
            if rest:
                inner = urllib.request.Request(req.full_url, data=rest, method="POST")
                self.box(inner, timeout)
                self.box.requests.pop()
            if not (deleted or files):
                return 200, json.dumps({"accepted": {"org_model": None}}).encode()
            return 200, json.dumps({"accepted": {"org_model": "stored", "org_files": {
                "applied": [n for n in files if n in deleted],
                "unchanged": [],
                "stale": [{"file": n, "replaced_at": "2026-10-03T08:00:00+00:00"}
                          for n in files if n not in deleted],
                "deleted": [n for n in deleted if n not in files]}}}).encode()
        return self.box(req, timeout)


def test_a_delete_is_in_the_dry_run_and_the_confirm_code_covers_it(machine):
    code, out, _err = _run("connect", "--box", BOX, "--out", str(machine / "p.jsonl"),
                           "--delete-org-file", "teams.yaml", "--delete-org-file",
                           "budgets.yaml")
    assert code == 0
    assert "teams.yaml" in out and "delete on the workspace, then this machine's copy" in out
    assert "budgets.yaml" in out and "this machine sends no copy" in out
    with_delete = cc.build(delete_files=["teams.yaml", "budgets.yaml"])
    without = cc.build()
    docs = [json.loads(x) for x in with_delete.data.split(b"\n") if x]
    assert docs[:3] == [{"file": "teams.yaml", "kind": "org_file_delete", "v": 1},
                        {"file": "budgets.yaml", "kind": "org_file_delete", "v": 1},
                        {"files": ORG, "kind": "org_model", "v": 1}]
    assert cc.confirm_code(with_delete, BOX) != cc.confirm_code(without, BOX)
    assert (machine / "p.jsonl").read_bytes() == with_delete.data
    _code, out, _err = _run("connect", "--box", BOX, "--json", "--out",
                            str(machine / "p.jsonl"), "--delete-org-file", "teams.yaml")
    doc = json.loads(out)
    assert doc["org_file_deletes"] == ["teams.yaml"]
    assert doc["confirm_sha256"] == cc.confirm_code(cc.build(delete_files=["teams.yaml"]), BOX)


def _delete_sha(machine, *names: str) -> str:
    argv = [w for n in names for w in ("--delete-org-file", n)]
    code, out, _err = _run("connect", "--box", BOX, "--json", "--out",
                           str(machine / "p.jsonl"), *argv)
    assert code == 0
    return json.loads(out)["confirm_sha256"]


def test_a_delete_is_refused_before_anything_is_sent_by_a_box_that_does_not_take_it(machine):
    sha = _delete_sha(machine, "teams.yaml")
    box = _DeleteBox(accepts=("org_model", "ledger_line", "agent_usage"))
    code, _out, err = _run("connect", "--box", BOX, "--send", "--confirm", sha,
                           "--delete-org-file", "teams.yaml", opener=box,
                           environ={cc.KEY_ENV: KEY})
    assert code == cli_cloud.EXIT_REFUSED and "org_file_delete" in err
    assert [r["method"] for r in box.requests] == ["GET"], "something was sent"


def test_a_delete_and_the_file_go_in_one_batch_and_the_answer_is_printed(machine):
    sha = _delete_sha(machine, "teams.yaml")
    box = _DeleteBox()
    code, out, err = _run("connect", "--box", BOX, "--send", "--confirm", sha,
                          "--delete-org-file", "teams.yaml", opener=box,
                          environ={cc.KEY_ENV: KEY})
    assert code == 0, err
    first = next(r["body"] for r in box.requests if r["method"] == "POST")
    kinds = [json.loads(x)["kind"] for x in first.split(b"\n") if x]
    assert kinds[:2] == [cc.DELETE_KIND, "org_model"], "the delete and the file were split"
    assert "teams.yaml" in out and "applied" in out
    assert "owners.yaml" in out and "stale, not applied" in out
    assert "replaced it at 2026-10-03T08:00:00+00:00" in out
    assert "--delete-org-file owners.yaml" in out


def test_a_delete_resends_the_org_model_the_box_already_holds(machine):
    """The box holds this org model, so a plain resend skips it; with a delete
    the file goes again, after the delete, which is what makes it apply."""
    payload = cc.build()
    box = _DeleteBox()
    cc.send(payload, BOX, KEY, opener=box)
    assert cc.plan(payload, box.box.status()) == []
    with_delete = cc.build(delete_files=["teams.yaml"])
    batches = cc.plan(with_delete, box.box.status())
    sent = b"".join(with_delete.data[a:b] for a, b in batches)
    assert b'"kind":"org_model"' in sent and b'"kind":"org_file_delete"' in sent


@pytest.mark.parametrize("name", ["notes.yaml", "../teams.yaml", KEY, "teams.yml"])
def test_an_unknown_file_name_is_a_usage_error_that_echoes_nothing(machine, name):
    box = _DeleteBox()
    code, out, err = _run("connect", "--box", BOX, "--delete-org-file", name, opener=box)
    assert code == cli_cloud.EXIT_USAGE
    assert name not in out + err
    assert "teams.yaml" in err and box.requests == []
    with pytest.raises(ValueError):
        cc.build(delete_files=[name])


def test_status_shows_each_files_sha256_bytes_time_and_key(machine):
    st = {"org_model": {"versions": 2, "latest": {"files": {"teams.yaml": 10}, "sha256": "a",
                                                  "uploaded_at": "2026-10-04T00:00:00+00:00"},
                        "files": {"teams.yaml": {"sha256": "b" * 64, "bytes": 10,
                                                 "uploaded_at": "2026-10-03T00:00:00+00:00",
                                                 "by": "key:7"}}},
          "chains": []}
    text = cli_cloud._status_text(BOX, st)
    assert "teams.yaml" in text and "b" * 16 in text and "key:7" in text
    assert "2026-10-03T00:00:00+00:00" in text and "10 bytes" in text


def test_a_delete_and_the_org_model_are_never_cut_apart(machine, monkeypatch):
    """Batches are cut at line ends to stay under BATCH_MAX, but never between
    a delete and the org model after it: the box applies a file sent after
    its delete only when both are in one request."""
    payload = cc.build(delete_files=["teams.yaml", "owners.yaml"])
    org = next(e - s for k, s, e in payload.lines if k == "org_model")
    monkeypatch.setattr(cc, "BATCH_MAX", org)
    first = payload.data[slice(*cc.plan(payload, None)[0])]
    kinds = [json.loads(x)["kind"] for x in first.split(b"\n") if x]
    assert kinds == [cc.DELETE_KIND, cc.DELETE_KIND, "org_model"]


# ── authority.yaml goes only to a box that takes it (D138) ───────────────────

AUTHORITY = ("# nable org model v1\n- fact: authority\n  subject: action_class:rds_ri\n"
             "  value: {rung: 'off'}\n  source: human\n  status: confirmed\n"
             "  confirmed_at: 2026-10-01\n")


@pytest.fixture
def with_authority(machine):
    (machine / "org" / "authority.yaml").write_text(AUTHORITY)
    return machine


def _connect_sha(machine, *argv) -> str:
    code, out, err = _run("connect", "--box", BOX, "--json", "--out",
                          str(machine / "p.jsonl"), *argv)
    assert code == 0, err
    return json.loads(out)["confirm_sha256"]


def test_authority_yaml_is_in_the_dry_run_and_the_confirm_code(with_authority):
    code, out, _err = _run("connect", "--box", BOX, "--out", str(with_authority / "p.jsonl"))
    assert code == 0
    assert "authority.yaml" in out and "org_file:authority.yaml" in out
    assert "--without-org-file authority.yaml" in out
    payload = cc.build()
    org_line = next(json.loads(payload.data[s:e]) for k, s, e in payload.lines
                    if k == "org_model")
    assert org_line["files"]["authority.yaml"] == AUTHORITY
    assert payload.org_files == sorted([*ORG, "authority.yaml"])
    (with_authority / "org" / "authority.yaml").write_text(AUTHORITY.replace("'off'",
                                                                           "recommend"))
    assert cc.confirm_code(cc.build(), BOX) != cc.confirm_code(payload, BOX)


def test_a_box_that_does_not_take_authority_yaml_receives_nothing(with_authority):
    sha = _connect_sha(with_authority)
    box = _DeleteBox(accepts=("org_model", "ledger_line", "agent_usage", cc.DELETE_KIND))
    code, _out, err = _run("connect", "--box", BOX, "--send", "--confirm", sha, opener=box,
                           environ={cc.KEY_ENV: KEY})
    assert code == cli_cloud.EXIT_REFUSED
    assert "--without-org-file authority.yaml" in err and "org_file:authority.yaml" in err
    assert [r["method"] for r in box.requests] == ["GET"], "something was sent"


def test_a_box_whose_status_names_no_accepts_at_all_receives_nothing(with_authority):
    sha = _connect_sha(with_authority)
    box = Box()
    code, _out, err = _run("connect", "--box", BOX, "--send", "--confirm", sha, opener=box,
                           environ={cc.KEY_ENV: KEY})
    assert code == cli_cloud.EXIT_REFUSED and "--without-org-file authority.yaml" in err
    assert box.posted == []


def test_a_box_that_takes_it_receives_it(with_authority):
    sha = _connect_sha(with_authority)
    box = _DeleteBox(accepts=("org_model", "ledger_line", "agent_usage", cc.DELETE_KIND,
                              "org_file:authority.yaml"))
    code, _out, err = _run("connect", "--box", BOX, "--send", "--confirm", sha, opener=box,
                           environ={cc.KEY_ENV: KEY})
    assert code == 0, err
    sent = b"".join(r["body"] for r in box.requests if r["method"] == "POST")
    org_line = next(json.loads(x) for x in sent.split(b"\n")
                    if x and json.loads(x)["kind"] == "org_model")
    assert org_line["files"]["authority.yaml"] == AUTHORITY


def test_without_org_file_leaves_it_out_is_shown_and_changes_the_confirm_code(with_authority):
    with_it = _connect_sha(with_authority)
    code, out, _err = _run("connect", "--box", BOX, "--out", str(with_authority / "p.jsonl"),
                           "--without-org-file", "authority.yaml")
    assert code == 0 and "authority.yaml" in out
    assert "left out: --without-org-file authority.yaml" in out
    without = _connect_sha(with_authority, "--without-org-file", "authority.yaml")
    assert without != with_it
    payload = cc.build(without_files=["authority.yaml"])
    assert "authority.yaml" not in payload.org_files and payload.without == ["authority.yaml"]
    assert payload.org_files == sorted(ORG)
    assert (with_authority / "p.jsonl").read_bytes() == payload.data
    _code, out, _err = _run("connect", "--box", BOX, "--json", "--out",
                            str(with_authority / "p.jsonl"), "--without-org-file",
                            "authority.yaml")
    assert json.loads(out)["org_files_left_out"] == ["authority.yaml"]
    # The send made with the flag goes to a box that does not take the file;
    # the code shown with the flag does not send without it, nor the reverse.
    box = _DeleteBox(accepts=("org_model", "ledger_line", "agent_usage", cc.DELETE_KIND))
    code, _out, err = _run("connect", "--box", BOX, "--send", "--confirm", without,
                           "--without-org-file", "authority.yaml", opener=box,
                           environ={cc.KEY_ENV: KEY})
    assert code == 0, err
    assert all(b"authority.yaml" not in r["body"] for r in box.requests)
    box = _DeleteBox()
    code, _out, err = _run("connect", "--box", BOX, "--send", "--confirm", without,
                           opener=box, environ={cc.KEY_ENV: KEY})
    assert code == cli_cloud.EXIT_REFUSED and box.requests == []


def test_without_org_file_leaves_out_any_named_file(machine):
    payload = cc.build(without_files=["teams.yaml"])
    assert payload.org_files == sorted(set(ORG) - {"teams.yaml"})
    assert cc.confirm_code(payload, BOX) != cc.confirm_code(cc.build(), BOX)


@pytest.mark.parametrize("name", ["notes.yaml", "../authority.yaml", KEY])
def test_an_unknown_without_name_is_a_usage_error_that_echoes_nothing(machine, name):
    code, out, err = _run("connect", "--box", BOX, "--without-org-file", name)
    assert code == cli_cloud.EXIT_USAGE and name not in out + err
    assert "authority.yaml" in err
    with pytest.raises(ValueError):
        cc.build(without_files=[name])


def test_deleting_authority_yaml_on_a_box_that_does_not_take_it_is_refused(machine):
    sha = _delete_sha(machine, "authority.yaml")
    box = _DeleteBox()
    code, _out, err = _run("connect", "--box", BOX, "--send", "--confirm", sha,
                           "--delete-org-file", "authority.yaml", opener=box,
                           environ={cc.KEY_ENV: KEY})
    assert code == cli_cloud.EXIT_REFUSED and "--delete-org-file authority.yaml" in err
    assert [r["method"] for r in box.requests] == ["GET"]


def test_the_network_manifest_says_both():
    doc = json.loads((Path(__file__).resolve().parents[1] / "docs" /
                      "network-manifest.json").read_text())
    text = json.dumps(doc)
    assert "org_file:authority.yaml" in text and "--without-org-file" in text


def test_a_held_authority_yaml_is_said_as_held_never_as_applied():
    """A box that holds an authority.yaml that would widen purchase authority
    answers it under org_files.held: the send says so."""
    from finops import cli_cloud

    lines = cli_cloud._org_files_text({"held": [{"file": "authority.yaml", "delete": False,
                                                 "sha256": "x", "message": "held"}],
                                       "applied": ["teams.yaml"]})
    text = "\n".join(lines)
    assert "authority.yaml" in text and "held, not applied" in text and "/rds-ri" in text
    assert "teams.yaml" in text and "applied" in text
    into: dict = {}
    cc._gather_org_files(into, {"accepted": {"org_files": {
        "held": [{"file": "authority.yaml", "delete": True}]}}})
    assert into == {"held": [{"file": "authority.yaml", "delete": True}]}
    assert "its delete would widen" in "\n".join(cli_cloud._org_files_text(into))
