# SPDX-License-Identifier: Apache-2.0
"""Installing packs: every source, every refusal, the capability diff on
update, tamper detection, and the one consumer wired end to end."""
from __future__ import annotations

import asyncio
import io
import json
import os
import tarfile
from datetime import date

import pytest

from finops import packs
from finops.packs import install as inst
from finops.packs import store
from finops.packs.errors import ApprovalRequired, IntegrityError, PackError, ValidationError
from tests import packs_support
from tests.packs_support import (
    EXAMPLE_PACK,
    copy_pack,
    make_git_repo,
    make_pack,
    make_tarball,
    sign_pack,
)

# The shared fixtures: an isolated packs root, policy file and registry
# setting; a throwaway key standing in for nable's first-party key.
packs_env = packs_support.packs_env
first_party_key = packs_support.first_party_key


def _index() -> dict:
    return json.loads(store.index_path().read_text())


# ── sources ──────────────────────────────────────────────────────────────────

def test_install_from_a_directory_records_everything(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref: x\n")      # a working copy's .git is skipped
    r = inst.install(str(src), yes=True)
    assert r["status"] == "installed"
    e = _index()["packs"]["io.github.example/demo"]
    assert e["source"] == {"kind": "dir", "spec": str(src.resolve()),
                           "location": str(src.resolve())}
    assert set(e["files"]) == {"nable-pack.toml", "policies/rules.yaml", "guard/rules.yaml",
                               "prices/book.yaml", "skills/demo-skill/SKILL.md"}
    assert all(len(h) == 64 for h in e["files"].values())
    assert e["capabilities"] == {"read_data": ["focus.cost"]}
    assert e["approval"] == "--yes" and e["approved_by"] and e["approved_at"]
    assert (packs_env.root / "io.github.example" / "demo" / "1.0.0" / "nable-pack.toml").is_file()
    assert oct(store.index_path().stat().st_mode & 0o777) == "0o600"


def test_install_from_a_tarball(packs_env, tmp_path):
    tgz = make_tarball(make_pack(tmp_path / "src"), tmp_path / "demo.tar.gz")
    r = inst.install(str(tgz), yes=True)
    src = _index()["packs"]["io.github.example/demo"]["source"]
    assert r["status"] == "installed" and src["kind"] == "tarball"
    assert src["sha256"] == store.sha256_file(tgz)


def _evil_tar(tmp_path, add) -> str:
    path = tmp_path / "evil.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        good = make_pack(tmp_path / "good")
        tf.add(good, arcname="demo")
        add(tf)
    return str(path)


def _member(tf, name, data=b"x", **kw):
    info = tarfile.TarInfo(name)
    for k, v in kw.items():
        setattr(info, k, v)
    info.size = len(data) if info.type == tarfile.REGTYPE else 0
    tf.addfile(info, io.BytesIO(data) if info.type == tarfile.REGTYPE else None)


@pytest.mark.parametrize("attack,needle", [
    (lambda tf: _member(tf, "../escape.txt"), "leaves the archive"),
    (lambda tf: _member(tf, "demo/../../escape.txt"), "leaves the archive"),
    (lambda tf: _member(tf, "/etc/evil.txt"), "absolute path"),
    (lambda tf: _member(tf, "demo/link", type=tarfile.SYMTYPE, linkname="/etc/passwd"),
     "is a link"),
    (lambda tf: _member(tf, "demo/hard", type=tarfile.LNKTYPE, linkname="demo/nable-pack.toml"),
     "is a link"),
    (lambda tf: _member(tf, "demo/fifo", type=tarfile.FIFOTYPE), "special file"),
    (lambda tf: _member(tf, "demo\\..\\x"), "backslash"),
])
def test_tarball_attacks_are_refused_whole(packs_env, tmp_path, attack, needle):
    evil = _evil_tar(tmp_path, attack)
    with pytest.raises(PackError) as ei:
        inst.install(evil, yes=True)
    assert "nothing was extracted" in ei.value.message
    assert any(needle in str(p) for p in ei.value.problems), ei.value.problems
    assert not (tmp_path / "escape.txt").exists()
    assert not store.index_path().exists()
    # the staging area is cleaned up
    assert not any((packs_env.root / ".staging").iterdir())


def test_a_symlink_in_a_directory_pack_is_refused(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    os.symlink("/etc/passwd", src / "policies" / "passwd.yaml")
    with pytest.raises(PackError) as ei:
        inst.install(str(src), yes=True)
    assert any("symlink" in str(p) for p in ei.value.problems)


def test_install_from_a_local_git_repo_pinned_to_a_commit(packs_env, tmp_path):
    url, commit = make_git_repo(tmp_path, make_pack(tmp_path / "src"), subdir="packs/demo")
    r = inst.install(f"git+{url}@{commit}#subdir=packs/demo", yes=True)
    src = _index()["packs"]["io.github.example/demo"]["source"]
    assert r["status"] == "installed"
    assert src["kind"] == "git" and src["commit"] == commit and src["subdir"] == "packs/demo"
    installed = packs_env.root / "io.github.example" / "demo" / "1.0.0"
    assert not (installed / ".git").exists()


@pytest.mark.parametrize("spec,needle", [
    ("git+file:///x/repo@main", "40-character commit"),
    ("git+file:///x/repo", "40-character commit"),
    ("git+http://example.com/r@" + "a" * 40, "https://"),
    ("git+ext::sh -c evil@" + "a" * 40, "https://"),
    ("git+file:///x@" + "a" * 40 + "#subdir=../up", ".."),
    ("https://example.com/pack.tar.gz", "git+https"),
    ("/definitely/not/here", "not a directory"),
])
def test_bad_sources_are_refused_before_anything_runs(packs_env, spec, needle):
    with pytest.raises(PackError) as ei:
        inst.parse_source(spec)
    assert needle in str(ei.value)


def test_a_commit_that_does_not_exist_is_refused(packs_env, tmp_path):
    url, _ = make_git_repo(tmp_path, make_pack(tmp_path / "src"))
    with pytest.raises(PackError) as ei:
        inst.install(f"git+{url}@{'0' * 40}", yes=True)
    assert "git checkout failed" in str(ei.value)


# ── integrity ────────────────────────────────────────────────────────────────

def _with_integrity(src, files: dict) -> None:
    body = ", ".join(f'"{k}" = "{v}"' for k, v in files.items())
    m = src / "nable-pack.toml"
    m.write_text(m.read_text() + f"\n[integrity]\nfiles = {{{body}}}\n")


def test_integrity_hashes_are_verified_when_present(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    good = {k: v for k, v in store.hash_tree(src).items() if k != "nable-pack.toml"}
    _with_integrity(src, good)
    assert inst.install(str(src), yes=True)["status"] == "installed"


def test_an_integrity_mismatch_is_refused(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    pins = {k: v for k, v in store.hash_tree(src).items() if k != "nable-pack.toml"}
    _with_integrity(src, pins)
    (src / "guard" / "rules.yaml").write_text((src / "guard" / "rules.yaml").read_text()
                                              + "# changed after pinning\n")
    (src / "extra.txt").write_text("slipped in")
    with pytest.raises(IntegrityError) as ei:
        inst.install(str(src), yes=True)
    probs = "\n".join(map(str, ei.value.problems))
    assert "guard/rules.yaml: does not match" in probs
    assert "extra.txt: is in the pack but not pinned" in probs
    assert not store.index_path().exists()


# ── approval and updates ─────────────────────────────────────────────────────

def test_nothing_installs_without_approval(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    with pytest.raises(ApprovalRequired):
        inst.install(str(src))
    with pytest.raises(ApprovalRequired):
        inst.install(str(src), approve=lambda plan: False)
    seen = {}
    r = inst.install(str(src), approve=lambda plan: seen.setdefault("plan", plan) is not None)
    assert r["status"] == "installed" and seen["plan"].manifest.id == "io.github.example/demo"
    assert _index()["packs"]["io.github.example/demo"]["approval"] == "interactive"
    with pytest.raises(ApprovalRequired):
        inst.install(str(make_pack(tmp_path / "other", name="other")), auto=True)


def test_update_that_adds_a_capability_needs_reapproval(packs_env, tmp_path):
    inst.install(str(make_pack(tmp_path / "v1")), yes=True)
    v2 = make_pack(tmp_path / "v2", version="1.1.0", capabilities=(
        'read_data = ["focus.cost"]\nnetwork = ["collector.example.com:443"]\n'))
    # an unattended update refuses it and says what was added
    with pytest.raises(ApprovalRequired) as ei:
        inst.install(str(v2), auto=True)
    assert "network: collector.example.com:443" in str(ei.value)
    assert _index()["packs"]["io.github.example/demo"]["version"] == "1.0.0"
    # a person sees the diff and approves
    seen = {}

    def approve(plan):
        seen["added"] = plan.added
        return True
    r = inst.install(str(v2), approve=approve)
    assert r["status"] == "updated" and seen["added"] == {"network": ["collector.example.com:443"]}
    e = _index()["packs"]["io.github.example/demo"]
    assert e["version"] == "1.1.0" and e["previous_version"] == "1.0.0"
    assert not (packs_env.root / "io.github.example" / "demo" / "1.0.0").exists()


def test_update_with_no_new_capability_can_apply_unattended(packs_env, tmp_path):
    inst.install(str(make_pack(tmp_path / "v1")), yes=True)
    r = inst.install(str(make_pack(tmp_path / "v2", version="1.0.1",
                                   capabilities="read_data = []\n")), auto=True)
    assert r["status"] == "updated"
    assert _index()["packs"]["io.github.example/demo"]["approval"].startswith("auto-update")


def test_same_version_different_files_and_downgrades_are_refused(packs_env, tmp_path):
    src = make_pack(tmp_path / "v1")
    inst.install(str(src), yes=True)
    assert inst.install(str(src), yes=True)["status"] == "unchanged"
    (src / "policies" / "rules.yaml").write_text(
        (src / "policies" / "rules.yaml").read_text() + "# republished\n")
    with pytest.raises(PackError, match="needs a new version"):
        inst.install(str(src), yes=True)
    inst.install(str(make_pack(tmp_path / "v2", version="2.0.0")), yes=True)
    with pytest.raises(PackError, match="older"):
        inst.install(str(make_pack(tmp_path / "v0", version="1.5.0")), yes=True)


def test_update_by_source_requires_an_installed_pack(packs_env, tmp_path):
    with pytest.raises(PackError, match="not installed"):
        inst.update(str(make_pack(tmp_path / "v1")), yes=True)


def test_invalid_content_is_refused_with_every_problem(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    (src / "guard" / "rules.yaml").write_text(
        "rules:\n  - {id: g, pattern: x, verdict: allow, reason: r}\n")
    with pytest.raises(ValidationError) as ei:
        inst.install(str(src), yes=True)
    assert any("may only tighten" in str(p) for p in ei.value.problems)


# ── remove, list, audit ──────────────────────────────────────────────────────

def test_remove_and_list(packs_env, tmp_path):
    inst.install(str(make_pack(tmp_path / "a")), yes=True)
    inst.install(str(make_pack(tmp_path / "b", name="bravo")), yes=True)
    ids = [p["id"] for p in inst.list_installed()]
    assert ids == ["io.github.example/bravo", "io.github.example/demo"]
    assert "files" not in inst.list_installed()[0]
    inst.remove("io.github.example/demo")
    assert [p["id"] for p in inst.list_installed()] == ["io.github.example/bravo"]
    assert not (packs_env.root / "io.github.example" / "demo").exists()
    with pytest.raises(PackError, match="not installed"):
        inst.remove("io.github.example/demo")


def test_audit_detects_tampering_and_runtime_stops_loading_it(packs_env, tmp_path):
    inst.install(str(make_pack(tmp_path / "src")), yes=True)
    assert inst.audit()["ok"]
    assert packs.guard_rules() and packs.load_problems() == []
    root = packs_env.root / "io.github.example" / "demo" / "1.0.0"
    (root / "guard" / "rules.yaml").write_text("rules: []\n")
    (root / "policies" / "new.yaml").write_text("rules: []\n")
    (root / "prices" / "book.yaml").unlink()
    a = inst.audit()
    row = a["packs"][0]
    assert not a["ok"] and row["status"] == "tampered"
    assert row["integrity"] == {"modified": ["guard/rules.yaml"], "missing": ["prices/book.yaml"],
                                "added": ["policies/new.yaml"]}
    from finops.packs import runtime
    runtime.invalidate()
    assert packs.guard_rules() == []
    assert "changed since it was approved" in packs.load_problems()[0]


def test_audit_flags_a_symlink_planted_after_install(packs_env, tmp_path):
    inst.install(str(make_pack(tmp_path / "src")), yes=True)
    root = packs_env.root / "io.github.example" / "demo" / "1.0.0"
    os.symlink("/etc/passwd", root / "policies" / "x.yaml")
    row = inst.audit()["packs"][0]
    assert row["status"] == "tampered" and any("symlink" in p for p in row["problems"])


def test_reinstalling_a_tampered_pack_restores_the_approved_files(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    inst.install(str(src), yes=True)
    root = packs_env.root / "io.github.example" / "demo" / "1.0.0"
    (root / "guard" / "rules.yaml").write_text("rules: []\n")
    assert not inst.audit()["ok"]
    assert inst.install(str(src), yes=True)["status"] == "repaired"
    assert inst.audit()["ok"]
    assert inst.install(str(src), yes=True)["status"] == "unchanged"


def test_a_crash_mid_write_leaves_the_old_index(packs_env, tmp_path, monkeypatch):
    inst.install(str(make_pack(tmp_path / "a")), yes=True)
    before = store.index_path().read_text()

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.write_index({"schema": 1, "packs": {}})
    assert store.index_path().read_text() == before
    assert [p.name for p in store.index_path().parent.iterdir() if p.name.endswith(".tmp")] == []


# ── authoring ────────────────────────────────────────────────────────────────

def test_scaffold_is_a_valid_pack(tmp_path):
    root = inst.new_pack("my-pack", tmp_path / "my-pack", namespace="io.github.acme")
    r = inst.validate_dir(root)
    assert r["ok"], r["problems"]
    assert r["provides"] == {"policies": 1, "guard_rules": 1, "skills": 1}
    with pytest.raises(PackError):
        inst.new_pack("my-pack", tmp_path / "my-pack")
    with pytest.raises(PackError):
        inst.new_pack("Bad Name", tmp_path / "x")


def test_the_example_pack_validates_and_does_what_it_says(packs_env, tmp_path, first_party_key):
    r = inst.validate_dir(EXAMPLE_PACK)
    assert r["ok"], r["problems"]
    assert r["id"] == "io.github.getnable/startup-credits-runway" and r["tier"] == "first-party"
    assert r["provides"] == {"policies": 1, "guard_rules": 1, "reports": 1, "skills": 1}
    # Unsigned, the first-party claim is a warning here and a refusal at install.
    assert any("install will refuse it" in w for w in r["warnings"])
    signed = sign_pack(copy_pack(EXAMPLE_PACK, tmp_path / "fp"), first_party_key)
    assert signed["digest"] == r["digest"]
    inst.install(str(tmp_path / "fp"), yes=True)
    (rule,) = packs.active("policies")
    hit = rule.evaluate({"type": "credits_runway",
                         "credits": {"balance_usd": 40000, "runway_months": 4},
                         "spend": {"growth_pct": 25, "monthly_usd": 10000,
                                   "projected_12m_usd": 120000}})
    assert hit and hit["action"] == "escalate" and "$40000" in hit["message"]
    assert rule.evaluate({"type": "credits_runway", "credits": {"balance_usd": 900000,
                                                                "runway_months": 30},
                          "spend": {"growth_pct": 25, "projected_12m_usd": 120000}}) is None
    (g,) = packs.guard_rules()
    assert g.pack == "io.github.getnable/startup-credits-runway"
    assert g.matches_command("aws ec2 run-instances --image-id ami-1 --instance-type p4d.24xlarge")
    assert g.matches_command("gcloud compute instances create x --accelerator type=nvidia-l4")
    assert not g.matches_command("aws ec2 run-instances --instance-type m5.large")
    (report,) = packs.active("reports")
    out = report.render({"period": "September", "credits": {"balance_usd": 40000,
                                                           "runway_months": 4}})
    assert "Credits left: $40000" in out and "4 months" in out
    (skill,) = packs.active("skills")
    assert skill.name == "credits-runway"


# ── the consumers wired end to end ───────────────────────────────────────────

def test_price_override_reads_installed_price_books(packs_env, tmp_path):
    assert packs.price_override("aws", "p4d.24xlarge") is None
    inst.install(str(make_pack(tmp_path / "src")), yes=True)
    r = packs.price_override("AWS", "P4D.24xlarge", on=date(2026, 3, 1))
    assert r["rate"] == 21.5 and r["unit"] == "hour" and r["pack"] == "io.github.example/demo"
    assert packs.price_override("aws", "p4d.24xlarge", on=date(2025, 12, 31)) is None
    assert packs.price_override("aws", "m5.large") is None


def test_active_rejects_unknown_kinds(packs_env):
    with pytest.raises(ValueError):
        packs.active("scripts")


def test_list_installed_packs_mcp_tool(packs_env, tmp_path):
    from finops import server
    from finops.tool_surface import _FAMILY_OF, tool_annotation
    assert _FAMILY_OF["list_installed_packs"] == "core"
    assert tool_annotation("list_installed_packs")["readOnlyHint"] is True
    empty = asyncio.run(server.mcp._tool_manager.call_tool("list_installed_packs", {}))
    assert empty["count"] == 0
    inst.install(str(make_pack(tmp_path / "src")), yes=True)
    out = asyncio.run(server.mcp._tool_manager.call_tool("list_installed_packs", {}))
    assert out["count"] == 1
    row = out["packs"][0]
    assert row["id"] == "io.github.example/demo" and row["loaded"] is True
    assert row["capabilities"] == {"read_data": ["focus.cost"]}
    assert "files" not in row and out["api_version"] == "1.0"


def test_an_archived_pack_is_not_installed(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    m = src / "nable-pack.toml"
    m.write_text(m.read_text().replace('support = "community"',
                                       'support = "community"\nstatus = "archived"'))
    with pytest.raises(PackError, match="archived"):
        inst.install(str(src), yes=True)
