/**
 * POST /api/account/dashboard-login
 *
 * Control-plane login for a customer's hosted nable dashboard. getnable.com acts
 * as the identity provider: it verifies the caller's account session, derives the
 * per-instance HMAC secret, mints a short-lived single-use token scoped to that
 * one instance, and hands back the /auth/cp redirect URL. The instance verifies
 * the token against its own copy of the same derived secret and mints a normal
 * dashboard session. No per-instance password or SSO setup is needed.
 *
 * The token format must match finops.auth.control_plane.verify_token exactly:
 *   token   = base64url(utf8(JSON.stringify(payload))) + "." + hmacHex(secret, payload_b64)
 *   payload = { email, instance_id, role, exp, jti }, exp = unix seconds, role in
 *             viewer | analyst | admin.
 *   secret  = hmacHex(CP_MASTER_SECRET, "nable-instance:" + instance_id)
 * The control plane holds ONE master secret and derives each instance's secret,
 * so a token minted for instance A can never open instance B. The instance's
 * FINOPS_CONTROL_PLANE_SECRET is set to that derived hex string at provisioning.
 *
 * The control plane never holds the instance's raw cloud credentials. This token
 * only grants dashboard access; the bills and keys stay on the instance.
 *
 * Required env vars:
 *   ACCOUNT_SECRET     -- session token HMAC (must match verify-code.js)
 *   STRIPE_SECRET_KEY  -- customer lookup by email
 *   CP_MASTER_SECRET   -- master secret each instance secret is derived from
 */

export const config = { runtime: "edge" };

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "https://getnable.com",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

const STRIPE_API = "https://api.stripe.com/v1";
const VALID_ROLES = new Set(["viewer", "analyst", "admin"]);

// ── Crypto + session helpers (copied from billing-portal.js / rotate-key.js) ────

async function hmacHex(secret, message) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function b64url(str) {
  const bytes = new TextEncoder().encode(str);
  let binary = "";
  bytes.forEach((b) => (binary += String.fromCharCode(b)));
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlDecode(str) {
  const padded = str.replace(/-/g, "+").replace(/_/g, "/");
  const pad = padded.length % 4;
  const bin = atob(pad ? padded + "=".repeat(4 - pad) : padded);
  // Decode UTF-8 to mirror the TextEncoder-based encoder (non-ASCII emails).
  return new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0)));
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function verifySessionToken(secret, token) {
  const dot = token.lastIndexOf(".");
  if (dot < 0) return null;
  const payload = token.slice(0, dot);
  const sig = token.slice(dot + 1);
  const expected = await hmacHex(secret, payload);
  if (!timingSafeEqual(sig, expected)) return null;
  let parsed;
  try {
    parsed = JSON.parse(b64urlDecode(payload));
  } catch {
    return null;
  }
  const now = Math.floor(Date.now() / 1000);
  if (!parsed.exp || parsed.exp < now) return null;
  return parsed; // { email, plan, exp }
}

function json(status, obj) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}

// ── Control-plane token mint (matches control_plane.py verify_token) ────────────

// The instance secret is derived, never stored: one master secret per control
// plane, one derived hex string per instance. Provisioning sets the derived
// string as FINOPS_CONTROL_PLANE_SECRET on the instance.
async function deriveInstanceSecret(masterSecret, instanceId) {
  return hmacHex(masterSecret, "nable-instance:" + instanceId);
}

