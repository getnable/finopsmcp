# SPDX-License-Identifier: Apache-2.0
"""The registry index: resolution, search, pins, and a registry that cannot
be reached. Every registry here is a local file; nothing touches the network."""
from __future__ import annotations

import json

import pytest

from finops import packs
from finops.packs import install as inst
from finops.packs import registry, store
from finops.packs.errors import IntegrityError, RegistryError
from tests import packs_support
from tests.packs_support import (
    make_git_repo,
    make_pack,
)

# The shared fixture: an isolated packs root, policy file and registry setting.
packs_env = packs_support.packs_env


def _digest(root) -> str:
    return store.content_digest(store.hash_tree(root, skip_ignored=True))


def _registry(tmp_path, entries: list[dict]) -> str:
    path = tmp_path / "registry" / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": 1, "packs": entries}))
    return str(path)


def _entry(src, version="1.0.0", **kw) -> dict:
    e = {"namespace": "io.github.example", "name": "demo", "version": version,
         "source": str(src), "sha256": _digest(src), "tier": "community",
         "description": "Demo pack for tests"}
    e.update(kw)
    return e


def test_default_registry_url_and_precedence(packs_env, monkeypatch, tmp_path):
    assert packs.DEFAULT_REGISTRY_URL == \
        "https://raw.githubusercontent.com/getnable/registry/main/index.json"
    assert registry.location() == packs.DEFAULT_REGISTRY_URL
    monkeypatch.setenv("NABLE_PACK_REGISTRY", "/env/index.json")
    assert registry.location() == "/env/index.json"
    packs_env.policy("packs:\n  registry: /org/index.json\n")
    assert registry.location() == "/org/index.json"      # the org wins over the environment


def test_install_resolves_through_the_registry(packs_env, tmp_path, monkeypatch):
    v1 = make_pack(tmp_path / "v1")
    v2 = make_pack(tmp_path / "v2", version="1.2.0")
    monkeypatch.setenv("NABLE_PACK_REGISTRY",
                       _registry(tmp_path, [_entry(v1), _entry(v2, "1.2.0")]))
    r = inst.install("io.github.example/demo", yes=True)
    assert r["pack"]["version"] == "1.2.0"               # newest
    src = r["pack"]["source"]
    assert src["ref"] == "io.github.example/demo@1.2.0" and src["kind"] == "dir"
    inst.remove("io.github.example/demo")
    assert inst.install("io.github.example/demo@1.0.0", yes=True)["pack"]["version"] == "1.0.0"
    # update by id goes to the newest version in the registry
    assert inst.update("io.github.example/demo", yes=True)["pack"]["version"] == "1.2.0"
    assert inst.update("io.github.example/demo", yes=True)["status"] == "up-to-date"


def test_registry_git_entries_and_relative_paths(packs_env, tmp_path, monkeypatch):
    src = make_pack(tmp_path / "src")
    url, commit = make_git_repo(tmp_path, src)
    reg = tmp_path / "registry"
    (reg / "packs").mkdir(parents=True)
    make_pack(reg / "packs" / "bravo", name="bravo")
    entries = [_entry(src, source=f"git+{url}@{commit}"),
               {**_entry(reg / "packs" / "bravo", name="bravo"), "source": "packs/bravo"}]
    monkeypatch.setenv("NABLE_PACK_REGISTRY", _registry(tmp_path, entries))
    assert inst.install("io.github.example/demo", yes=True)["pack"]["source"]["commit"] == commit
    assert inst.install("io.github.example/bravo", yes=True)["status"] == "installed"


def test_the_registry_pin_is_enforced(packs_env, tmp_path, monkeypatch):
    src = make_pack(tmp_path / "src")
    monkeypatch.setenv("NABLE_PACK_REGISTRY", _registry(tmp_path, [_entry(src, sha256="0" * 64)]))
    with pytest.raises(IntegrityError, match="registry pins sha256"):
        inst.install("io.github.example/demo", yes=True)
    assert not store.index_path().exists()


