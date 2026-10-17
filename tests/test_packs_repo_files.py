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

CATALOG = ("catalog-info.yaml", "catalog-info.yml")


def _prep(scopes=("repo.files",), files=CATALOG) -> broker.Prepared:
    return broker.Prepared("io.github.example/p", "io.github.example", "p", "1.0.0", "adapters",
                           "a", "m:f", Path("/nonexistent"),
                           {"read_data": tuple(scopes), "repo_files": tuple(files)},
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
    _, problems = capabilities.validate({"read_data": ["repo.files"],
                                         "repo_files": ["catalog-info.yaml"]}, first_party=False)
    assert problems == []


def test_the_scope_needs_the_files_it_reads_declared():
    # review: repo.files read any plainly named file, terraform.tfstate or
    # id_rsa included. A pack now names the files (or simple globs) it reads.
    _, problems = capabilities.validate({"read_data": ["repo.files"]}, first_party=True)
    assert [p.field for p in problems] == ["capabilities.repo_files"]
    _, problems = capabilities.validate({"repo_files": ["CODEOWNERS"]}, first_party=True)
    assert [p.field for p in problems] == ["capabilities.repo_files"]
    got, problems = capabilities.validate(
        {"read_data": ["repo.files"],
         "repo_files": ["catalog-info.yaml", ".github/CODEOWNERS", "docs/*.md", "*/CODEOWNERS"]},
        first_party=False)
    assert problems == [] and got["repo_files"] == (
        "*/CODEOWNERS", ".github/CODEOWNERS", "catalog-info.yaml", "docs/*.md")
    for bad in ("../x", "/etc/passwd", "a//b", "**/x", "a/[x]", "a/./b", "", "a\\b", "~/.ssh"):
        _, problems = capabilities.validate({"read_data": ["repo.files"], "repo_files": [bad]},
                                            first_party=True)
        assert problems, bad


@pytest.mark.parametrize("name", [
    "terraform.tfstate", "prod.tfstate.backup", "id_rsa", "id_ed25519", ".env", ".env.local",
    "server.pem", "tls.key", "keystore.p12", "credentials", "credentials.json", ".netrc",
    ".git-credentials", ".npmrc", ".pypirc", "kubeconfig", "*", "*.tfstate", "secrets/*",
    "infra/terraform.tfstate", ".aws/credentials", "id_*"])
def test_a_sensitive_file_is_refused_even_when_declared(name):
    _, problems = capabilities.validate({"read_data": ["repo.files"], "repo_files": [name]},
                                        first_party=True)
    assert problems and "sensitive" in problems[0].reason, name


def test_repo_files_are_shown_diffed_and_need_approval_again():
    assert "catalog-info.yaml" in capabilities.describe("repo_files", "catalog-info.yaml")
    d = capabilities.diff({"read_data": ("repo.files",), "repo_files": ("catalog-info.yaml",)},
                          {"read_data": ("repo.files",),
                           "repo_files": ("catalog-info.yaml", "CODEOWNERS")})
    assert d["added"] == {"repo_files": ["CODEOWNERS"]}


def test_the_broker_refuses_a_file_the_pack_did_not_declare(repo):
    (repo / "terraform.tfstate").write_text('{"resources": []}')
    (repo / ".github").mkdir()
    (repo / ".github" / "CODEOWNERS").write_text("* @acme/platform")
    (repo / "CODEOWNERS").write_text("* @acme/root")
    for names in (["terraform.tfstate"], ["catalog-info.yaml", "id_rsa"], ["CODEOWNERS"],
                  ["*.yaml"], ["svc/a/other.yaml"]):
        with pytest.raises(_RpcError) as ei:
            broker.read_data(_prep(), {"scope": "repo.files", "query": {"names": names}},
                             [repo])
        assert ei.value.code == -32001 and "repo_files" in ei.value.message, names
    # A declared name covers that name at any one path.
    got = broker.read_data(_prep(), {"scope": "repo.files",
                                     "query": {"names": ["svc/a/catalog-info.yaml"]}}, [repo])
    assert [f["path"] for f in got["files"]] == ["svc/a/catalog-info.yaml"]
    # A path pattern reads that path only; a plain name, that name anywhere.
    got = broker.read_data(_prep(files=(".github/CODEOWNERS",)), {
        "scope": "repo.files", "query": {"names": [".github/CODEOWNERS"]}}, [repo])
    assert [f["path"] for f in got["files"]] == [".github/CODEOWNERS"]
    got = broker.read_data(_prep(files=("CODEOWNERS",)), {
        "scope": "repo.files", "query": {"names": ["CODEOWNERS"]}}, [repo])
    assert [f["path"] for f in got["files"]] == ["CODEOWNERS", ".github/CODEOWNERS"]
    # A declared glob may be asked for as declared, or by a name it covers.
    got = broker.read_data(_prep(files=("catalog-info.*",)), {
        "scope": "repo.files", "query": {"names": ["catalog-info.yml"]}}, [repo])
    assert [f["path"] for f in got["files"]] == ["svc/b/catalog-info.yml"]
    got = broker.read_data(_prep(files=("catalog-info.*",)), {
        "scope": "repo.files", "query": {"names": ["catalog-info.*"]}}, [repo])
    assert "svc/b/catalog-info.yml" in [f["path"] for f in got["files"]]


def test_the_broker_never_hands_over_a_sensitive_file_even_if_declared(repo):
    # An index or manifest that got past validation still cannot read one.
    (repo / "svc" / "a" / "terraform.tfstate").write_text("state")
    (repo / "svc" / "a" / ".env").write_text("TOKEN=x")
    with pytest.raises(_RpcError):
        broker.read_data(_prep(files=("terraform.tfstate",)), {
            "scope": "repo.files", "query": {"names": ["terraform.tfstate"]}}, [repo])
    got = broker.read_data(_prep(files=("*",)), {
        "scope": "repo.files", "query": {"names": ["*"]}}, [repo])
    paths = [f["path"] for f in got["files"]]
    assert "svc/a/catalog-info.yaml" in paths
    assert not any(p.endswith(("terraform.tfstate", ".env")) for p in paths)


def test_repo_files_returns_named_files_and_nothing_else(repo):
    got = broker.read_data(_prep(), {"scope": "repo.files", "query": {
        "names": ["catalog-info.yaml", "catalog-info.yml"]}}, [repo])
    assert got["truncated"] is False
    assert [(f["repo"], f["path"], f["text"]) for f in got["files"]] == [
        (0, "catalog-info.yaml", "root"), (0, "svc/a/catalog-info.yaml", "a"),
        (0, "svc/b/catalog-info.yml", "b")]


def test_repo_files_refuses_paths_globs_and_undeclared_packs(repo):
    for names in (["../etc/passwd"], ["/etc/passwd"], ["a//b"], ["**/x"], [],
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


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="no O_NOFOLLOW here")
def test_repo_files_never_follows_a_symlink_that_appears_after_the_check(repo, monkeypatch):
    # The walk checks a name is no symlink, then reads it: a link put there
    # in between (here: a check that sees none) must still not be followed.
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    got = broker.read_data(_prep(), {"scope": "repo.files",
                                     "query": {"names": ["catalog-info.yaml"]}}, [repo])
    assert "outside" not in [f["text"] for f in got["files"]]
    assert "svc/c/catalog-info.yaml" not in [f["path"] for f in got["files"]]


def test_repo_files_reads_no_more_than_its_limit(repo, monkeypatch):
    # A file that grows between the size check and the read is cut off, not
    # read whole.
    real_stat = Path.stat

    def small(self, **kw):
        st = list(real_stat(self, **kw))[:10]
        st[6] = 0                                        # st_size
        return os.stat_result(st)
    monkeypatch.setattr(Path, "stat", small)
    got = broker.read_data(_prep(), {"scope": "repo.files",
                                     "query": {"names": ["catalog-info.yaml"]}}, [repo])
    assert "big/catalog-info.yaml" not in [f["path"] for f in got["files"]]
    assert all(len(f["text"]) <= broker.REPO_FILE_MAX_BYTES for f in got["files"])


def test_pack_run_prints_nothing_an_adapter_returns_that_can_drive_a_terminal(
        monkeypatch, tmp_path, capsys):
    from finops import org
    from finops.setup_wizard import main
    fact = org.make_fact("approval", "team:platform",
                         {"action_classes": ["*"], "approvers": ["team:x"], "min": 1},
                         source="pack:io.github.example/p:\x1b]0;owned\x07a")
    monkeypatch.setattr(broker, "prepare", lambda *a, **kw: _prep())
    monkeypatch.setattr(broker, "propose_facts", lambda *a, **kw: broker.RunResult(
        "io.github.example/p", "a", "adapters", "adapter.propose", [fact],
        problems=["fact[1] dropped: bad \x1b[2J value"]))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as ei:
        main(["pack", "run", "io.github.example/p", "a"])
    out = capsys.readouterr().out
    assert ei.value.code == 0 and "owned" in out and "\x1b" not in out and "\x07" not in out


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
