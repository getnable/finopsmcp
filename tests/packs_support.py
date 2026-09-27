# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and builders for the tests/test_packs_*.py files.

Every test gets its own packs root, its own (absent) policy file and no
registry override, so nothing reads or writes the developer's data dir and
nothing reaches the network.
"""
from __future__ import annotations

import shutil
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
EXAMPLE_PACK = REPO / "examples" / "packs" / "startup-credits-runway"
EXAMPLE_CODE_PACK = REPO / "examples" / "packs" / "example-csv-connector"


def new_key(tmp: Path, name: str = "test-key") -> SimpleNamespace:
    """A throwaway Ed25519 key, generated for this test run and never
    committed: .private (the key object), .pem (a PEM file under `tmp`),
    .public (base64), .key_id, and .trusted (a packs.trusted_keys entry)."""
    from finops.packs import signing
    out = tmp / "keys" / f"{name}.pem"
    r = signing.keygen(out)
    key = signing.load_private_key(out)
    return SimpleNamespace(private=key, pem=out, public=r["public_key"], key_id=r["key_id"],
                           name=name,
                           trusted=f"    - name: {name}\n      key: {r['public_key']}\n")


def sign_pack(root: Path, key: SimpleNamespace) -> dict:
    """Sign a pack directory in place, as `nable pack sign` does."""
    from finops.packs import install as inst
    return inst.sign(root, key.pem)


def copy_pack(src: Path, dest: Path) -> Path:
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dest


@pytest.fixture
def first_party_key(tmp_path, monkeypatch):
    """Pretend nable's first-party public key is a throwaway test key (the
    real constant is a placeholder that makes first-party fail closed)."""
    from finops.packs import signing
    key = new_key(tmp_path, "nable-test-first-party")
    monkeypatch.setattr(signing, "FIRST_PARTY_PUBLIC_KEY_B64", key.public)
    return key

POLICY = """\
version: 1
rules:
  - id: big-increase
    description: A monthly increase over 1000 USD.
    applies_to: finding
    match:
      all:
        - {field: monthly_delta_usd, op: gt, value: 1000}
    effect:
      action: flag
      severity: medium
      message: "adds ${monthly_delta_usd}/mo"
"""

GUARD = """\
version: 1
rules:
  - id: ask-nat
    pattern: '\\baws\\s+ec2\\s+create-nat-gateway\\b'
    verdict: ask
    reason: NAT gateways bill hourly.
"""

PRICE_BOOK = """\
version: 1
rates:
  - provider: aws
    sku: p4d.24xlarge
    unit: hour
    rate: 21.5
    currency: USD
    effective_from: 2026-01-01
    note: EDP rate
"""

SKILL = """\
---
name: demo-skill
description: Use when testing packs.
---

Do the thing, then say what it cost.
"""


def manifest_text(*, name: str = "demo", namespace: str = "io.github.example",
                  version: str = "1.0.0", support: str = "community",
                  capabilities: str = 'read_data = ["focus.cost"]\npricing = ["override"]\n',
                  provides: str | None = None, extra: str = "") -> str:
    provides = provides if provides is not None else (
        'policies = ["policies/*.yaml"]\nguard_rules = ["guard/*.yaml"]\n'
        'price_books = ["prices/*.yaml"]\nskills = ["skills/demo-skill/SKILL.md"]\n')
    return (f'[pack]\nname = "{name}"\nnamespace = "{namespace}"\nversion = "{version}"\n'
            f'description = "A pack for tests"\nlicense = "Apache-2.0"\n'
            f'nable_api = ">=1.0,<2.0"\nmaintainers = ["@tester"]\nsupport = "{support}"\n\n'
            f"[capabilities]\n{capabilities}\n[provides]\n{provides}\n{extra}")


def make_pack(root: Path, **kw) -> Path:
    """A valid data pack at `root`; keyword args go to manifest_text()."""
    files = {
        "nable-pack.toml": manifest_text(**kw),
        "policies/rules.yaml": POLICY,
        "guard/rules.yaml": GUARD,
        "prices/book.yaml": PRICE_BOOK,
        "skills/demo-skill/SKILL.md": SKILL,
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def make_tarball(src: Path, out: Path, *, top: str = "demo") -> Path:
    with tarfile.open(out, "w:gz") as tf:
        tf.add(src, arcname=top)
    return out


def git(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                        "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args],
                       cwd=cwd, capture_output=True, text=True, check=True, timeout=60)
    return r.stdout.strip()


def make_git_repo(tmp: Path, pack_src: Path, *, subdir: str | None = None) -> tuple[str, str]:
    """A bare repo at tmp/remote.git holding `pack_src` (optionally under
    `subdir`). Returns (file:// URL, commit)."""
    work = tmp / "gitwork"
    work.mkdir()
    git("init", "-q", str(work))
    dest = work / subdir if subdir else work
    dest.mkdir(parents=True, exist_ok=True)
    for p in sorted(pack_src.rglob("*")):
        if p.is_file():
            t = dest / p.relative_to(pack_src)
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_bytes(p.read_bytes())
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "pack", cwd=work)
    commit = git("rev-parse", "HEAD", cwd=work)
    bare = tmp / "remote.git"
    git("clone", "-q", "--bare", str(work), str(bare))
    return f"file://{bare}", commit


@pytest.fixture
def packs_env(tmp_path, monkeypatch):
    """An isolated packs root and policy file. `env.policy(text)` writes the
    org policy; `env.root` is the packs root."""
    from finops import policy
    from finops.packs import runtime, store

    root = tmp_path / "data" / "packs"
    monkeypatch.setattr(store, "_root_override", root)
    pol = tmp_path / "nable.policy.yaml"
    monkeypatch.setenv("FINOPS_POLICY_FILE", str(pol))
    monkeypatch.delenv("NABLE_PACK_REGISTRY", raising=False)
    policy._FILE_CACHE.clear()
    runtime.invalidate()

    def write_policy(text: str) -> Path:
        pol.write_text(text, encoding="utf-8")
        policy._FILE_CACHE.clear()
        runtime.invalidate()
        return pol

    yield SimpleNamespace(tmp=tmp_path, root=root, policy_path=pol, policy=write_policy)
    policy._FILE_CACHE.clear()
    runtime.invalidate()
