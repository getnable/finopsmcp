/**
 * GET /api/sso/oidc-callback?code=...&state=...
 *
 * OIDC authorization code callback. Exchanges the code for tokens,
 * validates the JWT, maps IdP groups → nable RBAC roles, and issues
 * a nable session token + license key.
 *
 * Required env vars:
 *   OIDC_ISSUER            -- must match oidc-start.js
 *   OIDC_CLIENT_ID         -- OAuth2 client ID
 *   OIDC_CLIENT_SECRET     -- OAuth2 client secret
 *   OIDC_REDIRECT_URI      -- must match oidc-start.js
 *   ACCOUNT_SECRET         -- for session tokens + state CSRF check
 *   FINOPS_LICENSE_PRIVATE_KEY -- Ed25519 seed (base64url) for license key generation
 *
 * Optional env vars:
 *   OIDC_GROUPS_CLAIM      -- JWT field that contains groups (default: "groups")
 *                            Okta: "groups", Azure AD: "roles", Google: custom
 *   OIDC_ROLE_MAP          -- JSON: {"group-name": "nable-role"}
 *                            e.g. '{"finops-admins":"admin","engineers":"analyst","finance":"viewer"}'
 *                            Users not in any mapped group get "viewer" (read-only).
 *   OIDC_DEFAULT_ROLE      -- fallback role when no groups match (default: "viewer")
 *   OIDC_PLAN              -- nable plan for SSO users (default: "pro")
 *                            Enterprise customers are always "pro".
 */

export const config = { runtime: "edge" };

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "https://getnable.com",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

// ── Crypto helpers ────────────────────────────────────────────────────────────

