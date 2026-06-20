/**
 * POST /api/account/admin-customers
 *
 * Founder/admin view: lists Stripe customers with plan, status, MRR, member-since,
 * and the hosting choice each one made (from customer metadata). Sourced entirely
 * from Stripe, so it works today with no new datastore. Per-customer USAGE stats
 * (connects, queries, savings) are NOT here yet -- those need the control-plane
 * sync that ties a local instance to the account, which is a later phase.
 *
 * Access: gated on a verified session whose email is in ADMIN_EMAILS. Fails
 * CLOSED -- if ADMIN_EMAILS is unset or the caller is not listed, returns 403.
 *
 * Required env vars:
 *   ACCOUNT_SECRET     -- session token HMAC (must match verify-code.js)
 *   STRIPE_SECRET_KEY  -- customer list
 *   ADMIN_EMAILS       -- comma-separated allowlist of admin emails
 */

export const config = { runtime: "edge" };

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "https://getnable.com",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

const STRIPE_API = "https://api.stripe.com/v1";

async function hmacHex(secret, message) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return Array.from(new Uint8Array(sig)).map((b) => b.toString(16).padStart(2, "0")).join("");
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
  return parsed;
}

function json(status, obj) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}

// Monthly-normalized revenue for one active/trialing subscription.
function subMrr(sub) {
  // Trialing subscriptions pay $0 during the trial, so they contribute $0 to MRR.
  if (!sub || sub.status !== "active") return 0;
  let mrr = 0;
  for (const item of sub.items?.data || []) {
    const price = item.price;
    // Only flat licensed prices have a fixed unit_amount. Tiered/metered prices
    // carry unit_amount=null (the amount lives in price.tiers / usage records); we
    // skip them, so MRR is a floor for those. Current plans are all flat per-seat.
    if (!price || !price.recurring || typeof price.unit_amount !== "number") continue;
    const qty = item.quantity || 1;
    // Months per billing period, honoring interval_count (a quarterly plan is
    // interval=month, interval_count=3 -> 3 months/period). Branch on "month"
    // explicitly and skip unknown intervals (=> 0), mirroring the canonical Python
    // normalizer _normalize_monthly in connectors/saas/stripe.py (else: return None).
    const n = price.recurring.interval_count || 1;
    const i = price.recurring.interval;
    const monthsPerPeriod =
      i === "month" ? n : i === "year" ? n * 12 : i === "week" ? n / 4.345 : i === "day" ? n / 30.44 : 0;
    if (monthsPerPeriod <= 0) continue;
    mrr += (price.unit_amount * qty) / 100 / monthsPerPeriod; // cents -> dollars, per month
  }
  return mrr;
}

function planAndStatus(customer) {
  // Note: Stripe caps an expanded data.subscriptions sublist at 10 items per
  // customer and it is not independently paginatable. nable customers have ~1
  // subscription, so picking the active one from the first 10 is sufficient;
  // revisit (follow-up GET /subscriptions?customer=) if multi-sub customers appear.
  const subs = customer.subscriptions && customer.subscriptions.data;
  if (!subs || subs.length === 0) return { plan: "free", status: "none", mrr: 0 };
  const active = subs.find((s) => s.status === "active" || s.status === "trialing");
  if (!active) return { plan: "free", status: subs[0].status || "inactive", mrr: 0 };
  const teamIds = new Set(
    (process.env.STRIPE_TEAM_PRICE_IDS || "").split(",").map((x) => x.trim()).filter(Boolean)
  );
  const priceIds = (active.items?.data || []).map((i) => i.price?.id).filter(Boolean);
  const plan =
    active.status === "trialing" ? "trial" : priceIds.some((id) => teamIds.has(id)) ? "team" : "pro";
  // Sum MRR across ALL active subscriptions (a customer could hold a plan plus an
  // add-on); plan/status still derive from the primary active sub picked above.
  const mrr = subs.filter((s) => s.status === "active").reduce((t, s) => t + subMrr(s), 0);
  return { plan, status: active.status, mrr };
}

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
    console.error("admin-customers: missing ACCOUNT_SECRET or STRIPE_SECRET_KEY");
    return json(500, { error: "Service misconfigured" });
  }

  const session = await verifySessionToken(ACCOUNT_SECRET, token);
  if (!session) return json(401, { error: "Invalid or expired session." });

  // Fail closed: admin access requires an explicit allowlist.
  const admins = new Set(
    (process.env.ADMIN_EMAILS || "").split(",").map((x) => x.trim().toLowerCase()).filter(Boolean)
  );
  if (admins.size === 0 || !admins.has((session.email || "").toLowerCase())) {
    return json(403, { error: "Not authorized" });
  }

  // Pull customers with subscriptions expanded, paginating through Stripe's
  // 100-per-page cap so the totals don't silently undercount past 100 customers.
  // Bounded by MAX_PAGES to stay well under the edge function's time budget.
  let customers = [];
  let truncated = false;
  try {
    const MAX_PAGES = 20; // up to 2000 customers
    let startingAfter = null;
    for (let page = 0; page < MAX_PAGES; page++) {
      let url = `${STRIPE_API}/customers?limit=100&expand[]=data.subscriptions`;
      if (startingAfter) url += `&starting_after=${encodeURIComponent(startingAfter)}`;
      const res = await fetch(url, { headers: { Authorization: `Bearer ${STRIPE_KEY}` } });
      if (!res.ok) {
        console.error(`admin-customers: list failed ${res.status}`);
        return json(502, { error: "Could not reach billing." });
      }
      const data = await res.json();
      const batch = data.data || [];
      customers = customers.concat(batch);
      if (!data.has_more || batch.length === 0) break;
      if (page === MAX_PAGES - 1) { truncated = true; break; } // hit the page cap, more remain
      startingAfter = batch[batch.length - 1].id;
    }
  } catch (err) {
    console.error("admin-customers: list error", err.message);
    return json(502, { error: "Could not reach billing." });
  }

  const rows = customers.map((c) => {
    const ps = planAndStatus(c);
    const md = c.metadata || {};
    return {
      email: c.email || "(no email)",
      plan: ps.plan,
      status: ps.status,
      mrr: Math.round(ps.mrr),
      mrrRaw: ps.mrr, // unrounded, so the total sums precisely and rounds once
      created: c.created || null,
      hosting_mode: md.hosting_mode || null,
      hosting_status: md.hosting_status || null,
      hosting_cloud: md.hosting_cloud || null,
    };
  });
  rows.sort((a, b) => (b.created || 0) - (a.created || 0));

  const totals = {
    count: rows.length,
    mrr: Math.round(rows.reduce((s, r) => s + r.mrrRaw, 0)), // sum raw floats, round once
    paying: rows.filter((r) => r.plan !== "free" && r.plan !== "trial").length,
    trials: rows.filter((r) => r.plan === "trial").length,
    hosting_requested: rows.filter((r) => r.hosting_status === "requested").length,
  };

  return json(200, { ok: true, truncated, totals, customers: rows });
}