def test_the_registry_cannot_disagree_with_the_manifest_about_tier(packs_env, tmp_path,
                                                                   monkeypatch):
    src = make_pack(tmp_path / "src")
    monkeypatch.setenv("NABLE_PACK_REGISTRY", _registry(tmp_path, [_entry(src, tier="verified")]))
    with pytest.raises(IntegrityError, match="lists it as verified"):
        inst.install("io.github.example/demo", yes=True)


def test_search_and_bad_entries(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    loc = _registry(tmp_path, [
        _entry(src), _entry(src, "1.1.0"),
        {"namespace": "bad", "name": "x", "version": "1.0.0"},
        _entry(src, name="loop", source="io.github.example/demo"),
        _entry(src, name="runway-thing", description="Credits RUNWAY"),
    ])
    hits = registry.search("runway", loc)
    assert [h.id for h in hits] == ["io.github.example/runway-thing"]
    assert [(h.id, h.version) for h in registry.search("", loc)] == [
        ("io.github.example/demo", "1.1.0"), ("io.github.example/runway-thing", "1.0.0")]
    _, skipped = registry.fetch_index(loc)
    assert len(skipped) == 2 and any("another registry entry" in s for s in skipped)
    with pytest.raises(RegistryError, match="not in the pack registry"):
        registry.resolve("io.github.example/missing", loc)


def test_an_unreachable_registry_is_a_clean_error(packs_env, tmp_path, monkeypatch):
    monkeypatch.setenv("NABLE_PACK_REGISTRY", str(tmp_path / "nowhere.json"))
    with pytest.raises(RegistryError) as ei:
        inst.install("io.github.example/demo", yes=True)
    msg = str(ei.value)
    assert "could not be read" in msg and "Nothing was installed" in msg
    assert "NABLE_PACK_REGISTRY" in msg


def test_an_https_registry_failure_is_the_same_clean_error(packs_env, monkeypatch):
    import httpx

    def refuse(self, *a, **k):
        raise httpx.ConnectError("no network in tests")
    monkeypatch.setattr(httpx.Client, "stream", refuse)
    with pytest.raises(RegistryError, match="could not be read .ConnectError."):
        registry.search("x", "https://registry.invalid/index.json")


def test_plain_http_and_garbage_registries_are_refused(packs_env, tmp_path):
    with pytest.raises(RegistryError, match="https"):
        registry.fetch_index("http://example.com/index.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(RegistryError, match="not valid JSON"):
        registry.fetch_index(str(bad))
    bad.write_text('{"packs": 3}')
    with pytest.raises(RegistryError, match="no packs list"):
        registry.fetch_index(str(bad))


def test_the_org_registry_wins_over_a_command_line_registry(packs_env, tmp_path):
    packs_env.policy(f"packs:\n  registry: {tmp_path / 'org.json'}\n")
    with pytest.raises(RegistryError, match="pins the pack registry"):
        registry.search("x", str(tmp_path / "other.json"))


def test_a_remote_registry_may_only_list_git_sources(packs_env, tmp_path, monkeypatch):
    import httpx
    src = make_pack(tmp_path / "src")
    doc = {"packs": [_entry(src, name="local-path"),
                     _entry(src, name="pinned", source="git+https://github.com/o/r@" + "a" * 40),
                     _entry(src, name="local-git", source="git+file:///tmp/r@" + "a" * 40)]}
    real = httpx.Client

    def client(*a, **k):
        k["transport"] = httpx.MockTransport(lambda req: httpx.Response(200, json=doc))
        return real(*a, **k)
    monkeypatch.setattr(httpx, "Client", client)
    entries, skipped = registry.fetch_index("https://registry.invalid/index.json")
    assert [e.name for e in entries] == ["pinned"]
    assert len(skipped) == 2
    assert all("only list pinned git+https:// or git+ssh:// sources" in s for s in skipped)
