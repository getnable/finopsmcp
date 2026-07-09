"""
Tests for the control-plane login primitive (auth/control_plane.py).

The dashboard lets getnable.com sign a short-lived, single-use, per-instance
token that mints a session. These pin the token format and the security
properties: signature, expiry, instance binding, role, single-use, and the
off-by-default gating.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import shutil
import socket
import subprocess
import textwrap
import threading
import time

import pytest

import finops.auth.control_plane as cp

SECRET = "test-instance-secret-abc123"
INSTANCE = "inst_acme"


def _raw_token(secret: str, payload: dict) -> str:
    """Assemble a token from an arbitrary payload, for edge cases mint_token will
    not produce (missing fields, bad role). The signature is always valid for the
    payload, so the rejection comes from the field check, not a bad signature."""
    payload_b64 = cp._b64url_encode(json.dumps(payload).encode("utf-8"))
    return f"{payload_b64}.{cp._sign(secret, payload_b64)}"


def test_valid_token_verifies():
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE,
                      role="analyst", now=1000)
    out = cp.verify_token(SECRET, t, INSTANCE, now=1000)
    assert out is not None
    assert out["email"] == "a@acme.com"
    assert out["role"] == "analyst"
    assert out["instance_id"] == INSTANCE


def test_tampered_signature_rejected():
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="admin", now=1000)
    body, sig = t.rsplit(".", 1)
    assert cp.verify_token(SECRET, body + "." + ("0" * len(sig)), INSTANCE, now=1000) is None


def test_tampered_payload_rejected():
    # Swap the payload but keep the original signature: it no longer matches.
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="viewer", now=1000)
    _, sig = t.rsplit(".", 1)
    evil = cp._b64url_encode(json.dumps(
        {"email": "evil@x.com", "instance_id": INSTANCE, "role": "admin",
         "exp": 9999999999, "jti": "x"}).encode())
    assert cp.verify_token(SECRET, f"{evil}.{sig}", INSTANCE, now=1000) is None


def test_expired_token_rejected():
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="analyst",
                      ttl_seconds=60, now=1000)
    assert cp.verify_token(SECRET, t, INSTANCE, now=1000 + 61) is None


def test_wrong_instance_rejected():
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="analyst", now=1000)
    assert cp.verify_token(SECRET, t, "inst_other", now=1000) is None


def test_wrong_secret_rejected():
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="analyst", now=1000)
    assert cp.verify_token("different-secret", t, INSTANCE, now=1000) is None


def test_invalid_role_rejected():
    t = _raw_token(SECRET, {"email": "a@acme.com", "instance_id": INSTANCE,
                            "role": "superuser", "exp": 9999999999, "jti": "j-role"})
    assert cp.verify_token(SECRET, t, INSTANCE, now=1000) is None


def test_missing_email_rejected():
    t = _raw_token(SECRET, {"instance_id": INSTANCE, "role": "admin",
                            "exp": 9999999999, "jti": "j-email"})
    assert cp.verify_token(SECRET, t, INSTANCE, now=1000) is None


def test_missing_jti_rejected():
    t = _raw_token(SECRET, {"email": "a@acme.com", "instance_id": INSTANCE,
                            "role": "admin", "exp": 9999999999})
    assert cp.verify_token(SECRET, t, INSTANCE, now=1000) is None


def test_replay_rejected():
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="admin",
                      jti="unique-jti-replay", ttl_seconds=300, now=1000)
    assert cp.verify_token(SECRET, t, INSTANCE, now=1000) is not None
    # The same token again: the single-use guard rejects it.
    assert cp.verify_token(SECRET, t, INSTANCE, now=1000) is None


def test_malformed_or_empty_token_rejected():
    assert cp.verify_token(SECRET, "no-dot-here", INSTANCE, now=1000) is None
    assert cp.verify_token(SECRET, "", INSTANCE, now=1000) is None


def test_empty_secret_or_instance_rejected():
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="admin", now=1000)
    assert cp.verify_token("", t, INSTANCE, now=1000) is None
    assert cp.verify_token(SECRET, t, "", now=1000) is None


def test_viewer_role_round_trips():
    t = cp.mint_token(SECRET, email="v@acme.com", instance_id=INSTANCE, role="viewer", now=1000)
    out = cp.verify_token(SECRET, t, INSTANCE, now=1000)
    assert out is not None and out["role"] == "viewer"


def test_is_enabled_requires_both(monkeypatch):
    monkeypatch.delenv("FINOPS_CONTROL_PLANE_SECRET", raising=False)
    monkeypatch.delenv("FINOPS_INSTANCE_ID", raising=False)
    assert cp.is_enabled() is False
    monkeypatch.setenv("FINOPS_CONTROL_PLANE_SECRET", "s")
    assert cp.is_enabled() is False  # instance id still missing
    monkeypatch.setenv("FINOPS_INSTANCE_ID", "i")
    assert cp.is_enabled() is True


def test_verify_request_token_uses_env(monkeypatch):
    monkeypatch.setenv("FINOPS_CONTROL_PLANE_SECRET", SECRET)
    monkeypatch.setenv("FINOPS_INSTANCE_ID", INSTANCE)
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="viewer",
                      jti="env-jti-ok", now=1000)
    out = cp.verify_request_token(t, now=1000)
    assert out is not None and out["role"] == "viewer"


def test_verify_request_token_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("FINOPS_CONTROL_PLANE_SECRET", raising=False)
    monkeypatch.delenv("FINOPS_INSTANCE_ID", raising=False)
    t = cp.mint_token(SECRET, email="a@acme.com", instance_id=INSTANCE, role="admin",
                      jti="env-jti-disabled", now=1000)
    assert cp.verify_request_token(t, now=1000) is None


# ── /auth/cp HTTP route (the dashboard endpoint that consumes the token) ──────

def _start_dashboard():
    """Start the dashboard server on a free port in a background thread."""
    from finops.server_web import _make_server

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = _make_server("127.0.0.1", port)
    threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True).start()
    time.sleep(0.1)
    return server, port


def _route_get(port: int, path: str):
    """GET without following redirects, so the 302 and Set-Cookie stay visible."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    out = (r.status, r.getheader("Location"), r.getheader("Set-Cookie"), r.read().decode())
    c.close()
    return out


