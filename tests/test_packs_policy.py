# SPDX-License-Identifier: Apache-2.0
"""The org's packs: policy in nable.policy.yaml, and that adding it changed
nothing about the keys the policy file already had."""
from __future__ import annotations

import pytest

from finops import packs
from finops.packs import install as inst
from finops.packs.errors import PolicyRefusal
from finops.policy import load_policy, pack_policy, policy_problems
from tests import packs_support
from tests.packs_support import (
    EXAMPLE_PACK,
    make_git_repo,
    make_pack,
)

# The shared fixture: an isolated packs root, policy file and registry setting.
packs_env = packs_support.packs_env


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


def test_require_signed_refuses_every_pack_that_is_not_first_party(packs_env, tmp_path):
    packs_env.policy("packs:\n  require_signed: true\n")
    msg = _refusal(make_pack(tmp_path / "src"))
    assert "community" in msg and "part 2" in msg


def test_require_signed_refuses_a_first_party_claim_from_a_local_copy(packs_env, tmp_path):
    packs_env.policy("packs:\n  require_signed: true\n")
    msg = _refusal(EXAMPLE_PACK)
    assert "says it is first-party" in msg and "github.com/getnable" in msg


def test_require_signed_refuses_yes_even_for_first_party(packs_env, tmp_path, monkeypatch):
    packs_env.policy("packs:\n  require_signed: true\n")
    url, commit = make_git_repo(tmp_path, EXAMPLE_PACK)
    # Pretend the local repo is nable's own, which is the only first-party
    # provenance part 1 can check.
    monkeypatch.setattr(inst, "FIRST_PARTY_GIT_PREFIXES", (url,))
    msg = _refusal(f"git+{url}@{commit}", yes=True)
    assert "--yes is refused" in msg
    r = inst.install(f"git+{url}@{commit}", approve=lambda plan: True)
    assert r["status"] == "installed"


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
