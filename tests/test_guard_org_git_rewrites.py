"""git commands that rewrite the org model's files ask (D134).

A merge, pull, rebase, cherry-pick, revert, am, apply or hard reset, and the
`patch` program, can rewrite nable.org/authority.yaml without naming it, and
the guard reads git's index, not what a commit or a patch would change. So
each asks whenever the repo holds one of the guard's own org files (or the
org directory it would be made in), the way a stash pop does; one that only
reads (`git apply --check`, `git merge --help`) and one in a repo with no org
model never ask. A branch switch is not asked, and `nable guard doctor` says so.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

import finops.guard as g
import finops.guard_plugin as gp
from finops import ai_budget

OFF = ('# nable org model v1\n- fact: authority\n  subject: {kind: action_class, id: rds_ri}\n'
       '  value: {rung: "off"}\n  source: human\n  status: confirmed\n')


GIT = shutil.which("git")


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com"}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    if GIT is None:
        pytest.skip("git is not installed")
    home = tmp_path / "home"
    home.mkdir()
    r = tmp_path / "repo"
    monkeypatch.setenv("HOME", str(home))
    for var in ("FINOPS_DATA_DIR", "FINOPS_PROFILE", "FINOPS_ORG_DIR", "XDG_CONFIG_HOME",
                "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "CLAUDE_CONFIG_DIR",
                "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(gp, "_data_root_override", None)
    monkeypatch.setattr(gp, "_user_dir_override", None)
    monkeypatch.setattr(ai_budget, "status", lambda **_: {"verdict": ai_budget.BUDGET_OK})

    def git(*a):
        subprocess.run([GIT, "-c", "commit.gpgsign=false", *a], cwd=r, check=True,
                       capture_output=True, env=_git_env(), timeout=60)

    r.mkdir()
    git("init", "-q", "-b", "main")
    (r / "nable.org").mkdir()
    (r / "nable.org" / "authority.yaml").write_text(OFF)
    (r / "app.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-qm", "off")
    git("checkout", "-qb", "evil")
    (r / "nable.org" / "authority.yaml").write_text("# nable org model v1\n[]\n")
    git("commit", "-qam", "widen")
    git("format-patch", "-q", "-1", "-o", str(tmp_path / "p"))
    git("checkout", "-q", "main")
    monkeypatch.chdir(r)
    return r, next((tmp_path / "p").iterdir())


@pytest.mark.parametrize("cmd", [
    "git merge -q evil", "git pull . evil", "git rebase evil", "git cherry-pick evil",
    "git revert HEAD", "git am {p}", "git apply {p}", "git apply --cached {p}",
    "git reset -q --hard evil", "git reset --merge evil", "git -C . merge evil",
    "patch -p1 -i {p}", "patch -p1 < {p}", "cd sub && git merge evil"])
def test_a_command_that_can_rewrite_the_org_files_asks(repo, cmd):
    r, patch = repo
    (r / "sub").mkdir(exist_ok=True)
    v = g.gate_command(cmd.format(p=patch), "s1", cwd=str(r), record=False)
    assert v is not None and v["decision"] in ("ask", "deny"), (cmd, v)
    assert "authority.yaml" in v["reason"] or "nable.org" in v["reason"], v["reason"]


@pytest.mark.parametrize("cmd", ["git apply --check {p}", "git apply --stat {p}",
                                 "git merge --help", "git reset HEAD~1", "git reset --soft HEAD~1"])
def test_one_that_only_reads_or_moves_the_index_alone_does_not(repo, cmd):
    r, patch = repo
    assert (r / "nable.org" / "authority.yaml").exists()
    assert g.gate_command(cmd.format(p=patch), "s1", cwd=str(r), record=False) is None, cmd


def test_a_repo_with_no_org_model_is_never_asked(repo, tmp_path):
    _r, patch = repo
    other = tmp_path / "plain"
    other.mkdir()
    subprocess.run([GIT, "init", "-q"], cwd=other, check=True, capture_output=True,
                   env=_git_env(), timeout=60)
    for cmd in ("git merge evil", "git reset --hard HEAD", f"git apply {patch}",
                f"patch -p1 -i {patch}"):
        assert g.gate_command(cmd, "s1", cwd=str(other), record=False) is None, cmd


def test_the_doctor_names_the_branch_switch_it_does_not_ask(repo):
    gaps = " ".join(g.doctor()["not_covered"])
    assert "branch" in gaps and "git merge" not in gaps and "git reset" not in gaps
