# SPDX-License-Identifier: Apache-2.0
"""The org's packs: policy in nable.policy.yaml, and that adding it changed
nothing about the keys the policy file already had."""
from __future__ import annotations

import pytest

from finops import packs
from finops.packs import install as inst
from finops.packs.errors import IntegrityError, PolicyRefusal
from finops.policy import load_policy, pack_policy, policy_problems
from tests import packs_support
from tests.packs_support import (
    EXAMPLE_PACK,
    copy_pack,
    make_git_repo,
    make_pack,
    new_key,
    sign_pack,
)

# The shared fixtures: an isolated packs root, policy file and registry
# setting; a throwaway key standing in for nable's first-party key.
packs_env = packs_support.packs_env
first_party_key = packs_support.first_party_key


def _refusal(src, **kw) -> str:
    with pytest.raises(PolicyRefusal) as ei:
        inst.install(str(src), yes=kw.pop("yes", True), **kw)
    return str(ei.value)


def test_no_policy_file_means_no_restrictions(packs_env):
    pp = pack_policy()
    assert pp["allowed_sources"] is None and pp["blocked_sources"] == []
    assert pp["require_signed"] is False and pp["allowed_capabilities"] is None
    assert pp["invalid"] is False


def test_the_policy_is_read_from_the_data_dir_file_never_the_working_dir(
        packs_env, tmp_path, monkeypatch):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "nable.policy.yaml").write_text("packs:\n  blocked_sources: ['*']\n")
    monkeypatch.chdir(cwd)
    assert pack_policy()["blocked_sources"] == []


