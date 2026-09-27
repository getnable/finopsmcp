# SPDX-License-Identifier: Apache-2.0
"""Pack signatures: a detached Ed25519 signature over the content digest,
verified against nable's first-party key (a placeholder that fails closed)
and the org's packs.trusted_keys. Every key here is generated at test time."""
from __future__ import annotations

import base64
import json
import os
import stat

import pytest

from finops import packs
from finops.packs import install as inst
from finops.packs import signing, store
from finops.packs.errors import IntegrityError, PolicyRefusal
from finops.setup_wizard import main
from tests import packs_support
from tests.packs_support import (
    EXAMPLE_PACK,
    copy_pack,
    make_pack,
    make_tarball,
    manifest_text,
    new_key,
    sign_pack,
)

packs_env = packs_support.packs_env
first_party_key = packs_support.first_party_key


def _trust(packs_env, *keys, extra: str = "") -> None:
    packs_env.policy("packs:\n" + extra + "  trusted_keys:\n" + "".join(k.trusted for k in keys))


def _verdict(root) -> signing.Verdict:
    return signing.verify_pack(root, inst.validate_dir(root)["digest"])


# ── the signature itself ─────────────────────────────────────────────────────

def test_a_valid_signature_verifies_with_an_org_trusted_key(packs_env, tmp_path):
    key = new_key(tmp_path, "acme")
    src = make_pack(tmp_path / "src")
    r = sign_pack(src, key)
    doc = json.loads((src / store.SIG_NAME).read_text())
    assert set(doc) == {"format", "algorithm", "digest", "key_id", "signature", "signed_at"}
    assert doc["digest"] == r["digest"] == inst.validate_dir(src)["digest"]
    assert doc["key_id"] == key.key_id
    assert _verdict(src).status == "untrusted"          # nobody trusts acme yet
    _trust(packs_env, key)
    v = _verdict(src)
    assert v.verified and v.trust == "org" and v.key_name == "acme"


