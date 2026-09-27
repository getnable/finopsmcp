# SPDX-License-Identifier: Apache-2.0
"""Pack signatures: a detached Ed25519 signature over the pack's content digest.

What is signed is the digest `nable pack validate` prints (store.content_digest:
one sha256 over every file's path and sha256, the signature file left out), so
one signature holds whether the pack travels as a directory, a git checkout or
a tarball. The signature lives in `nable-pack.sig`, inside the pack or next to
a tarball (`<tarball>.sig`, or a `nable-pack.sig` beside it whose digest
matches):

    {"format": 1, "algorithm": "ed25519", "digest": "<64 hex>",
     "key_id": "ed25519:<16 hex>", "signature": "<base64>",
     "signed_at": "2026-09-27T12:00:00+00:00"}

The signed bytes are b"nable-pack-signature/v1\\n" + the digest, so a pack
signature can never be replayed as anything else a key signs.

Trust roots, and nothing else:

  - nable's first-party key, FIRST_PARTY_PUBLIC_KEY_B64 below. It is a
    PLACEHOLDER until the owner fills it in; while it is, no pack verifies as
    first-party (fail closed), and a first-party or verified claim is refused.
  - the org's keys, `packs.trusted_keys` in the org policy file, each a name and
    a base64 Ed25519 public key (`nable pack keygen` makes one).

A key_id is "ed25519:" and the first 16 hex characters of the sha256 of the raw
public key. It names the key; trust comes only from the lists above.

What a verdict means where:

  - tier "first-party" or "verified" is honoured only with a signature that
    verifies with the first-party key; otherwise the pack is refused.
  - packs.require_signed accepts a pack signed by the first-party key or an
    org-trusted key.
  - an invalid signature (the files changed after signing, or the signature
    does not verify) is refused everywhere, since it means tampering.
  - code (connectors, adapters, sinks) runs only from a pack signed by a
    trusted key, or one the org allowlists in packs.allow_unsigned_code.

Not verified yet: the manifest's [integrity].attestation (a PEP 740 / Sigstore
bundle reference). It is parsed and shown, and nothing relies on it.

Publishers and orgs sign with `nable pack sign <dir|tarball> --key <pem>`. The
private key never leaves the file it is read from: it is not logged, printed
or copied, and an error loading it names the file, not its content.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import PackError
from .store import SIG_NAME

SIG_FORMAT = 1
ALGORITHM = "ed25519"
MAX_SIG_BYTES = 4096
_DOMAIN = b"nable-pack-signature/v1\n"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

FIRST_PARTY_KEY_NAME = "nable first-party"
FIRST_PARTY_PLACEHOLDER = "UNSET-nable-first-party-ed25519-public-key"
# TODO(owner): replace with the base64 of nable's first-party Ed25519 public key
# (32 raw bytes). Generate the pair offline (`nable pack keygen --out ...`), keep
# the private half offline, publish the public half, and paste it here. Until
# then this is a placeholder and first-party verification fails closed: no pack
# can claim first-party or verified. Tests monkeypatch it with a throwaway key.
FIRST_PARTY_PUBLIC_KEY_B64 = FIRST_PARTY_PLACEHOLDER

# The tiers whose claim needs the first-party key's signature. Verified packs
# are published by others and countersigned by nable after review.
KEYED_TIERS: tuple[str, ...] = ("first-party", "verified")
# Env var the CLI reads a private key's passphrase from, when the PEM is
# encrypted and there is no terminal to ask at. The name, not a secret.
PASSPHRASE_ENV = "NABLE_PACK_KEY_PASSPHRASE"  # nosec B105


class SignatureError(PackError):
    """A signature file or a signing key could not be read or used."""


def b64decode(text: str) -> bytes:
    """Standard or URL-safe base64, padded or not. Raises ValueError."""
    raw = text.strip()
    try:
        return base64.b64decode(raw.replace("-", "+").replace("_", "/") + "=" * (-len(raw) % 4),
                                validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("is not base64") from None


def key_id(raw: bytes) -> str:
    return "ed25519:" + hashlib.sha256(raw).hexdigest()[:16]


@dataclass(frozen=True)
class TrustedKey:
    name: str
    trust: str          # "first-party" | "org"
    raw: bytes

    @property
    def key_id(self) -> str:
        return key_id(self.raw)


def first_party_configured() -> bool:
    try:
        return len(b64decode(FIRST_PARTY_PUBLIC_KEY_B64)) == 32
    except ValueError:
        return False


def first_party_keys() -> list[TrustedKey]:
    """nable's own key, or nothing while it is the placeholder."""
    if not first_party_configured():
        return []
    return [TrustedKey(FIRST_PARTY_KEY_NAME, "first-party", b64decode(FIRST_PARTY_PUBLIC_KEY_B64))]


def org_keys(pp: dict[str, Any] | None) -> list[TrustedKey]:
    out = []
    for k in (pp or {}).get("trusted_keys") or []:
        try:
            raw = b64decode(str(k.get("key", "")))
        except ValueError:
            continue
        if len(raw) == 32:
            out.append(TrustedKey(str(k.get("name") or "org key"), "org", raw))
    return out