async function hmacHex(secret, message) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function b64url(str) {
  return btoa(str).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64UrlDecode(str) {
  str = str.replace(/-/g, "+").replace(/_/g, "/");
  while (str.length % 4) str += "=";
  return atob(str);
}

// ── JWKS-based JWT verification (RS256 / ES256) ───────────────────────────────

async function fetchJwks(issuer) {
  const discovery = await fetch(
    `${issuer.replace(/\/$/, "")}/.well-known/openid-configuration`
  );
  if (!discovery.ok) throw new Error(`Discovery failed: ${discovery.status}`);
  const meta = await discovery.json();
  if (!meta.jwks_uri || !meta.jwks_uri.startsWith("https://")) {
    throw new Error("OIDC jwks_uri must be HTTPS");
  }
  const jwksRes = await fetch(meta.jwks_uri);
  if (!jwksRes.ok) throw new Error(`JWKS fetch failed: ${jwksRes.status}`);
  return jwksRes.json();
}

function base64urlToArrayBuffer(b64) {
  const bin = base64UrlDecode(b64);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return buf.buffer;
}

async function importRsaKey(jwk) {
  return crypto.subtle.importKey(
    "jwk",
    jwk,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["verify"]
  );
}

async function importEcKey(jwk) {
  return crypto.subtle.importKey(
    "jwk",
    jwk,
    { name: "ECDSA", namedCurve: jwk.crv || "P-256" },
    false,
    ["verify"]
  );
}

async function verifyJwt(token, jwks, issuer, clientId) {
  const parts = token.split(".");
  if (parts.length !== 3) throw new Error("Malformed JWT");

  const [headerB64, payloadB64, sigB64] = parts;
  const header = JSON.parse(base64UrlDecode(headerB64));
  const payload = JSON.parse(base64UrlDecode(payloadB64));

  // Reject alg=none and only accept asymmetric signing algorithms.
  if (!header.alg || header.alg === "none") throw new Error("Unsigned JWT rejected");

  // Find the right key by kid. Only fall back to a lone key when the JWKS has
  // exactly one — never silently pick the first of several (key-confusion risk).
  const jwk =
    jwks.keys.find((k) => k.kid === header.kid) ||
    (jwks.keys.length === 1 ? jwks.keys[0] : null);
  if (!jwk) throw new Error("No matching JWK found");

  // Import the public key
  let pubKey;
  if (header.alg === "RS256" || jwk.kty === "RSA") {
    pubKey = await importRsaKey(jwk);
  } else if (header.alg === "ES256" || jwk.kty === "EC") {
    pubKey = await importEcKey(jwk);
  } else {
    throw new Error(`Unsupported alg: ${header.alg}`);
  }

  // Verify signature
  const enc = new TextEncoder();
  const signingInput = enc.encode(`${headerB64}.${payloadB64}`);
  const signature = base64urlToArrayBuffer(sigB64);

  let valid;
  if (jwk.kty === "RSA") {
    valid = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", pubKey, signature, signingInput);
  } else {
    valid = await crypto.subtle.verify(
      { name: "ECDSA", hash: "SHA-256" },
      pubKey,
      signature,
      signingInput
    );
  }
  if (!valid) throw new Error("JWT signature verification failed");

  // Verify expiry
  const now = Math.floor(Date.now() / 1000);
  if (payload.exp && payload.exp < now) throw new Error("JWT expired");
  if (payload.nbf && payload.nbf > now + 60) throw new Error("JWT not yet valid");

  // Validate issuer and audience. Without the audience check, a token your IdP
  // minted for a different OAuth client would be accepted here (cross-client
  // token replay → account takeover).
  if (issuer && (payload.iss || "").replace(/\/$/, "") !== issuer.replace(/\/$/, "")) {
    throw new Error("JWT issuer mismatch");
  }
  if (clientId) {
    const aud = payload.aud;
    const audOk = Array.isArray(aud) ? aud.includes(clientId) : aud === clientId;
    if (!audOk) throw new Error("JWT audience mismatch");
  }

  return payload;
}

// ── Group → RBAC role mapping ─────────────────────────────────────────────────

function mapGroupsToRole(groups, roleMapJson, defaultRole = "viewer") {
  if (!groups || !Array.isArray(groups) || groups.length === 0) return defaultRole;

  let roleMap = {};
  try {
    roleMap = JSON.parse(roleMapJson || "{}");
  } catch {
    console.warn("OIDC_ROLE_MAP is not valid JSON, using default role for all SSO users");
  }

  // Priority: admin > analyst > viewer
  const priority = { admin: 3, analyst: 2, viewer: 1 };
  let best = defaultRole;

  for (const group of groups) {
    const mapped = roleMap[group];
    if (mapped && (priority[mapped] || 0) > (priority[best] || 0)) {
      best = mapped;
    }
  }
  return best;
}

// ── License key generation (v2, Ed25519 — mirrors verify-code.js and license.py) ──
// Signs with FINOPS_LICENSE_PRIVATE_KEY (raw 32-byte seed). The MCP server
// verifies with the bundled public key, so no shared secret is needed anywhere.

const ED25519_PKCS8_PREFIX = Uint8Array.from([
  0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x04, 0x22, 0x04, 0x20,
]);

function b64urlToBytes(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function bytesToB64url(bytes) {
  let bin = "";
  bytes.forEach((b) => (bin += String.fromCharCode(b)));
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function generateLicenseKey(email, privateKeyB64, plan = "pro") {
  const d = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  const payload = b64url(JSON.stringify({ e: email, d, p: plan }));
  const seed = b64urlToBytes(privateKeyB64);
  const pkcs8 = new Uint8Array(ED25519_PKCS8_PREFIX.length + seed.length);
  pkcs8.set(ED25519_PKCS8_PREFIX);
  pkcs8.set(seed, ED25519_PKCS8_PREFIX.length);
  const key = await crypto.subtle.importKey("pkcs8", pkcs8, { name: "Ed25519" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("Ed25519", key, new TextEncoder().encode(`2:${payload}`));
  return `FINOPS-2-${payload}-${bytesToB64url(new Uint8Array(sig))}`;
}

// ── Session token ─────────────────────────────────────────────────────────────

async function createSessionToken(secret, email, plan, role) {
  const exp = Math.floor(Date.now() / 1000) + 86400; // 24h
  const payloadJson = JSON.stringify({ email, plan, role, exp, sso: true });
  const payload = b64url(payloadJson);
  const sig = await hmacHex(secret, payload);
  return `${payload}.${sig}`;
}

// ── State CSRF verification ───────────────────────────────────────────────────

async function verifyState(secret, state) {
  try {
    const [payloadB64, sig] = state.split(".");
    if (!payloadB64) return null;
    if (sig === "unsigned") return payloadB64; // dev mode without ACCOUNT_SECRET

    const expected = (await hmacHex(secret, payloadB64)).slice(0, 16);
    let diff = 0;
    for (let i = 0; i < expected.length; i++) {
      diff |= (sig.charCodeAt(i) || 0) ^ expected.charCodeAt(i);
    }
    if (diff !== 0) return null;

    // Check timestamp (state expires in 10 minutes)
    const decoded = atob(payloadB64);
    const parts = decoded.split(":");
    const timestamp = parseInt(parts[parts.length - 1], 10);
    if (Date.now() - timestamp > 10 * 60 * 1000) return null;

    return payloadB64;
  } catch {
    return null;
  }
}

// ── Handler ────────────────────────────────────────────────────────────────────

export default async function handler(req) {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS_HEADERS });
  }

  const url = new URL(req.url);
  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");
  const error = url.searchParams.get("error");

  // IdP returned an error (user denied, account not assigned, etc.)
  if (error) {
    const desc = url.searchParams.get("error_description") || error;
    return Response.redirect(
      `https://getnable.com/?sso_error=${encodeURIComponent(desc)}`,
      302
    );
  }

  if (!code) {
    return Response.redirect(
      "https://getnable.com/account?error=sso_not_configured",
      302
    );
  }

  const ISSUER = process.env.OIDC_ISSUER;
  const CLIENT_ID = process.env.OIDC_CLIENT_ID;
  const CLIENT_SECRET = process.env.OIDC_CLIENT_SECRET;
  const REDIRECT_URI = process.env.OIDC_REDIRECT_URI || "https://getnable.com/api/sso/oidc-callback";
  const ACCOUNT_SECRET = process.env.ACCOUNT_SECRET;
  const LICENSE_PRIVATE_KEY = process.env.FINOPS_LICENSE_PRIVATE_KEY;
  const GROUPS_CLAIM = process.env.OIDC_GROUPS_CLAIM || "groups";
  const ROLE_MAP = process.env.OIDC_ROLE_MAP || "{}";
  const DEFAULT_ROLE = process.env.OIDC_DEFAULT_ROLE || "viewer";
  const PLAN = process.env.OIDC_PLAN || "pro"; // Enterprise SSO users are always pro

  if (!ISSUER || !CLIENT_ID || !CLIENT_SECRET) {
    return Response.redirect(
      "https://getnable.com/account?error=sso_not_configured",
      302
    );
  }

  // ACCOUNT_SECRET is required when SSO is configured — it protects both CSRF state and session tokens
  if (!ACCOUNT_SECRET) {
    console.error("ACCOUNT_SECRET is not set — SSO is misconfigured");
    return Response.redirect("https://getnable.com/?sso_error=misconfigured", 302);
  }

  // Verify CSRF state. Required: a callback without state is a forged login.
  if (!state) {
    return Response.redirect("https://getnable.com/?sso_error=missing_state", 302);
  }
  {
    const valid = await verifyState(ACCOUNT_SECRET, state);
    if (!valid) {
      return Response.redirect("https://getnable.com/?sso_error=invalid_state", 302);
    }
  }

  // Exchange authorization code for tokens
  let idToken, accessToken;
  try {
    // Get token endpoint from discovery
    const discovery = await fetch(
      `${ISSUER.replace(/\/$/, "")}/.well-known/openid-configuration`
    );
    const meta = await discovery.json();

    // The token endpoint comes from the IdP discovery document. Require HTTPS so a
    // tampered discovery response can't exfiltrate the auth code + client secret
    // over cleartext (SSRF / downgrade).
    if (!meta.token_endpoint || !meta.token_endpoint.startsWith("https://")) {
      throw new Error("OIDC token_endpoint must be HTTPS");
    }

    const tokenRes = await fetch(meta.token_endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        code,
        redirect_uri: REDIRECT_URI,
        client_id: CLIENT_ID,
        client_secret: CLIENT_SECRET,
      }),
    });

    if (!tokenRes.ok) {
      const err = await tokenRes.text();
      console.error("Token exchange failed:", err);
      return Response.redirect("https://getnable.com/?sso_error=token_exchange_failed", 302);
    }

    const tokens = await tokenRes.json();
    idToken = tokens.id_token;
    accessToken = tokens.access_token;
  } catch (err) {
    console.error("Token exchange error:", err.message);
    return Response.redirect("https://getnable.com/?sso_error=idp_unreachable", 302);
  }

  // Validate the ID token JWT
  let claims;
  try {
    const jwks = await fetchJwks(ISSUER);
    claims = await verifyJwt(idToken, jwks, ISSUER, CLIENT_ID);
  } catch (err) {
    console.error("JWT validation error:", err.message);
    return Response.redirect("https://getnable.com/?sso_error=jwt_invalid", 302);
  }

  // Extract identity
  const email = claims.email || claims.preferred_username || claims.upn;
  if (!email) {
    return Response.redirect("https://getnable.com/?sso_error=no_email_claim", 302);
  }

  // Map IdP groups → nable RBAC role
  const groups = claims[GROUPS_CLAIM] || claims.roles || [];
  const role = mapGroupsToRole(groups, ROLE_MAP, DEFAULT_ROLE);

  console.log(`SSO login: ${email} | groups=${JSON.stringify(groups)} | role=${role}`);

  // Generate license key (SSO users always get pro)
  let license_key = null;
  if (LICENSE_PRIVATE_KEY) {
    try {
      license_key = await generateLicenseKey(email, LICENSE_PRIVATE_KEY, PLAN);
    } catch (err) {
      console.error("License key generation error:", err.message);
    }
  }

  // Create session token
  const token = ACCOUNT_SECRET
    ? await createSessionToken(ACCOUNT_SECRET, email, PLAN, role)
    : null;

  // Redirect back to the app with token in fragment (never in query string to avoid server logs)
  // The frontend reads the fragment and stores it in memory / sessionStorage.
  const returnTo = (() => {
    try {
      const decoded = atob(state.split(".")[0]);
      const parts = decoded.split(":");
      // returnTo is everything between tenant and timestamp
      return parts.slice(1, -1).join(":") || "/";
    } catch {
      return "/";
    }
  })();

  const fragment = new URLSearchParams({
    token: token || "",
    email,
    plan: PLAN,
    role,
    ...(license_key ? { license_key } : {}),
    sso: "1",
  }).toString();

  return Response.redirect(`https://getnable.com${returnTo}#${fragment}`, 302);
}
