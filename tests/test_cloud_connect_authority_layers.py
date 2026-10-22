# SPDX-License-Identifier: Apache-2.0
"""`nable cloud connect` never sends an authority.yaml wider than this machine shows (D134).

`nable org authority` shows the strictest answer across every layer the
model reads: a repo's nable.org/ and the data dir's model under it. Cloud
connect sends one directory's files. When the authority.yaml it would send
answers wider for any purchase kind than what this machine shows, it is left
out of the payload, in the dry run and so in what leaves, with the reason
naming the stricter file and how to send it; the box then keeps what it has.
An authority.yaml as narrow as what this machine shows, or narrower, is sent.
"""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

import pytest

from finops import cloud_connect as cc

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
APPROVE = ('# nable org model v1\n- fact: authority\n  subject: {kind: action_class, id: '
           'rds_ri}\n  value: {rung: approve_each, approvers: ["email:ana@acme.example"], '
           'expires: 2027-06-30}\n  source: human\n  status: confirmed\n'
           '  confirmed_at: 2026-10-01\n')
OFF = ('# nable org model v1\n- fact: authority\n  subject: {kind: action_class, id: '
       'rds_ri}\n  value: {rung: "off"}\n  source: human\n  status: confirmed\n'
       '  confirmed_at: 2026-10-01\n')


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A trusted repo with nable.org/, and the data dir's model under it."""
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "nable.org").mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("FINOPS_DATA_DIR", "FINOPS_PROFILE", "FINOPS_ORG_DIR", "XDG_CONFIG_HOME",
                "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"):
        monkeypatch.delenv(var, raising=False)
    data_org = home / ".finops" / "org"
    data_org.mkdir(parents=True)
    (data_org / "trusted-repos.json").write_text(json.dumps(
        {"repos": [{"root": str(repo.resolve()), "remote": "", "by": "t",
                    "at": "2026-10-01"}]}))
    monkeypatch.chdir(repo)
    return repo, data_org, tmp_path


def _build(tmp):
    return cc.build(agent_usage=False, ledger_path=tmp / "none.jsonl", now=NOW)


def sent(p) -> dict:
    """The org model files the payload carries, as the box would read them."""
    for line in p.data.splitlines():
        doc = json.loads(line)
        if doc["kind"] == "org_model":
            return doc["files"]
    return {}


def test_a_repo_file_wider_than_the_data_dirs_is_left_out_with_why(machine):
    repo, data_org, tmp = machine
    (repo / "nable.org" / "authority.yaml").write_text(APPROVE)
    (data_org / "authority.yaml").write_text(OFF)
    from finops.org import store
    shown = store.load().authority()["classes"]["rds_ri"]["rung"]
    assert shown == "off"
    p = _build(tmp)
    assert p.org["dir"] == str(repo / "nable.org")
    assert "authority.yaml" not in p.org["files"] and "authority.yaml" not in sent(p)
    why = p.org["not_read"]["authority.yaml"]
    assert "rds_ri" in why and str(data_org / "authority.yaml") in why and "--org-dir" in why
    assert b"approve_each" not in p.data
    said = cc.describe(p, "https://box.example.com", None)
    assert "authority.yaml" in said and "wider" in said


def test_one_as_narrow_as_what_this_machine_shows_is_sent(machine):
    repo, data_org, tmp = machine
    (repo / "nable.org" / "authority.yaml").write_text(OFF)
    (data_org / "authority.yaml").write_text(APPROVE)
    p = _build(tmp)
    assert sent(p)["authority.yaml"] == OFF and "authority.yaml" not in p.org["not_read"]


def test_with_no_other_layer_the_file_is_sent_as_it_is(machine):
    repo, _data_org, tmp = machine
    (repo / "nable.org" / "authority.yaml").write_text(APPROVE)
    p = _build(tmp)
    assert sent(p)["authority.yaml"] == APPROVE


def test_the_data_dir_named_with_org_dir_sends_its_own_stricter_file(machine):
    repo, data_org, tmp = machine
    (repo / "nable.org" / "authority.yaml").write_text(APPROVE)
    (data_org / "authority.yaml").write_text(OFF)
    p = cc.build(org_dir=str(data_org), agent_usage=False, ledger_path=tmp / "none.jsonl",
                 now=NOW)
    assert sent(p)["authority.yaml"] == OFF


def test_it_reads_no_network_and_runs_no_program(machine, monkeypatch):
    repo, data_org, tmp = machine
    (repo / "nable.org" / "authority.yaml").write_text(APPROVE)
    (data_org / "authority.yaml").write_text(OFF)

    def no(*a, **k):
        raise AssertionError("a program was run")

    monkeypatch.setattr(subprocess, "run", no)
    monkeypatch.setattr(subprocess, "Popen", no)
    assert "authority.yaml" in _build(tmp).org["not_read"]
