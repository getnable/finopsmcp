/**
 * POST /api/account/hosting
 *
 * Two jobs, one endpoint (keeps the client simple):
 *   - Fetch  (body = { token }):           returns the account summary the
 *     account page renders: plan, subscription status, member-since, and the
 *     current hosting choice stored on the Stripe customer.
 *   - Save   (body = { token, choice, ... }): records the customer's hosting
 *     choice. "self" = they run finops serve themselves. "hosted" = they want
 *     a managed single-tenant instance; we capture the request as a lead and
 *     a human provisions it (concierge-first, no auto-provisioning yet).
 *
 * The choice + details live in Stripe customer metadata, so they show up in the
 * admin view and in the Stripe dashboard with zero new infrastructure. A free
 * user with no Stripe customer yet gets one created to hold the lead.
 *
 * Required env vars:
 *   ACCOUNT_SECRET     -- session token HMAC (must match verify-code.js)
 *   STRIPE_SECRET_KEY  -- customer lookup / create / metadata update
 */

export const config = { runtime: "edge" };

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "https://getnable.com",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

const STRIPE_API = "https://api.stripe.com/v1";

// ── Session token verification (mirrors billing-portal.js) ─────────────────────

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

// ── Stripe helpers ─────────────────────────────────────────────────────────────

async function stripeGet(path, key) {
  const res = await fetch(`${STRIPE_API}${path}`, {
    headers: { Authorization: `Bearer ${key}` },
  });
  return res;
}