def test_the_digest_leaves_the_signature_out_so_signing_does_not_change_it(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    before = inst.validate_dir(src)["digest"]
    sign_pack(src, new_key(tmp_path))
    assert inst.validate_dir(src)["digest"] == before


def test_an_altered_signature_is_invalid(packs_env, tmp_path):
    key = new_key(tmp_path)
    src = make_pack(tmp_path / "src")
    sign_pack(src, key)
    _trust(packs_env, key)
    p = src / store.SIG_NAME
    doc = json.loads(p.read_text())
    raw = bytearray(base64.b64decode(doc["signature"]))
    raw[0] ^= 1
    doc["signature"] = base64.b64encode(bytes(raw)).decode()
    p.write_text(json.dumps(doc))
    v = _verdict(src)
    assert v.status == "invalid" and "does not verify" in v.reason
    with pytest.raises(IntegrityError) as ei:
        inst.install(str(src), yes=True)
    assert "does not hold" in str(ei.value)


def test_a_signature_by_the_wrong_key_is_untrusted_or_invalid(packs_env, tmp_path):
    trusted, other = new_key(tmp_path, "trusted"), new_key(tmp_path, "other")
    src = make_pack(tmp_path / "src")
    sign_pack(src, other)
    _trust(packs_env, trusted)
    v = _verdict(src)
    assert v.status == "untrusted" and other.key_id in v.reason
    # Claiming the trusted key's id does not help: the signature does not verify.
    p = src / store.SIG_NAME
    doc = json.loads(p.read_text())
    doc["key_id"] = trusted.key_id
    p.write_text(json.dumps(doc))
    assert _verdict(src).status == "invalid"


def test_a_file_changed_after_signing_is_refused_everywhere(packs_env, tmp_path):
    key = new_key(tmp_path)
    src = make_pack(tmp_path / "src")
    sign_pack(src, key)
    _trust(packs_env, key)
    (src / "policies" / "rules.yaml").write_text(
        (src / "policies" / "rules.yaml").read_text().replace("1000", "1"))
    v = _verdict(src)
    assert v.status == "invalid" and "changed after it was signed" in v.reason
    assert not inst.validate_dir(src)["ok"]
    # Refused even with no require_signed: a broken signature means tampering.
    with pytest.raises(IntegrityError):
        inst.install(str(src), yes=True)
    # Re-signing the edited digest with the same key is what a publisher does.
    sign_pack(src, key)
    assert inst.install(str(src), yes=True)["pack"]["signature"]["status"] == "verified"


def test_a_malformed_signature_file_is_invalid(packs_env, tmp_path):
    src = make_pack(tmp_path / "src")
    for text in ("not json", "[]", '{"format": 2}', json.dumps({
            "format": 1, "digest": "0" * 64, "key_id": "ed25519:x", "signature": "short"})):
        (src / store.SIG_NAME).write_text(text)
        assert _verdict(src).status == "invalid", text


def test_the_manifest_cannot_pin_the_signature_but_integrity_ignores_it(packs_env, tmp_path):
    key = new_key(tmp_path)
    src = make_pack(tmp_path / "src")
    files = {k: v for k, v in store.hash_tree(src).items() if k != "nable-pack.toml"}
    pins = "".join(f'"{k}" = "{v}"\n' for k, v in files.items())
    (src / "nable-pack.toml").write_text(manifest_text() + "\n[integrity.files]\n" + pins)
    sign_pack(src, key)
    _trust(packs_env, key)
    assert inst.install(str(src), yes=True)["status"] == "installed"
    bad = make_pack(tmp_path / "bad")
    (bad / "nable-pack.toml").write_text(
        manifest_text() + f'\n[integrity.files]\n"nable-pack.sig" = "{"0" * 64}"\n')
    assert any("cannot pin it" in p for p in inst.validate_dir(bad)["problems"])


# ── the first-party key ──────────────────────────────────────────────────────

def test_the_first_party_key_is_a_placeholder_that_fails_closed(packs_env, tmp_path):
    assert signing.FIRST_PARTY_PUBLIC_KEY_B64 == signing.FIRST_PARTY_PLACEHOLDER
    assert not signing.first_party_configured() and signing.first_party_keys() == []
    # Signed by some key, trusted by the org even: still not first-party.
    key = new_key(tmp_path)
    src = copy_pack(EXAMPLE_PACK, tmp_path / "fp")
    sign_pack(src, key)
    _trust(packs_env, key)
    with pytest.raises(IntegrityError) as ei:
        inst.install(str(src), yes=True)
    msg = str(ei.value)
    assert "says it is first-party" in msg and "no first-party public key yet" in msg


def test_first_party_and_verified_need_the_first_party_key(packs_env, tmp_path, first_party_key):
    org = new_key(tmp_path, "acme")
    _trust(packs_env, org)
    fp = copy_pack(EXAMPLE_PACK, tmp_path / "fp")
    sign_pack(fp, org)
    with pytest.raises(IntegrityError, match="honoured only when nable's first-party key"):
        inst.install(str(fp), yes=True)
    sign_pack(fp, first_party_key)
    r = inst.install(str(fp), yes=True)
    assert r["pack"]["signature"]["trust"] == "first-party"

    verified = make_pack(tmp_path / "v", name="verified-pack", support="verified")
    sign_pack(verified, org)
    with pytest.raises(IntegrityError, match="says it is verified"):
        inst.install(str(verified), yes=True)
    sign_pack(verified, first_party_key)
    assert inst.install(str(verified), yes=True)["pack"]["tier"] == "verified"


def test_an_unsigned_community_pack_installs_without_require_signed(packs_env, tmp_path):
    r = inst.install(str(make_pack(tmp_path / "src")), yes=True)
    assert r["pack"]["signature"]["status"] == "unsigned"


# ── tarballs, audit and the runtime ──────────────────────────────────────────

def test_a_tarball_signature_can_sit_beside_it(packs_env, tmp_path):
    key = new_key(tmp_path)
    _trust(packs_env, key, extra="  require_signed: true\n")
    src = make_pack(tmp_path / "src")
    tar = make_tarball(src, tmp_path / "demo-1.0.0.tar.gz")
    r = inst.sign(tar, key.pem)
    assert r["signature_path"].endswith("demo-1.0.0.tar.gz.sig")
    got = inst.install(str(tar), approve=lambda plan: True)
    assert got["pack"]["signature"]["key_name"] == key.name
    installed = store.install_dir("io.github.example", "demo", "1.0.0")
    assert (installed / store.SIG_NAME).is_file()        # kept with the pack, and audited
    assert inst.audit()["ok"]


def test_a_stray_signature_beside_a_tarball_is_ignored_unless_its_digest_matches(
        packs_env, tmp_path):
    key = new_key(tmp_path)
    _trust(packs_env, key)
    other = make_pack(tmp_path / "other", name="other")
    sign_pack(other, key)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / store.SIG_NAME).write_bytes((other / store.SIG_NAME).read_bytes())
    tar = make_tarball(make_pack(tmp_path / "src"), tmp_path / "dist" / "demo.tar.gz")
    assert inst.install(str(tar), yes=True)["pack"]["signature"]["status"] == "unsigned"


