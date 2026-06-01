/**
 * POST /api/account/verify-code
 *
 * Verifies a 6-digit OTP and returns a session token plus license info.
 * Uses time-bucketed HMAC (no KV required). Checks both the current and
 * the previous time bucket to handle code entry at the 10-minute boundary.
 *
 * Required env vars:
 *   ACCOUNT_SECRET        -- must match send-code.js
 *   STRIPE_SECRET_KEY     -- for subscription lookup
 *   FINOPS_LICENSE_PRIVATE_KEY -- Ed25519 seed (base64url) for signing license keys
 */

export const config = { runtime: "edge" };

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "https://getnable.com",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

const STRIPE_API = "https://api.stripe.com/v1";

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
  const bytes = new TextEncoder().encode(str);
  let binary = "";
  bytes.forEach(b => binary += String.fromCharCode(b));
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

// ── OTP verification ──────────────────────────────────────────────────────────

async function verifyOtp(secret, email, code) {
  if (!code || !/^\d{6}$/.test(code)) return false;
  const now = Date.now();
  // Check current bucket and the previous one to handle boundary edge cases
  const buckets = [
    Math.floor(now / 600000),
    Math.floor(now / 600000) - 1,
  ];
  for (const bucket of buckets) {
    const mac = await hmacHex(secret, `otp:${email}:${bucket}`);
    const expected = (parseInt(mac.slice(0, 8), 16) % 900000 + 100000).toString();
    if (timingSafeEqual(expected, code)) return true;
  }
  return false;
}

// ── License key generation (v2, Ed25519 — mirrors license.py) ─────────────────
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

async function generateLicenseKey(email) {
  const d = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  const payload = b64url(JSON.stringify({ e: email, d, p: "pro" }));
  const seed = b64urlToBytes(process.env.FINOPS_LICENSE_PRIVATE_KEY);
  const pkcs8 = new Uint8Array(ED25519_PKCS8_PREFIX.length + seed.length);
  pkcs8.set(ED25519_PKCS8_PREFIX);
  pkcs8.set(seed, ED25519_PKCS8_PREFIX.length);
  const key = await crypto.subtle.importKey("pkcs8", pkcs8, { name: "Ed25519" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("Ed25519", key, new TextEncoder().encode(`2:${payload}`));
  return `FINOPS-2-${payload}-${bytesToB64url(new Uint8Array(sig))}`;
}

// ── Session token (signed payload, no library needed) ─────────────────────────

async function createSessionToken(secret, email, plan) {
  const exp = Math.floor(Date.now() / 1000) + 86400; // 24h
  const payloadJson = JSON.stringify({ email, plan, exp });
  const payload = b64url(payloadJson);
  const sig = await hmacHex(secret, payload);
  return `${payload}.${sig}`;
}

// ── Stripe subscription lookup ────────────────────────────────────────────────

async function getStripePlan(email, stripeKey) {
  const url = `${STRIPE_API}/customers?email=${encodeURIComponent(email)}&limit=1&expand[]=data.subscriptions`;
  const res = await fetch(url, {
    headers: {
      Authorization: `Bearer ${stripeKey}`,
    },
  });
  if (!res.ok) {
    console.error(`Stripe customer lookup failed: ${res.status}`);
    return "free";
  }
  const data = await res.json();
  const customer = data.data && data.data[0];
  if (!customer) return "free";

  const subs = customer.subscriptions && customer.subscriptions.data;
  if (!subs || subs.length === 0) return "free";

  const activeSub = subs.find(
    (s) => s.status === "active" || s.status === "trialing"
  );
  if (!activeSub) return "free";
  if (activeSub.status === "trialing") return "trial";
  return "pro";
}

// ── Handler ────────────────────────────────────────────────────────────────────

export default async function handler(req) {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS_HEADERS });
  }

  if (req.method !== "POST") {
    return new Response(JSON.stringify({ error: "Method not allowed" }), {
      status: 405,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    });
  }

  let body;
  try {
    body = await req.json();
  } catch {
    return new Response(JSON.stringify({ error: "Invalid JSON" }), {
      status: 400,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    });
  }

  const email = (body.email || "").trim().toLowerCase();
  const code = (body.code || "").trim();

  if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return new Response(JSON.stringify({ error: "Invalid email" }), {
      status: 400,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    });
  }

  if (!code || !/^\d{6}$/.test(code)) {
    return new Response(JSON.stringify({ error: "Invalid code format" }), {
      status: 400,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    });
  }

  const ACCOUNT_SECRET = process.env.ACCOUNT_SECRET;
  const STRIPE_KEY = process.env.STRIPE_SECRET_KEY;
  const LICENSE_PRIVATE_KEY = process.env.FINOPS_LICENSE_PRIVATE_KEY;

  if (!ACCOUNT_SECRET) {
    console.error("ACCOUNT_SECRET not configured");
    return new Response(JSON.stringify({ error: "Service misconfigured" }), {
      status: 500,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    });
  }

  const valid = await verifyOtp(ACCOUNT_SECRET, email, code);
  if (!valid) {
    return new Response(
      JSON.stringify({ error: "Invalid or expired code. Please try again." }),
      {
        status: 401,
        headers: { "Content-Type": "application/json", ...CORS_HEADERS },
      }
    );
  }

  // Determine plan via Stripe
  let plan = "free";
  if (STRIPE_KEY) {
    try {
      plan = await getStripePlan(email, STRIPE_KEY);
    } catch (err) {
      console.error("Stripe lookup error:", err.message);
      // Default to free on error; do not block sign-in
    }
  }

  // Generate license key for pro/trial users
  let license_key = null;
  if ((plan === "pro" || plan === "trial") && LICENSE_PRIVATE_KEY) {
    try {
      license_key = await generateLicenseKey(email);
    } catch (err) {
      console.error("License key generation error:", err.message);
    }
  }

  // Create session token
  const token = await createSessionToken(ACCOUNT_SECRET, email, plan);

  return new Response(
    JSON.stringify({ ok: true, token, email, plan, license_key }),
    {
      status: 200,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    }
  );
}