async function stripePostForm(path, key, form, extraHeaders) {
  return fetch(`${STRIPE_API}${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/x-www-form-urlencoded",
      ...(extraHeaders || {}),
    },
    body: form.toString(),
  });
}

// Plan from an expanded customer (mirrors verify-code.js getStripePlan).
function planFromCustomer(customer) {
  const subs = customer && customer.subscriptions && customer.subscriptions.data;
  if (!subs || subs.length === 0) return { plan: "free", status: "none" };
  const activeSub = subs.find((s) => s.status === "active" || s.status === "trialing");
  if (!activeSub) return { plan: "free", status: subs[0].status || "inactive" };
  if (activeSub.status === "trialing") return { plan: "trial", status: "trialing" };
  const teamIds = new Set(
    (process.env.STRIPE_TEAM_PRICE_IDS || "").split(",").map((x) => x.trim()).filter(Boolean)
  );
  const priceIds = (activeSub.items?.data || []).map((i) => i.price?.id).filter(Boolean);
  const plan = priceIds.some((id) => teamIds.has(id)) ? "team" : "pro";
  return { plan, status: "active" };
}

function hostingFromMetadata(md) {
  md = md || {};
  return {
    mode: md.hosting_mode || null, // "self" | "hosted" | null
    cloud: md.hosting_cloud || null,
    idp: md.hosting_idp || null,
    team_size: md.hosting_team_size || null,
    status: md.hosting_status || null, // "requested" for managed
    updated_at: md.hosting_updated_at || null,
  };
}

// ── Input sanitizing (Stripe metadata: keys <=40, values <=500 chars) ──────────

function clip(v, n) {
  return String(v == null ? "" : v).replace(/[\r\n]+/g, " ").trim().slice(0, n);
}

// ── Save rate limiting (mirrors verify-code.js: in-memory + optional Vercel KV) ─
// The save path issues Stripe writes and can create a customer, so cap it per
// session email to stop a token holder from looping it (the other sensitive
// endpoints all throttle; this one must too).
const _saveAttempts = new Map();
const _SAVE_WINDOW_S = 3600;
const _SAVE_MAX = 20;

function _localTooManySaves(key) {
  const now = Date.now();
  const e = _saveAttempts.get(key) || { count: 0, resetAt: now + _SAVE_WINDOW_S * 1000 };
  if (now > e.resetAt) { e.count = 0; e.resetAt = now + _SAVE_WINDOW_S * 1000; }
  if (e.count >= _SAVE_MAX) return true;
  e.count += 1;
  _saveAttempts.set(key, e);
  if (_saveAttempts.size > 500) {
    for (const [k, v] of _saveAttempts) if (now > v.resetAt) _saveAttempts.delete(k);
  }
  return false;
}

async function tooManySaves(email) {
  const url = process.env.VERCEL_KV_REST_API_URL;
  const token = process.env.VERCEL_KV_REST_API_TOKEN;
  if (url && token) {
    try {
      const k = encodeURIComponent(`hosting_saves:${email}`);
      const res = await fetch(`${url}/incr/${k}`, { headers: { Authorization: `Bearer ${token}` } });
      const data = await res.json();
      const count = Number(data.result);
      if (count === 1) {
        await fetch(`${url}/expire/${k}/${_SAVE_WINDOW_S}`, { headers: { Authorization: `Bearer ${token}` } });
      }
      if (Number.isFinite(count)) return count > _SAVE_MAX;
    } catch {
      // KV unreachable: fall through to the local cap rather than failing open
    }
  }
  return _localTooManySaves(email);
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
    console.error("hosting: missing ACCOUNT_SECRET or STRIPE_SECRET_KEY");
    return json(500, { error: "Service misconfigured" });
  }

  const session = await verifySessionToken(ACCOUNT_SECRET, token);
  if (!session) {
    return json(401, { error: "Invalid or expired session. Please sign in again." });
  }
  const email = session.email;

  // Look up the customer (with subscriptions for plan/status). Track whether the
  // lookup actually SUCCEEDED, so a transient Stripe failure is not mistaken for
  // "this user has no customer" (which would wrongly downgrade them to Free or
  // create a duplicate customer).
  let customer = null;
  let lookupOk = false;
  try {
    const res = await stripeGet(
      `/customers?email=${encodeURIComponent(email)}&limit=1&expand[]=data.subscriptions`,
      STRIPE_KEY
    );
    if (res.ok) {
      const data = await res.json();
      customer = data.data && data.data[0];
      lookupOk = true;
    } else {
      console.error(`hosting: customer lookup failed ${res.status}`);
    }
  } catch (err) {
    console.error("hosting: customer lookup error", err.message);
  }

  const isSave = typeof body.choice === "string" && body.choice.length > 0;

  // ── Save path ───────────────────────────────────────────────────────────────
  if (isSave) {
    const choice = body.choice === "hosted" ? "hosted" : body.choice === "self" ? "self" : null;
    if (!choice) return json(400, { error: "Invalid choice" });

    // A transient lookup failure must not fall through to creating a duplicate
    // customer; ask the caller to retry instead.
    if (!lookupOk && !(customer && customer.id)) {
      return json(502, { error: "Could not reach billing. Try again in a moment." });
    }

    // Self-host is the free default, not a lead. A free user with no Stripe
    // customer who clicks "Run it yourself" should NOT create a phantom customer;
    // a customer-less account already reads as self, so there is nothing to persist.
    if (choice === "self" && !(customer && customer.id)) {
      return json(200, { ok: true, hosting: { mode: "self" } });
    }

    if (await tooManySaves(email)) {
      return json(429, { error: "Too many changes. Try again in a few minutes." });
    }

    const form = new URLSearchParams();
    form.set("metadata[hosting_mode]", choice);
    form.set("metadata[hosting_updated_at]", new Date().toISOString());
    if (choice === "hosted") {
      form.set("metadata[hosting_status]", "requested");
      if (body.cloud) form.set("metadata[hosting_cloud]", clip(body.cloud, 40));
      if (body.idp) form.set("metadata[hosting_idp]", clip(body.idp, 60));
      if (body.team_size) form.set("metadata[hosting_team_size]", clip(body.team_size, 20));
      if (body.notes) form.set("metadata[hosting_notes]", clip(body.notes, 500));
    } else {
      // Switching back to self-host clears the managed request flag AND the stale
      // managed-request details (empty string => Stripe deletes the key), so the
      // admin view never shows "Self · aws" left over from a prior request.
      form.set("metadata[hosting_status]", "");
      form.set("metadata[hosting_cloud]", "");
      form.set("metadata[hosting_idp]", "");
      form.set("metadata[hosting_team_size]", "");
      form.set("metadata[hosting_notes]", "");
    }

    try {
      let res;
      if (customer && customer.id) {
        res = await stripePostForm(`/customers/${customer.id}`, STRIPE_KEY, form);
      } else {
        // No customer yet (free user requesting hosting): create one to hold the
        // lead. An Idempotency-Key keyed to the email collapses double-clicks and
        // retries into a single customer instead of racing to create duplicates.
        form.set("email", email);
        res = await stripePostForm(`/customers`, STRIPE_KEY, form, {
          "Idempotency-Key": `hosting-create:${email}`,
        });
      }
      const updated = await res.json();
      if (!res.ok) {
        console.error("hosting: save failed", JSON.stringify(updated.error || updated));
        return json(502, { error: "Could not save your choice. Try again in a moment." });
      }
      return json(200, { ok: true, hosting: hostingFromMetadata(updated.metadata) });
    } catch (err) {
      console.error("hosting: save error", err.message);
      return json(502, { error: "Could not save your choice. Try again in a moment." });
    }
  }

  // ── Fetch path (account summary) ──────────────────────────────────────────────
  // Distinguish "looked up, genuinely no customer" from "lookup failed": the
  // latter must not masquerade as a Free account. The client treats a non-200 as
  // non-fatal (the hosting panel just stays at its defaults), so 502 is safe.
  if (!lookupOk) {
    return json(502, { error: "Could not reach billing. Try again in a moment." });
  }
  const { plan, status } = customer ? planFromCustomer(customer) : { plan: "free", status: "none" };
  return json(200, {
    ok: true,
    plan,
    status,
    member_since: customer && customer.created ? customer.created : null, // unix seconds
    hosting: hostingFromMetadata(customer && customer.metadata),
  });
}