@pytest.fixture()
def cp_dashboard():
    server, port = _start_dashboard()
    yield port
    server.shutdown()


def test_route_disabled_returns_404(cp_dashboard, monkeypatch):
    monkeypatch.delenv("FINOPS_CONTROL_PLANE_SECRET", raising=False)
    monkeypatch.delenv("FINOPS_INSTANCE_ID", raising=False)
    status, _loc, _cookie, _body = _route_get(cp_dashboard, "/auth/cp?token=anything")
    assert status == 404


def test_route_valid_admin_token_mints_full_session(cp_dashboard, monkeypatch):
    monkeypatch.setenv("FINOPS_CONTROL_PLANE_SECRET", "sekret")
    monkeypatch.setenv("FINOPS_INSTANCE_ID", "inst1")
    token = cp.mint_token("sekret", email="a@x.com", instance_id="inst1", role="admin")
    status, location, cookie, _body = _route_get(cp_dashboard, f"/auth/cp?token={token}")
    assert status == 302
    assert location == "/"
    assert cookie and cookie.startswith("nable_session=")


def test_route_viewer_token_mints_readonly_session(cp_dashboard, monkeypatch):
    monkeypatch.setenv("FINOPS_CONTROL_PLANE_SECRET", "sekret")
    monkeypatch.setenv("FINOPS_INSTANCE_ID", "inst1")
    token = cp.mint_token("sekret", email="v@x.com", instance_id="inst1", role="viewer")
    status, _loc, cookie, _body = _route_get(cp_dashboard, f"/auth/cp?token={token}")
    assert status == 302
    assert cookie and cookie.startswith("nable_view=")


def test_route_bad_token_shows_login_no_cookie(cp_dashboard, monkeypatch):
    monkeypatch.setenv("FINOPS_CONTROL_PLANE_SECRET", "sekret")
    monkeypatch.setenv("FINOPS_INSTANCE_ID", "inst1")
    _status, _loc, cookie, body = _route_get(cp_dashboard, "/auth/cp?token=garbage.deadbeef")
    assert cookie is None
    assert "invalid or expired" in body.lower()


# ── Control-plane mint interop (web/api/account/dashboard-login.js) ───────────
#
# The getnable.com endpoint mints these tokens in JavaScript (Vercel edge). This
# proves the JS mint path round-trips through the Python verifier: it runs the
# SAME crypto the endpoint uses (derive the per-instance secret from a master
# secret, then sign the payload) in Node, then verifies the token here. If the
# two ever drift, this fails. Skips cleanly when Node is not on the runner.

# The mint logic copied verbatim from dashboard-login.js (hmacHex, b64url, the
# nable-instance derivation, and the {email,instance_id,role,exp,jti} payload).
_NODE_MINT = textwrap.dedent(
    """
    import { webcrypto as crypto } from "node:crypto";

    async function hmacHex(secret, message) {
      const enc = new TextEncoder();
      const key = await crypto.subtle.importKey(
        "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
      );
      const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
      return Array.from(new Uint8Array(sig))
        .map((b) => b.toString(16).padStart(2, "0")).join("");
    }
    function b64url(str) {
      const bytes = new TextEncoder().encode(str);
      let binary = "";
      bytes.forEach((b) => (binary += String.fromCharCode(b)));
      return btoa(binary).replace(/\\+/g, "-").replace(/\\//g, "_").replace(/=+$/, "");
    }
    function freshJti() {
      const raw = new Uint8Array(16);
      crypto.getRandomValues(raw);
      let binary = "";
      raw.forEach((b) => (binary += String.fromCharCode(b)));
      return btoa(binary).replace(/\\+/g, "-").replace(/\\//g, "_").replace(/=+$/, "");
    }
    async function deriveInstanceSecret(master, instanceId) {
      return hmacHex(master, "nable-instance:" + instanceId);
    }
    async function mintControlPlaneToken(secret, { email, instanceId, role }) {
      const payload = {
        email, instance_id: instanceId, role,
        exp: Math.floor(Date.now() / 1000) + 60, jti: freshJti(),
      };
      const payloadB64 = b64url(JSON.stringify(payload));
      const sig = await hmacHex(secret, payloadB64);
      return `${payloadB64}.${sig}`;
    }

    // With `node --input-type=module -e`, user args start at argv[1] (there is no
    // script-name slot), so slice(1), not slice(2).
    const [master, instanceId, email, role] = process.argv.slice(1);
    const secret = await deriveInstanceSecret(master, instanceId);
    const token = await mintControlPlaneToken(secret, { email, instanceId, role });
    process.stdout.write(JSON.stringify({ derived_secret: secret, token }));
    """
)


