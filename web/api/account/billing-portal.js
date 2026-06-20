/**
 * POST /api/account/billing-portal
 *
 * Verifies a session token, finds the caller's Stripe customer by their
 * verified email, and creates a Billing Portal session so they can update their
 * card, download invoices, or cancel. Returns { url } for the client to redirect
 * to. This replaces a hardcoded portal link that went stale and 404'd.
 *
 * Required env vars:
 *   ACCOUNT_SECRET     -- must match verify-code.js (session token HMAC)
 *   STRIPE_SECRET_KEY  -- for the customer lookup + portal session
 */

export const config = { runtime: "edge" };

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "https://getnable.com",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

const STRIPE_API = "https://api.stripe.com/v1";
const RETURN_URL = "https://getnable.com/account";

// ── Session token verification (mirrors rotate-key.js / verify-code.js) ────────

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
    console.error("billing-portal: missing ACCOUNT_SECRET or STRIPE_SECRET_KEY");
    return json(500, { error: "Service misconfigured" });
  }

  const session = await verifySessionToken(ACCOUNT_SECRET, token);
  if (!session) {
    return json(401, { error: "Invalid or expired session. Please sign in again." });
  }

  const email = session.email;

  // Find the Stripe customer for this verified email.
  let customerId;
  try {
    const res = await fetch(
      `${STRIPE_API}/customers?email=${encodeURIComponent(email)}&limit=1`,
      { headers: { Authorization: `Bearer ${STRIPE_KEY}` } }
    );
    if (!res.ok) {
      console.error(`billing-portal: customer lookup failed ${res.status}`);
      return json(502, { error: "Could not reach billing. Try again in a moment." });
    }
    const data = await res.json();
    customerId = data.data && data.data[0] && data.data[0].id;
  } catch (err) {
    console.error("billing-portal: customer lookup error", err.message);
    return json(502, { error: "Could not reach billing. Try again in a moment." });
  }

  if (!customerId) {
    return json(404, {
      error: "No billing account found for this email. The free tier has no subscription to manage.",
    });
  }

  // Create a portal session for this customer.
  try {
    const form = new URLSearchParams();
    form.set("customer", customerId);
    form.set("return_url", RETURN_URL);
    const res = await fetch(`${STRIPE_API}/billing_portal/sessions`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${STRIPE_KEY}`,
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: form.toString(),
    });
    const data = await res.json();
    if (!res.ok) {
      // Most common cause: the Customer Portal is not activated in the Stripe
      // dashboard (Settings -> Billing -> Customer portal). Surface the reason.
      console.error("billing-portal: session create failed", JSON.stringify(data.error || data));
      const msg = data.error && data.error.message ? data.error.message : "Could not open billing.";
      return json(502, { error: msg });
    }
    return json(200, { url: data.url });
  } catch (err) {
    console.error("billing-portal: session create error", err.message);
    return json(502, { error: "Could not open billing. Try again in a moment." });
  }
}
