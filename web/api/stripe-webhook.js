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

// Set FINOPS_LICENSE_SECRET in Vercel project environment variables.
// Must match the FINOPS_LICENSE_SECRET env var on the MCP server side.
const LICENSE_SECRET = process.env.FINOPS_LICENSE_SECRET;

// ─── License key generation ──────────────────────────────────────────────────
// Mirrors generate_key() in license.py exactly so keys are valid in the MCP server.

function b64url(buf) {
  return buf.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function generateKey(email) {
  const d = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  const payload = b64url(Buffer.from(JSON.stringify({ e: email, d, p: "pro" })));
  const sig = b64url(
    crypto.createHmac("sha256", LICENSE_SECRET).update(`1:${payload}`).digest()
  );
  return `FINOPS-1-${payload}-${sig}`;
}

// ─── Stripe signature verification ───────────────────────────────────────────

const STRIPE_TIMESTAMP_TOLERANCE_S = 300; // 5 minutes — reject replays

function verifyStripe(rawBody, sigHeader, secret) {
  // sigHeader format: t=timestamp,v1=hex_sig[,v0=...]
  const parts = Object.fromEntries(sigHeader.split(",").map((p) => p.split("=", 2)));

  // Replay attack prevention: reject if timestamp is more than 5 minutes old
  const ts = parseInt(parts.t, 10);
  const now = Math.floor(Date.now() / 1000);
  if (isNaN(ts) || Math.abs(now - ts) > STRIPE_TIMESTAMP_TOLERANCE_S) {
    console.error(`Stripe webhook timestamp out of tolerance: ts=${ts} now=${now}`);
    return false;
  }

  const signed = `${parts.t}.${rawBody}`;
  const expected = crypto.createHmac("sha256", secret).update(signed).digest("hex");
  // timingSafeEqual needs same-length buffers
  const expBuf = Buffer.from(expected, "hex");
  const gotBuf = Buffer.from(parts.v1 || "", "hex");
  if (expBuf.length !== gotBuf.length) return false;
  return crypto.timingSafeEqual(expBuf, gotBuf);
}

// ─── Email via Resend ─────────────────────────────────────────────────────────

async function sendLicenseEmail(to, licenseKey) {
  const html = `<!DOCTYPE html>
<html>
<head><meta charset="utf-8"/></head>
<body style="margin:0;padding:0;background:#fbfaf7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="max-width:520px;margin:48px auto;padding:0 24px 48px;">

  <!-- Logo -->
  <div style="margin-bottom:36px;">
    <svg width="32" height="32" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect width="32" height="32" rx="7" fill="#1a1915"/>
      <path d="M9.5 23V11.5h2.6v1.5c.7-1.1 1.9-1.7 3.4-1.7 2.6 0 4.2 1.7 4.2 4.5V23h-2.7v-6.6c0-1.7-.9-2.6-2.4-2.6s-2.5 1-2.5 2.7V23H9.5Z" fill="#fbfaf7"/>
    </svg>
  </div>

  <h1 style="font-size:22px;font-weight:500;letter-spacing:-0.025em;color:#1a1915;margin:0 0 10px;">
    Your nable Pro license key
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
      <strong style="color:#1a1915;">Step 1 — </strong>Set it in your environment:
    </p>
    <div style="background:#ebe8e0;border-radius:7px;padding:12px 16px;">
      <code style="font-family:'JetBrains Mono','Courier New',monospace;font-size:12px;color:#1a1915;word-break:break-all;">
        FINOPS_LICENSE_KEY=${licenseKey}
      </code>
    </div>
  </div>

  <!-- Step 2 -->
  <div style="margin-bottom:36px;">
    <p style="font-size:13px;color:#54524a;margin:0 0 8px;">
      <strong style="color:#1a1915;">Step 2 — </strong>Install and connect your first provider:
    </p>
    <div style="background:#ebe8e0;border-radius:7px;padding:12px 16px;">
      <code style="font-family:'JetBrains Mono','Courier New',monospace;font-size:12px;color:#1a1915;">
        pip install finops-mcp<br/>
        finops setup
      </code>
    </div>
  </div>

  <!-- CTA -->
  <a href="https://nable.sh/docs" style="display:inline-block;background:#1a1915;color:#fbfaf7;font-size:13px;font-weight:500;text-decoration:none;padding:11px 20px;border-radius:7px;letter-spacing:-0.005em;">
    Open setup guide →
  </a>

  <!-- Footer -->
  <hr style="border:none;border-top:1px solid #e6e2d6;margin:36px 0 20px;"/>
  <p style="font-size:12px;color:#8b8879;margin:0;line-height:1.6;">
    Questions? Reply here or email
    <a href="mailto:hello@nable.sh" style="color:#1a1915;">hello@nable.sh</a>.
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
      from: "nable <noreply@nable.sh>",
      to: [to],
      subject: "Your nable Pro license key",
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

  if (!secret || !LICENSE_SECRET) {
    console.error("Missing env vars:", { STRIPE_WEBHOOK_SECRET: !!secret, FINOPS_LICENSE_SECRET: !!LICENSE_SECRET });
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

  // Only handle successful checkouts
  if (event.type !== "checkout.session.completed") {
    return res.status(200).json({ received: true, skipped: event.type });
  }

  const session = event.data.object;
  const email =
    session.customer_details?.email ||
    session.customer_email ||
    null;

  if (!email) {
    console.error(`No email on session ${session.id}`);
    return res.status(200).json({ received: true, warning: "no email found" });
  }

  // 4. Generate key + send email
  try {
    const key = generateKey(email);
    await sendLicenseEmail(email, key);
    console.log(`License key delivered to ${email}`);
  } catch (err) {
    // Log but return 200 — Stripe retries on non-2xx and we don't want duplicate sends
    console.error(`Delivery failed for ${email}:`, err.message);
    return res.status(200).json({ received: true, error: err.message });
  }

  return res.status(200).json({ received: true });
}