def test_removing_trust_in_a_key_stops_the_pack_loading(packs_env, tmp_path):
    key = new_key(tmp_path)
    src = make_pack(tmp_path / "src")
    sign_pack(src, key)
    _trust(packs_env, key, extra="  require_signed: true\n")
    inst.install(str(src), approve=lambda plan: True)
    assert packs.guard_rules()
    packs_env.policy("packs:\n  require_signed: true\n")
    assert packs.guard_rules() == []
    assert "not signed by nable's first-party key" in packs.load_problems()[0]
    row = inst.audit()["packs"][0]
    assert row["status"] == "outside-policy" and row["signature"]["status"] == "untrusted"


# ── the CLI: keygen and sign, and the key never shows ────────────────────────

def _cli(capsys, *argv):
    with pytest.raises(SystemExit) as ei:
        main(["pack", *argv])
    out = capsys.readouterr()
    return ei.value.code, out.out, out.err


def test_keygen_and_sign_from_the_cli(packs_env, tmp_path, capsys):
    pem = tmp_path / "keys" / "org.pem"
    code, out, _ = _cli(capsys, "keygen", "--out", str(pem), "--name", "acme")
    assert code == 0
    assert stat.S_IMODE(os.stat(pem).st_mode) == 0o600
    pub = (tmp_path / "keys" / "org.pem.pub").read_text().strip()
    assert f"key: {pub}" in out and "trusted_keys" in out
    body = pem.read_text()
    assert "PRIVATE KEY" in body
    secret_lines = [ln for ln in body.splitlines() if ln and "-----" not in ln]
    assert not any(ln in out for ln in secret_lines)
    code, _, err = _cli(capsys, "keygen", "--out", str(pem))
    assert code == 1 and "already exists" in err          # never over an existing key

    src = make_pack(tmp_path / "src")
    code, out, _ = _cli(capsys, "sign", str(src), "--key", str(pem), "--json")
    r = json.loads(out)
    assert code == 0 and r["digest"] == inst.validate_dir(src)["digest"]
    assert not any(ln in out for ln in secret_lines)
    packs_env.policy(f"packs:\n  trusted_keys:\n    - name: acme\n      key: {pub}\n")
    code, out, _ = _cli(capsys, "validate", str(src))
    assert code == 0 and "signature verified: signed by acme" in out


def test_sign_refuses_a_pack_that_does_not_validate_and_a_key_that_is_not_ed25519(
        packs_env, tmp_path, capsys):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = new_key(tmp_path)
    bad = make_pack(tmp_path / "bad")
    (bad / "policies" / "rules.yaml").write_text("rules: [{id: x}]\n")
    code, _, err = _cli(capsys, "sign", str(bad), "--key", str(key.pem))
    assert code == 1 and "does not validate" in err and not (bad / store.SIG_NAME).exists()
    ecpem = tmp_path / "ec.pem"
    ecpem.write_bytes(ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    code, _, err = _cli(capsys, "sign", str(make_pack(tmp_path / "ok")), "--key", str(ecpem))
    assert code == 1 and "not an Ed25519 key" in err


def test_an_encrypted_key_signs_with_its_passphrase(packs_env, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(signing.PASSPHRASE_ENV, "correct horse")
    pem = tmp_path / "enc.pem"
    code, _, _ = _cli(capsys, "keygen", "--out", str(pem), "--encrypt")
    assert code == 0 and b"ENCRYPTED" in pem.read_bytes()
    src = make_pack(tmp_path / "src")
    assert _cli(capsys, "sign", str(src), "--key", str(pem))[0] == 0
    monkeypatch.setenv(signing.PASSPHRASE_ENV, "wrong")
    code, _, err = _cli(capsys, "sign", str(src), "--key", str(pem))
    assert code == 1 and "passphrase" in err


def test_require_signed_still_refuses_yes_for_a_signed_pack(packs_env, tmp_path):
    key = new_key(tmp_path)
    src = make_pack(tmp_path / "src")
    sign_pack(src, key)
    _trust(packs_env, key, extra="  require_signed: true\n")
    with pytest.raises(PolicyRefusal, match="--yes is refused"):
        inst.install(str(src), yes=True)