def trusted_keys(pp: dict[str, Any] | None = None) -> list[TrustedKey]:
    """Every key a signature may verify against: first-party, then the org's."""
    if pp is None:
        from ..policy import pack_policy
        pp = pack_policy()
    return first_party_keys() + org_keys(pp)


@dataclass(frozen=True)
class Verdict:
    """What a pack's signature says.

    status  verified   the signature verifies with a trusted key
            unsigned   there is no signature
            untrusted  a well-formed signature by a key nobody here trusts
            invalid    the files changed after signing, the signature does not
                       verify, or the signature file is malformed
    """

    status: str
    reason: str
    key_id: str | None = None
    key_name: str | None = None
    trust: str | None = None
    signed_at: str | None = None

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in {"status": self.status, "reason": self.reason,
                                  "key_id": self.key_id, "key_name": self.key_name,
                                  "trust": self.trust, "signed_at": self.signed_at}.items()
                if v is not None}


UNSIGNED = Verdict("unsigned", f"the pack has no {SIG_NAME}")


def message(digest: str) -> bytes:
    return _DOMAIN + digest.encode("ascii")


def parse_sig(data: bytes | str) -> dict[str, Any]:
    """A signature document, checked for shape. Raises ValueError saying why."""
    if isinstance(data, bytes):
        if len(data) > MAX_SIG_BYTES:
            raise ValueError(f"is larger than {MAX_SIG_BYTES} bytes")
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("is not UTF-8 text") from None
    try:
        doc = json.loads(data)
    except ValueError:
        raise ValueError("is not JSON") from None
    if not isinstance(doc, dict):
        raise ValueError("is not a JSON object")  # noqa: TRY004 - a malformed file, like bad JSON
    if doc.get("format") != SIG_FORMAT:
        raise ValueError(f"format must be {SIG_FORMAT}")
    if doc.get("algorithm", ALGORITHM) != ALGORITHM:
        raise ValueError(f"algorithm must be {ALGORITHM}")
    if not isinstance(doc.get("digest"), str) or not _HEX64.match(doc["digest"]):
        raise ValueError("digest must be the pack's 64-hex content digest")
    if not isinstance(doc.get("key_id"), str) or not doc["key_id"].startswith("ed25519:"):
        raise ValueError("key_id must name an ed25519 key")
    try:
        sig = b64decode(str(doc.get("signature", "")))
    except ValueError:
        raise ValueError("signature is not base64") from None
    if len(sig) != 64:
        raise ValueError("signature is not a 64-byte Ed25519 signature")
    if doc.get("signed_at") is not None and not isinstance(doc["signed_at"], str):
        raise ValueError("signed_at must be a string")
    return doc


def verify_doc(doc: dict[str, Any], digest: str, keys: list[TrustedKey]) -> Verdict:
    """Check a parsed signature document against the files' digest."""
    kid, when = doc["key_id"], doc.get("signed_at")
    if doc["digest"] != digest:
        return Verdict("invalid", f"the signature is over digest {doc['digest'][:12]}..., but the "
                       f"files hash to {digest[:12]}...: the pack changed after it was signed",
                       key_id=kid, signed_at=when)
    matches = [k for k in keys if k.key_id == kid]
    if not matches:
        why = f"it is signed by {kid}, which is neither nable's first-party key"
        why += "" if first_party_configured() else (" (not set in this build: a placeholder, so "
                                                    "nothing verifies as first-party)")
        why += " nor a key in packs.trusted_keys"
        return Verdict("untrusted", why, key_id=kid, signed_at=when)
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    sig = b64decode(doc["signature"])
    for k in matches:
        try:
            Ed25519PublicKey.from_public_bytes(k.raw).verify(sig, message(digest))
        except InvalidSignature:
            continue
        return Verdict("verified", f"signed by {k.name} ({kid})", key_id=kid, key_name=k.name,
                       trust=k.trust, signed_at=when)
    return Verdict("invalid", f"the signature does not verify with {matches[0].name} ({kid})",
                   key_id=kid, key_name=matches[0].name, signed_at=when)


def read_sig_file(path: Path) -> dict[str, Any] | None:
    """The document at `path`, None when there is no file. Raises ValueError."""
    p = Path(path)
    if p.is_symlink():
        raise ValueError("is a symlink")
    try:
        if p.stat().st_size > MAX_SIG_BYTES:
            raise ValueError(f"is larger than {MAX_SIG_BYTES} bytes")
        data = p.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ValueError(f"could not be read ({e.strerror or e})") from None
    return parse_sig(data)


def verify_file(path: Path, digest: str, pp: dict[str, Any] | None = None) -> Verdict:
    """The verdict for the signature file at `path` (UNSIGNED when absent)."""
    try:
        doc = read_sig_file(path)
    except ValueError as e:
        return Verdict("invalid", f"{Path(path).name} {e}")
    if doc is None:
        return UNSIGNED
    return verify_doc(doc, digest, trusted_keys(pp))