def _node_mint(master: str, instance_id: str, email: str, role: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available; skipping JS interop")
    proc = subprocess.run(
        [node, "--input-type=module", "-e", _NODE_MINT,
         master, instance_id, email, role],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node mint failed: {proc.stderr}"
    return json.loads(proc.stdout)


def test_js_minted_token_verifies_in_python():
    """The exact JS mint path produces a token Python accepts. This is the gate:
    if the JS and Python token formats ever diverge, this fails."""
    master = "test-master-secret-not-real"
    instance_id = "inst_interop"
    out = _node_mint(master, instance_id, "owner@acme.com", "admin")

    payload = cp.verify_token(out["derived_secret"], out["token"], instance_id)
    assert payload is not None, "JS-minted token did not verify in Python"
    assert payload["email"] == "owner@acme.com"
    assert payload["role"] == "admin"
    assert payload["instance_id"] == instance_id


def test_js_derivation_matches_python():
    """The per-instance secret the JS derives equals the one Python derives from
    the same master, so provisioning can set the JS-derived hex on the instance."""
    master = "test-master-secret-not-real"
    instance_id = "inst_interop"
    out = _node_mint(master, instance_id, "owner@acme.com", "viewer")

    expected = hmac.new(
        master.encode("utf-8"),
        ("nable-instance:" + instance_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert out["derived_secret"] == expected


def test_js_token_rejected_for_wrong_instance():
    """A token minted for one instance cannot open another (no shared key)."""
    master = "test-master-secret-not-real"
    out = _node_mint(master, "inst_a", "owner@acme.com", "admin")
    assert cp.verify_token(out["derived_secret"], out["token"], "inst_b") is None


# ── Managed instance never serves demo data ──────────────────────────────────

def test_managed_instance_forces_demo_off(monkeypatch):
    import finops.demo_data as dd
    monkeypatch.setattr(dd, "DEMO_MODE", True)  # pretend FINOPS_DEMO was on
    monkeypatch.setenv("FINOPS_CONTROL_PLANE_SECRET", "s")
    monkeypatch.setenv("FINOPS_INSTANCE_ID", "i")
    assert dd.is_demo() is False


def test_demo_mode_honored_when_not_managed(monkeypatch):
    import finops.demo_data as dd
    monkeypatch.setattr(dd, "DEMO_MODE", True)
    monkeypatch.setattr(dd, "_real_provider_connected", lambda: False)  # nothing connected
    monkeypatch.delenv("FINOPS_CONTROL_PLANE_SECRET", raising=False)
    monkeypatch.delenv("FINOPS_INSTANCE_ID", raising=False)
    assert dd.is_demo() is True


def test_demo_yields_when_a_real_provider_is_connected(monkeypatch):
    # The bug: demo data kept showing after the user connected a real account.
    # Demo must step aside the moment a provider is connected, so they see their
    # real numbers, not the canned dataset.
    import finops.demo_data as dd
    monkeypatch.setattr(dd, "DEMO_MODE", True)
    monkeypatch.delenv("FINOPS_CONTROL_PLANE_SECRET", raising=False)
    monkeypatch.delenv("FINOPS_INSTANCE_ID", raising=False)
    monkeypatch.delenv("FINOPS_DEMO_FORCE", raising=False)
    monkeypatch.setattr(dd, "_real_provider_connected", lambda: True)
    assert dd.is_demo() is False


def test_demo_force_overrides_a_connected_provider(monkeypatch):
    # FINOPS_DEMO_FORCE keeps demo on even with a real account connected, for
    # recording a demo on a machine that has real credentials.
    import finops.demo_data as dd
    monkeypatch.setattr(dd, "DEMO_MODE", True)
    monkeypatch.delenv("FINOPS_CONTROL_PLANE_SECRET", raising=False)
    monkeypatch.delenv("FINOPS_INSTANCE_ID", raising=False)
    monkeypatch.setattr(dd, "_real_provider_connected", lambda: True)
    monkeypatch.setenv("FINOPS_DEMO_FORCE", "1")
    assert dd.is_demo() is True


def test_real_provider_detected_from_env_credential(monkeypatch):
    # The connect-then-restart path: a provider credential lands in os.environ at
    # startup, so a cred in the env is enough to flip demo off.
    import finops.demo_data as dd
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setattr(dd, "_real_provider_cache", None)  # bust the short cache
    assert dd._real_provider_connected() is True