// A fresh random jti as base64url of 16 bytes, so a captured token cannot be
// replayed inside its short window (the instance records the jti until it expires).
function freshJti() {
  const raw = new Uint8Array(16);
  crypto.getRandomValues(raw);
  let binary = "";
  raw.forEach((b) => (binary += String.fromCharCode(b)));
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function mintControlPlaneToken(instanceSecret, { email, instanceId, role }) {
  const payload = {
    email,
    instance_id: instanceId,
    role,
    exp: Math.floor(Date.now() / 1000) + 60, // short-lived: 60 seconds
    jti: freshJti(),
  };
  // Compact JSON (no spaces) so the bytes are stable; the instance re-derives the
  // signature from this exact base64 string, so it just has to be valid JSON.
  const payloadB64 = b64url(JSON.stringify(payload));
  const sig = await hmacHex(instanceSecret, payloadB64);
  return `${payloadB64}.${sig}`;
}

// ── Stripe ──────────────────────────────────────────────────────────────────────

async function findCustomerByEmail(email, stripeKey) {
  const res = await fetch(
    `${STRIPE_API}/customers?email=${encodeURIComponent(email)}&limit=1`,
    { headers: { Authorization: `Bearer ${stripeKey}` } }
  );
  if (!res.ok) {
    throw new Error(`customer lookup failed ${res.status}`);
  }
  const data = await res.json();
  return (data.data && data.data[0]) || null;
}

// ── Abuse + input guards ─────────────────────────────────────────────────────

// Per-email throttle so one valid session cannot hammer the Stripe lookup + mint.
// In-memory per edge instance: enough to blunt abuse, not a global limiter.
const _loginHits = new Map();
const LOGIN_WINDOW_MS = 60 * 1000;
const LOGIN_MAX = 20;
function loginRateLimited(email) {
  const now = Date.now();
  const hits = (_loginHits.get(email) || []).filter((t) => now - t < LOGIN_WINDOW_MS);
  hits.push(now);
  _loginHits.set(email, hits);
  return hits.length > LOGIN_MAX;
}

// instance_domain comes from provisioning-controlled Stripe metadata, but validate
// it is a bare hostname before building a redirect to it, so a malformed value can
// never send the short-lived token to an arbitrary host.
const HOSTNAME_RE =
  /^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$/;

// ── Handler ────────────────────────────────────────────────────────────────────

export default async function handler(req) {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS_HEADERS });
  }
  if (req.method !== "POST") return json(405, { error: "Method not allowed" });

  let body;
  try {
    body = await req.json();
  } catch {
    return json(400, { error: "Invalid JSON" });
  }

  const token = (body.token || "").trim();
  if (!token) return json(401, { error: "Missing session token" });

  const ACCOUNT_SECRET = process.env.ACCOUNT_SECRET;
  const STRIPE_KEY = process.env.STRIPE_SECRET_KEY;
  if (!ACCOUNT_SECRET || !STRIPE_KEY) {
    console.error("dashboard-login: missing ACCOUNT_SECRET or STRIPE_SECRET_KEY");
    return json(500, { error: "Service misconfigured" });
  }

  const session = await verifySessionToken(ACCOUNT_SECRET, token);
  if (!session) {
    return json(401, { error: "Invalid or expired session. Please sign in again." });
  }
  const email = session.email;

  if (loginRateLimited(email)) {
    return json(429, { error: "Too many requests. Try again in a minute." });
  }

  // Find the customer for this verified email and read the hosted-instance fields
  // off their metadata. Provisioning writes instance_domain + instance_id when a
  // managed instance is set up; dashboard_role is the role the token carries.
  let customer;
  try {
    customer = await findCustomerByEmail(email, STRIPE_KEY);
  } catch (err) {
    console.error("dashboard-login: customer lookup error", err.message);
    return json(502, { error: "Could not reach billing. Try again in a moment." });
  }

  const md = (customer && customer.metadata) || {};
  const instanceDomain = (md.instance_domain || "").trim();
  const instanceId = (md.instance_id || "").trim();

  // No managed instance provisioned yet: tell the UI so it can show a note, not
  // an error. A free or self-hosting customer lands here too.
  if (!instanceDomain || !instanceId) {
    return json(200, { ok: true, hosted: false });
  }
  if (!HOSTNAME_RE.test(instanceDomain)) {
    console.error("dashboard-login: invalid instance_domain in metadata");
    return json(500, { error: "Service misconfigured" });
  }

  let role = (md.dashboard_role || "analyst").trim();
  if (!VALID_ROLES.has(role)) role = "analyst";

  const CP_MASTER_SECRET = process.env.CP_MASTER_SECRET;
  if (!CP_MASTER_SECRET) {
    console.error("dashboard-login: missing CP_MASTER_SECRET; cannot mint token");
    return json(500, { error: "Service misconfigured" });
  }

  let cpToken;
  try {
    const instanceSecret = await deriveInstanceSecret(CP_MASTER_SECRET, instanceId);
    cpToken = await mintControlPlaneToken(instanceSecret, {
      email,
      instanceId,
      role,
    });
  } catch (err) {
    console.error("dashboard-login: token mint error", err.message);
    return json(500, { error: "Could not start a dashboard session. Try again." });
  }

  const url =
    "https://" + instanceDomain + "/auth/cp?token=" + encodeURIComponent(cpToken);
  return json(200, { ok: true, hosted: true, url });
}
