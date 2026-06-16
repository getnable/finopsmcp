/**
 * Stripe webhook → license key delivery
 *
 * Listens for checkout.session.completed, generates a signed license key,
 * and emails it to the customer via Resend.
 *
 * Required env vars (set in Vercel project settings):
 *   STRIPE_WEBHOOK_SECRET   — from Stripe Dashboard → Webhooks → signing secret
 *   RESEND_API_KEY          — from resend.com
 */

import crypto from "node:crypto";

// Two-layer deduplication:
// Layer 1 — in-memory Set: fast dedup within the same warm Lambda instance.
// Layer 2 — Vercel KV (optional): cross-instance persistent dedup.
//   Set VERCEL_KV_REST_API_URL + VERCEL_KV_REST_API_TOKEN in Vercel project settings
//   to enable. Without KV, cold-start duplicates may re-send the same key email
//   (harmless: same email+date produces the same key, so both emails are valid).
const processedEvents = new Set();

async function _kvMarkSeen(eventId) {
  const url = process.env.VERCEL_KV_REST_API_URL;
  const token = process.env.VERCEL_KV_REST_API_TOKEN;
  if (!url || !token) return false; // KV not configured — fall back to in-memory
  try {
    const key = `stripe_dedup:${eventId}`;
    // SET NX EX 86400 — set only if not exists, expire after 24h
    const res = await fetch(`${url}/set/${encodeURIComponent(key)}/1/EX/86400/NX`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const data = await res.json();
    return data.result === null; // null = key existed = duplicate
  } catch {
    return false; // on KV failure, allow processing (prefer duplicate to dropped event)
  }
}

// Undo the dedup marks when delivery fails, so Stripe's retry is processed
// instead of being swallowed as a duplicate. Without this, a Resend outage
// means the customer paid and never gets a key.
async function _kvUnmark(eventId) {
  const url = process.env.VERCEL_KV_REST_API_URL;
  const token = process.env.VERCEL_KV_REST_API_TOKEN;
  if (!url || !token) return;
  try {
    await fetch(`${url}/del/${encodeURIComponent(`stripe_dedup:${eventId}`)}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
  } catch {
    // KV unmark failure leaves a 24h-expiring mark; the in-memory delete
    // still lets a retry through on a fresh instance.
  }
}

// Set FINOPS_LICENSE_PRIVATE_KEY in Vercel project environment variables.
// Ed25519 private signing key (raw 32-byte seed, base64url). The matching public
// key is bundled in the MCP server (license.py) and verifies keys with no shared
// secret. This private key must never be exposed client-side.
const LICENSE_PRIVATE_KEY = process.env.FINOPS_LICENSE_PRIVATE_KEY;

// PKCS8 DER prefix for an Ed25519 private key; the 32-byte raw seed follows it.
const ED25519_PKCS8_PREFIX = Buffer.from("302e020100300506032b657004220420", "hex");

// ─── License key generation (v2, Ed25519) ────────────────────────────────────
// Mirrors generate_key() in license.py so keys validate in the MCP server.

function b64url(buf) {
  return buf.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// Map the checkout to a license plan. Primary signal: which payment link the
// customer used (session.payment_link, a plink_... id). Set these in Vercel:
//   STRIPE_TEAM_PAYMENT_LINKS — comma-separated plink ids for Team links
//   STRIPE_PRO_PAYMENT_LINKS  — comma-separated plink ids for Pro links
// (Stripe Dashboard → Payment links → click a link → id is in the URL.)
//
// Fallback when the link id is unknown (negotiated deals, manual invoices):
// amount at or above STRIPE_TEAM_MIN_CENTS (default $500) issues a team key.
// The fallback cannot tell Pro annual ($1,000/yr) from Team monthly
// ($1,000/mo) — both are 100000 cents — so that exact amount falls back to
// PRO. Issuing the cheaper key to a Team buyer is a recoverable support
// email; silently handing the Slack-bot tier to a Pro buyer is not. With the
// link env vars set, the ambiguity never arises. Team keys unlock the
// conversational Slack bot; pro keys do not.
const TEAM_MIN_CENTS = parseInt(process.env.STRIPE_TEAM_MIN_CENTS || "50000", 10);
const PRO_ANNUAL_TEAM_MONTHLY_COLLISION_CENTS = 100000;

function _linkSet(envVar) {
  return new Set(
    (process.env[envVar] || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)
  );
}

function planForSession(session) {
  const link = session.payment_link || null;
  if (link) {
    if (_linkSet("STRIPE_TEAM_PAYMENT_LINKS").has(link)) return "team";
    if (_linkSet("STRIPE_PRO_PAYMENT_LINKS").has(link)) return "pro";
  }
  return planForAmount(session.amount_total ?? 0);
}

function planForAmount(amount) {
  if (amount === PRO_ANNUAL_TEAM_MONTHLY_COLLISION_CENTS) return "pro";
  return amount >= TEAM_MIN_CENTS ? "team" : "pro";
}

// Renewal invoices carry price ids, not payment links. Map via
//   STRIPE_TEAM_PRICE_IDS / STRIPE_PRO_PRICE_IDS — comma-separated price_...
// ids (Stripe Dashboard → Products → price). Falls back to amount.
function planForInvoice(invoice) {
  // Line price id moved between Stripe API versions: classic `line.price.id`
  // vs basil-era `line.pricing.price_details.price`. Read both. On proration
  // invoices (upgrades/downgrades) multiple lines carry different prices, so
  // classify from the line with the latest period end (the plan going
  // forward), not "any line matches".
  const lines = (invoice.lines?.data || [])
    .map((l) => ({
      id: l.price?.id || l.pricing?.price_details?.price || null,
      end: l.period?.end ?? 0,
    }))
    .filter((l) => l.id)
    .sort((a, b) => b.end - a.end);
  const teamIds = _linkSet("STRIPE_TEAM_PRICE_IDS");
  const proIds = _linkSet("STRIPE_PRO_PRICE_IDS");
  for (const { id } of lines) {
    if (teamIds.has(id)) return "team";
    if (proIds.has(id)) return "pro";
  }
  return planForAmount(invoice.amount_paid ?? 0);
}

function generateKey(email, plan) {
  const d = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  const payload = b64url(Buffer.from(JSON.stringify({ e: email, d, p: plan })));
  const seed = Buffer.from(LICENSE_PRIVATE_KEY, "base64url");
  const keyObj = crypto.createPrivateKey({
    key: Buffer.concat([ED25519_PKCS8_PREFIX, seed]),
    format: "der",
    type: "pkcs8",
  });
  const sig = b64url(crypto.sign(null, Buffer.from(`2:${payload}`), keyObj));
  return `FINOPS-2-${payload}-${sig}`;
}

// ─── Stripe signature verification ───────────────────────────────────────────

const STRIPE_TIMESTAMP_TOLERANCE_S = 300; // 5 minutes — reject replays

function verifyStripe(rawBody, sigHeader, secret) {
  // sigHeader format: t=timestamp,v1=hex_sig[,v1=...,v0=...]
  // During secret rotation Stripe signs with both secrets and sends multiple
  // v1 entries, so collect them all and accept if any matches.
  const pairs = sigHeader.split(",").map((p) => p.split("=", 2));
  const t = (pairs.find(([k]) => k === "t") || [])[1];
  const v1s = pairs.filter(([k]) => k === "v1").map(([, v]) => v || "");

  // Replay attack prevention: reject if timestamp is more than 5 minutes old
  const ts = parseInt(t, 10);
  const now = Math.floor(Date.now() / 1000);
  if (isNaN(ts) || Math.abs(now - ts) > STRIPE_TIMESTAMP_TOLERANCE_S) {
    console.error(`Stripe webhook timestamp out of tolerance: ts=${ts} now=${now}`);
    return false;
  }

  const signed = `${t}.${rawBody}`;
  const expected = crypto.createHmac("sha256", secret).update(signed).digest("hex");
  const expBuf = Buffer.from(expected, "hex");
  return v1s.some((v1) => {
    const gotBuf = Buffer.from(v1, "hex");
    return expBuf.length === gotBuf.length && crypto.timingSafeEqual(expBuf, gotBuf);
  });
}

// ─── Email via Resend ─────────────────────────────────────────────────────────

async function sendLicenseEmail(to, licenseKey, plan) {
  const planLabel = plan === "team" ? "Team" : "Pro";
  const html = `<!DOCTYPE html>
<html>
<head><meta charset="utf-8"/></head>
<body style="margin:0;padding:0;background:#fbfaf7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="max-width:520px;margin:48px auto;padding:0 24px 48px;">

  <!-- Logo -->
  <div style="margin-bottom:36px;">
    <svg width="32" height="32" viewBox="0 0 120 120" fill="none" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="nmg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5cc1da"/><stop offset="1" stop-color="#3a9ab6"/></linearGradient></defs><rect width="120" height="120" rx="27" fill="url(#nmg)"/><path d="M44 80 L44 56 A16 16 0 0 1 76 56 L76 80" fill="none" stroke="#0d0f10" stroke-width="13" stroke-linecap="round" stroke-linejoin="round"/></svg>
  </div>

  <h1 style="font-size:22px;font-weight:500;letter-spacing:-0.025em;color:#1a1915;margin:0 0 10px;">
    Your nable ${planLabel} license key
  </h1>
  <p style="font-size:15px;color:#54524a;line-height:1.65;margin:0 0 32px;">
    Thanks for subscribing. Here's your license key — keep it somewhere safe.
  </p>

  <!-- Key block -->
  <div style="background:#1a1915;border-radius:8px;padding:18px 20px;margin-bottom:32px;">
    <p style="font-family:'JetBrains Mono','Courier New',monospace;font-size:11.5px;color:#fbfaf7;word-break:break-all;margin:0;line-height:1.7;">
      ${licenseKey}
    </p>
  </div>

  <!-- Step 1 -->
  <div style="margin-bottom:20px;">
    <p style="font-size:13px;color:#54524a;margin:0 0 8px;">
      <strong style="color:#1a1915;">Step 1 — </strong>Run this command in your terminal. It activates your key and writes it to your editor config automatically.
    </p>
    <div style="background:#ebe8e0;border-radius:7px;padding:12px 16px;">
      <code style="font-family:'JetBrains Mono','Courier New',monospace;font-size:12px;color:#1a1915;word-break:break-all;">
        finops setup license ${licenseKey}
      </code>
    </div>
  </div>

  <!-- Step 2 -->
  <div style="margin-bottom:36px;">
    <p style="font-size:13px;color:#54524a;margin:0 0 8px;">
      <strong style="color:#1a1915;">Step 2 — </strong>Restart your editor. ${planLabel} features unlock immediately.
    </p>
    <p style="font-size:12px;color:#8b8879;margin:6px 0 0;">
      If you haven't installed nable yet, run <code style="font-family:'JetBrains Mono','Courier New',monospace;font-size:11px;">pip install finops-mcp &amp;&amp; finops setup</code> first.
    </p>
  </div>

  <!-- CTA -->
  <a href="https://getnable.com/docs" style="display:inline-block;background:#1a1915;color:#fbfaf7;font-size:13px;font-weight:500;text-decoration:none;padding:11px 20px;border-radius:7px;letter-spacing:-0.005em;">
    Open setup guide →
  </a>

  <!-- Footer -->
  <hr style="border:none;border-top:1px solid #e6e2d6;margin:36px 0 20px;"/>
  <p style="font-size:12px;color:#8b8879;margin:0;line-height:1.6;">
    Questions? Reply here or email
    <a href="mailto:hello@getnable.com" style="color:#1a1915;">hello@getnable.com</a>.
    You can manage your subscription at any time via
    <a href="https://billing.stripe.com/p/login/eVq3cY8qQ" style="color:#1a1915;">the billing portal</a>.
  </p>
</div>
</body>
</html>`;

  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${process.env.RESEND_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from: "nable <hello@getnable.com>",
      reply_to: "hello@getnable.com",
      to: [to],
      subject: `Your nable ${planLabel} license key`,
      html,
    }),
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Resend ${res.status}: ${body}`);
  }
  return res.json();
}

// ─── Raw body reader ──────────────────────────────────────────────────────────

function readRawBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

// ─── Handler ──────────────────────────────────────────────────────────────────

export const config = { api: { bodyParser: false } };

export default async function handler(req, res) {
  if (req.method !== "POST") {
    return res.status(405).json({ error: "method not allowed" });
  }

  // 1. Read raw body
  const rawBody = await readRawBody(req);

  // 2. Verify Stripe signature
  const sig = req.headers["stripe-signature"];
  const secret = process.env.STRIPE_WEBHOOK_SECRET;

  if (!secret || !LICENSE_PRIVATE_KEY) {
    console.error("Missing env vars:", { STRIPE_WEBHOOK_SECRET: !!secret, FINOPS_LICENSE_PRIVATE_KEY: !!LICENSE_PRIVATE_KEY });
    return res.status(500).json({ error: "webhook not configured" });
  }

  try {
    if (!verifyStripe(rawBody.toString("utf8"), sig || "", secret)) {
      return res.status(401).json({ error: "invalid signature" });
    }
  } catch (err) {
    console.error("Signature verification error:", err.message);
    return res.status(401).json({ error: "signature check failed" });
  }

  // 3. Parse event
  const event = JSON.parse(rawBody);
  console.log(`Stripe event: ${event.type} [${event.id}]`);

  // Handle first purchases (checkout) and renewals/upgrades (invoice.paid).
  // License keys expire after 366 days, so renewals must re-issue one.
  if (event.type !== "checkout.session.completed" && event.type !== "invoice.paid") {
    return res.status(200).json({ received: true, skipped: event.type });
  }

  if (event.type === "invoice.paid") {
    const inv = event.data.object;
    // subscription_create is already covered by checkout.session.completed;
    // $0 invoices (trial starts) should not mint a key.
    const reason = inv.billing_reason || "";
    if (reason !== "subscription_cycle" && reason !== "subscription_update") {
      return res.status(200).json({ received: true, skipped: `invoice:${reason}` });
    }
    if (!inv.amount_paid) {
      return res.status(200).json({ received: true, skipped: "invoice:zero_amount" });
    }
  }

  // Deduplicate — check in-memory first (fast), then KV (cross-instance)
  if (processedEvents.has(event.id)) {
    console.log(`Duplicate event ${event.id} (in-memory) - skipping`);
    return res.status(200).json({ received: true, deduplicated: true });
  }
  processedEvents.add(event.id);
  const kvDuplicate = await _kvMarkSeen(event.id);
  if (kvDuplicate) {
    console.log(`Duplicate event ${event.id} (KV) - skipping`);
    return res.status(200).json({ received: true, deduplicated: true });
  }

  const obj = event.data.object;
  const isInvoice = event.type === "invoice.paid";
  const email = isInvoice
    ? obj.customer_email || obj.customer_details?.email || null
    : obj.customer_details?.email || obj.customer_email || null;

  if (!email) {
    console.error(`No email on ${event.type} ${obj.id}`);
    return res.status(200).json({ received: true, warning: "no email found" });
  }

  // 4. Generate key + send email
  try {
    const plan = isInvoice ? planForInvoice(obj) : planForSession(obj);
    const key = generateKey(email, plan);
    await sendLicenseEmail(email, key, plan);
    console.log(`License key delivered to ${email} (event=${event.type}, plan=${plan}, amount=${obj.amount_total ?? obj.amount_paid}, link=${obj.payment_link || "none"})`);
  } catch (err) {
    // Return 500 so Stripe retries delivery on transient failures (Resend
    // outage, etc.), and UNDO the dedup marks so the retry is actually
    // processed instead of being swallowed as a duplicate. generateKey() is
    // deterministic for the same email+date, so a same-day retry sends the
    // same key again — harmless.
    processedEvents.delete(event.id);
    await _kvUnmark(event.id);
    console.error(`Delivery failed for ${email}:`, err.message);
    return res.status(500).json({ error: err.message });
  }

  return res.status(200).json({ received: true });
}