def verify_pack(root: Path, digest: str, pp: dict[str, Any] | None = None) -> Verdict:
    """The verdict for `<root>/nable-pack.sig` against the pack's digest."""
    return verify_file(Path(root) / SIG_NAME, digest, pp)


def trusted(verdict: Verdict | dict[str, Any] | None) -> bool:
    """Signed by the first-party key or an org-trusted key, and it verifies."""
    v = verdict.to_dict() if isinstance(verdict, Verdict) else (verdict or {})
    return v.get("status") == "verified" and v.get("trust") in ("first-party", "org")


def claim_problem(pack_id: str, tier: str, verdict: Verdict | dict[str, Any] | None
                  ) -> str | None:
    """Why the signature does not back the pack, whatever the org policy says:
    an invalid signature, or a first-party / verified claim without the
    first-party key's signature. None when there is nothing wrong."""
    v = verdict.to_dict() if isinstance(verdict, Verdict) else (verdict or UNSIGNED.to_dict())
    status, reason = v.get("status", "unsigned"), v.get("reason", "")
    if status == "invalid":
        return f"{pack_id} has a signature that does not hold: {reason}"
    if tier in KEYED_TIERS and not (status == "verified" and v.get("trust") == "first-party"):
        base = (f"{pack_id} says it is {tier}, and that claim is honoured only when nable's "
                f"first-party key signed the pack ({SIG_NAME})")
        if not first_party_configured():
            base += ("; this build of nable has no first-party public key yet (a placeholder), "
                     "so no pack can verify as first-party")
        return f"{base}. Here: {reason}"
    return None


# ── the publishing side ───────────────────────────────────────────────────────

def load_private_key(path: str | Path, passphrase: bytes | None = None):
    """An Ed25519 private key from a PEM file. Raises SignatureError naming the
    file, never its content. An encrypted PEM needs `passphrase`."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    p = Path(path).expanduser()
    try:
        data = p.read_bytes()
    except OSError as e:
        raise SignatureError(f"the signing key {p} could not be read "
                             f"({e.strerror or type(e).__name__})") from None
    try:
        key = serialization.load_pem_private_key(data, password=passphrase)
    except TypeError:
        if passphrase is None:
            raise SignatureError(f"the signing key {p} is encrypted; give its passphrase "
                                 f"(at the prompt, or in {PASSPHRASE_ENV})") from None
        raise SignatureError(f"the signing key {p} could not be decrypted") from None
    except ValueError:
        raise SignatureError(f"{p} is not a PEM private key, or the passphrase is "
                             "wrong") from None
    finally:
        del data
    if not isinstance(key, Ed25519PrivateKey):
        raise SignatureError(f"{p} is not an Ed25519 key; make one with `nable pack keygen`")
    return key


def public_key_b64(private_key) -> str:
    from cryptography.hazmat.primitives import serialization
    raw = private_key.public_key().public_bytes(serialization.Encoding.Raw,
                                                serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def sign_digest(digest: str, private_key, *, now: datetime | None = None) -> dict[str, Any]:
    """The signature document for `digest`."""
    if not _HEX64.match(digest or ""):
        raise SignatureError("a pack digest is 64 lowercase hex characters")
    raw = b64decode(public_key_b64(private_key))
    return {"format": SIG_FORMAT, "algorithm": ALGORITHM, "digest": digest,
            "key_id": key_id(raw),
            "signature": base64.b64encode(private_key.sign(message(digest))).decode("ascii"),
            "signed_at": (now or datetime.now(UTC)).isoformat(timespec="seconds")}


def write_sig(path: Path, doc: dict[str, Any]) -> Path:
    p = Path(path)
    try:
        p.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as e:
        raise SignatureError(f"the signature could not be written to {p} "
                             f"({e.strerror or type(e).__name__})") from None
    return p


def keygen(out: str | Path, *, passphrase: bytes | None = None) -> dict[str, str]:
    """Write a new Ed25519 private key to `out` (PEM, mode 0600, never over an
    existing file) and its public half to `<out>.pub`. Returns the public key,
    its key id and both paths; the private key is not returned."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    p = Path(out).expanduser()
    pub_path = p.with_name(p.name + ".pub")
    for q in (p, pub_path):
        if q.exists() or q.is_symlink():
            raise SignatureError(f"{q} already exists; a key is never written over another")
    key = Ed25519PrivateKey.generate()
    enc = (serialization.BestAvailableEncryption(passphrase) if passphrase
           else serialization.NoEncryption())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)
    pub = public_key_b64(key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        pub_path.write_text(pub + "\n", encoding="utf-8")
    except OSError as e:
        raise SignatureError(f"the key could not be written to {p} "
                             f"({e.strerror or type(e).__name__})") from None
    return {"private_key_path": str(p), "public_key_path": str(pub_path), "public_key": pub,
            "key_id": key_id(b64decode(pub))}