def test_allowed_sources_is_an_allowlist_over_the_source(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    packs_env.policy(f"packs:\n  allowed_sources: ['{tmp_path}/approved/*']\n")
    assert "is not in packs.allowed_sources" in _refusal(src)
    ok = make_pack(tmp_path / "approved" / "demo")
    assert inst.install(str(ok), yes=True)["status"] == "installed"


def test_a_namespace_cannot_earn_a_place_on_the_allowlist(packs_env, tmp_path):
    packs_env.policy("packs:\n  allowed_sources: ['io.github.example/*']\n")
    assert "allowed_sources" in _refusal(make_pack(tmp_path / "src"))


def test_blocked_sources_match_the_source_or_the_pack_id(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    packs_env.policy("packs:\n  blocked_sources: ['io.github.example/*']\n")
    assert "blocked_sources" in _refusal(src)
    packs_env.policy(f"packs:\n  blocked_sources: ['{tmp_path}/*']\n")
    assert "blocked_sources" in _refusal(src)


def test_require_signed_refuses_an_unsigned_pack(packs_env, tmp_path):
    packs_env.policy("packs:\n  require_signed: true\n")
    msg = _refusal(make_pack(tmp_path / "src"))
    assert "not signed by nable's first-party key or a key in packs.trusted_keys" in msg
    assert "no nable-pack.sig" in msg


def test_an_unsigned_first_party_claim_is_refused_with_or_without_the_policy(packs_env):
    # The placeholder first-party key means nothing verifies as first-party:
    # a local copy of nable's own pack is refused, whatever the policy says.
    with pytest.raises(IntegrityError) as ei:
        inst.install(str(EXAMPLE_PACK), yes=True)
    msg = str(ei.value)
    assert "says it is first-party" in msg and "no first-party public key yet" in msg
    packs_env.policy("packs:\n  require_signed: true\n")
    with pytest.raises(IntegrityError):
        inst.install(str(EXAMPLE_PACK), yes=True)


def test_require_signed_accepts_first_party_and_refuses_yes_for_it(
        packs_env, tmp_path, first_party_key):
    packs_env.policy("packs:\n  require_signed: true\n")
    src = copy_pack(EXAMPLE_PACK, tmp_path / "fp")
    sign_pack(src, first_party_key)
    url, commit = make_git_repo(tmp_path, src)
    msg = _refusal(f"git+{url}@{commit}", yes=True)
    assert "--yes is refused" in msg
    r = inst.install(f"git+{url}@{commit}", approve=lambda plan: True)
    assert r["status"] == "installed"
    assert r["pack"]["signature"]["trust"] == "first-party"


def test_require_signed_accepts_a_pack_signed_by_an_org_trusted_key(packs_env, tmp_path):
    key = new_key(tmp_path, "acme")
    src = make_pack(tmp_path / "src")
    sign_pack(src, key)
    packs_env.policy("packs:\n  require_signed: true\n")
    msg = _refusal(src, yes=False, approve=lambda plan: True)
    assert "is signed by ed25519:" in msg and "nor a key in packs.trusted_keys" in msg
    packs_env.policy("packs:\n  require_signed: true\n  trusted_keys:\n" + key.trusted)
    r = inst.install(str(src), approve=lambda plan: True)
    assert r["pack"]["signature"]["key_name"] == "acme"
    assert r["pack"]["signature"]["trust"] == "org"


def test_trusted_keys_and_allow_unsigned_code_fail_closed_when_malformed(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    for text in ("packs:\n  trusted_keys: nope\n",
                 "packs:\n  trusted_keys:\n    - name: x\n      key: not-a-key\n",
                 "packs:\n  trusted_keys:\n    - key: AAAA\n",
                 "packs:\n  allow_unsigned_code: [not a pack id]\n",
                 "packs:\n  allow_unsigned_code: io.github.a/b\n"):
        packs_env.policy(text)
        assert pack_policy()["invalid"] is True, text
        assert "refused until it is fixed" in _refusal(src), text
    packs_env.policy("packs:\n  allow_unsigned_code: [io.github.a/b]\n  trusted_keys: []\n")
    pp = pack_policy()
    assert pp["invalid"] is False and pp["allow_unsigned_code"] == ["io.github.a/b"]


def test_allowed_capabilities_is_a_ceiling(packs_env, tmp_path):
    src = make_pack(tmp_path / "src", capabilities=(
        'read_data = ["focus.cost", "org.owners"]\nnetwork = ["api.example.com:443"]\n'))
    packs_env.policy("packs:\n  allowed_capabilities:\n    read_data: [focus.cost]\n")
    msg = _refusal(src)
    assert "'org.owners' is outside packs.allowed_capabilities.read_data" in msg
    assert "'api.example.com:443' is outside packs.allowed_capabilities.network" in msg
    packs_env.policy("packs:\n  allowed_capabilities:\n    read_data: [focus.cost, org.owners]\n"
                     "    network: ['*.example.com:443']\n")
    assert inst.install(str(src), yes=True)["status"] == "installed"


def test_a_broken_packs_section_fails_closed(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    for text in ("packs:\n  allowed_sources: 'not a list'\n",
                 "packs:\n  require_signed: maybe\n",
                 "packs:\n  alowed_sources: []\n",
                 "packs: [1, 2]\n",
                 "packs: {allowed_sources: [\n"):          # does not parse at all
        packs_env.policy(text)
        assert pack_policy()["invalid"] is True, text
        assert "refused until it is fixed" in _refusal(src), text


def test_policy_changes_after_install_stop_the_pack_loading(packs_env, tmp_path):
    inst.install(str(make_pack(tmp_path / "src")), yes=True)
    assert packs.guard_rules()
    packs_env.policy("packs:\n  blocked_sources: ['io.github.example/demo']\n")
    assert packs.guard_rules() == []
    assert "is not loaded" in packs.load_problems()[0]
    row = inst.audit()["packs"][0]
    assert row["status"] == "outside-policy"


def test_existing_policy_keys_behave_exactly_as_before(packs_env):
    packs_env.policy("on_budget_breach: deny\npacks:\n  require_signed: true\n")
    assert load_policy()["on_budget_breach"] == "deny"
    assert "packs" not in load_policy()
    assert policy_problems() == []
    packs_env.policy("on_budget_breach: maybe\n")
    assert load_policy()["on_budget_breach"] == "ask"
    assert pack_policy()["invalid"] is False


def test_pack_policy_problems_reach_doctor(packs_env):
    packs_env.policy("packs:\n  require_signed: 3\n")
    assert any("packs.require_signed" in p for p in policy_problems())
