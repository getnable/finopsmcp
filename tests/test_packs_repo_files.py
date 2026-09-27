# SPDX-License-Identifier: Apache-2.0
"""The repo.files data scope: an adapter reads files by name from the repos
`nable org init` reads, through nable, and only when it declares the scope."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from finops.org.adapters import AdapterContext
from finops.org.model import OrgModel
from finops.packs import broker, capabilities
from finops.packs.broker import RepoRef, _RpcError


def _prep(scopes=("repo.files",)) -> broker.Prepared:
    return broker.Prepared("io.github.example/p", "io.github.example", "p", "1.0.0", "adapters",
                           "a", "m:f", Path("/nonexistent"), {"read_data": tuple(scopes)},
                           broker.signing.UNSIGNED, True, "community")


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    for rel, text in {"catalog-info.yaml": "root", "svc/a/catalog-info.yaml": "a",
                      "svc/b/catalog-info.yml": "b", "svc/b/other.yaml": "no",
                      "node_modules/x/catalog-info.yaml": "vendored",
                      ".git/catalog-info.yaml": "git"}.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(text)
    (r / "big").mkdir()
    (r / "big" / "catalog-info.yaml").write_bytes(b"x" * (broker.REPO_FILE_MAX_BYTES + 1))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "catalog-info.yaml").write_text("outside")
    if hasattr(os, "symlink"):
        (r / "linked").symlink_to(outside, target_is_directory=True)
        (r / "svc" / "c").mkdir()
        (r / "svc" / "c" / "catalog-info.yaml").symlink_to(outside / "catalog-info.yaml")
    return r


def test_the_scope_is_in_the_vocabulary():
    assert "repo.files" in capabilities.READ_DATA_SCOPES
    _, problems = capabilities.validate({"read_data": ["repo.files"]}, first_party=False)
    assert problems == []


def test_repo_files_returns_named_files_and_nothing_else(repo):
    got = broker.read_data(_prep(), {"scope": "repo.files", "query": {
        "names": ["catalog-info.yaml", "catalog-info.yml"]}}, [repo])
    assert got["truncated"] is False
    assert [(f["repo"], f["path"], f["text"]) for f in got["files"]] == [
        (0, "catalog-info.yaml", "root"), (0, "svc/a/catalog-info.yaml", "a"),
        (0, "svc/b/catalog-info.yml", "b")]


def test_repo_files_refuses_paths_globs_and_undeclared_packs(repo):
    for names in (["../etc/passwd"], ["svc/a/catalog-info.yaml"], ["*.yaml"], [".ssh"], [],
                  ["a"] * 17, "catalog-info.yaml", [1]):
        with pytest.raises(_RpcError) as ei:
            broker.read_data(_prep(), {"scope": "repo.files", "query": {"names": names}},
                             [repo])
        assert ei.value.code == -32602
    with pytest.raises(_RpcError) as ei:
        broker.read_data(_prep(("org.owners",)), {"scope": "repo.files",
                                                  "query": {"names": ["x"]}}, [repo])
    assert ei.value.code == -32001
    # No repos named: nothing to read.
    assert broker.read_data(_prep(), {"scope": "repo.files",
                                      "query": {"names": ["catalog-info.yaml"]}}) == {
        "files": [], "truncated": False}


def test_repo_files_stops_at_its_limits(repo, monkeypatch):
    monkeypatch.setattr(broker, "REPO_FILES_MAX", 1)
    got = broker.read_data(_prep(), {"scope": "repo.files",
                                     "query": {"names": ["catalog-info.yaml"]}}, [repo, repo])
    assert len(got["files"]) == 1 and got["truncated"] is True


def test_an_adapter_gets_its_repos_only_when_it_declares_the_scope(monkeypatch, tmp_path):
    seen = {}

    def fake_prepare(pack_id, entry_id, kind=None, *, pp=None):
        return broker.Prepared(pack_id, "io.github.example", "p", "1.0.0", "adapters",
                               entry_id, "m:f", tmp_path, seen["caps"],
                               broker.signing.UNSIGNED, True, "community")

    def fake_execute(prep, method, params, *, timeout=None, max_output=None, repos=None):
        seen["params"], seen["repos"] = params, repos
        return {"facts": []}, {"network": {}, "log": ""}

    monkeypatch.setattr(broker, "prepare", fake_prepare)
    monkeypatch.setattr(broker, "execute", fake_execute)
    ref = RepoRef(tmp_path / "shop", "github.com/acme/shop", "repo_path:github.com/acme/shop//",
                  "shop/")
    seen["caps"] = {"read_data": ("repo.files",), "write_org": ("proposals",)}
    broker.propose_facts("io.github.example/p", "a", {"today": "2026-09-27"}, repos=[ref])
    assert seen["repos"] == [tmp_path / "shop"]
    assert seen["params"]["context"] == {"today": "2026-09-27", "repos": [
        {"id": 0, "name": "github.com/acme/shop",
         "subject_prefix": "repo_path:github.com/acme/shop//", "label": "shop/"}]}
    assert str(tmp_path) not in repr(seen["params"])      # the path stays in the core
    seen["caps"] = {"read_data": ("org.owners",), "write_org": ("proposals",)}
    broker.propose_facts("io.github.example/p", "a", {"today": "2026-09-27"}, repos=[ref])
    assert seen["repos"] == [] and seen["params"]["context"] == {"today": "2026-09-27"}


def test_org_init_hands_its_repos_to_pack_adapters(monkeypatch, tmp_path):
    repo = tmp_path / "shop"
    (repo / ".git").mkdir(parents=True)
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(broker, "propose_facts",
                        lambda *a, **kw: calls.append(kw["repos"]) or broker.RunResult(
                            "p", "a", "adapters", "adapter.propose", []))
    ctx = AdapterContext(model=OrgModel([]), repos=[repo, other],
                         org_dir=repo / "nable.org")
    assert broker.PackAdapter("io.github.example/p", "a")(ctx) == []
    (refs,) = calls
    assert [(r.path, r.name, r.subject_prefix, r.label) for r in refs] == [
        (repo, "shop", "repo_path:", ""), (other, "other", "repo_path:other//", "other/")]
    assert [r.subject_prefix for r in broker.repo_refs(roots=[repo])] == ["repo_path:shop//"]
