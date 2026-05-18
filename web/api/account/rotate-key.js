/**
 * POST /api/account/rotate-key
 *
 * Verifies a session token, generates a new license key, and emails it.
 * Only available to Pro subscribers.
 *
 * Required env vars:
 *   ACCOUNT_SECRET        -- must match verify-code.js
 *   FINOPS_LICENSE_SECRET -- must match the MCP server
 *   RESEND_API_KEY        -- from resend.com
 */

export const config = { runtime: "edge" };

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "https://nable.sh",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

const RESEND_API = "https://api.resend.com";

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
  return btoa(str)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function b64urlDecode(str) {
  const padded = str.replace(/-/g, "+").replace(/_/g, "/");
  const pad = padded.length % 4;
  return atob(pad ? padded + "=".repeat(4 - pad) : padded);
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

// ── Session token verification ─────────────────────────────────────────────────

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

// ── License key generation (mirrors stripe-webhook.js and license.py) ─────────

async function generateLicenseKey(email, licenseSecret) {
  const d = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  const payloadJson = JSON.stringify({ e: email, d, p: "pro" });
  const payload = b64url(payloadJson);
  const sigHex = await hmacHex(licenseSecret, `1:${payload}`);
  const sigBytes = sigHex.match(/.{2}/g).map((h) => parseInt(h, 16));
  const sigB64 = b64url(
    String.fromCharCode.apply(null, sigBytes)
  );
  return `FINOPS-1-${payload}-${sigB64}`;
}

// ── Rotation notification email ────────────────────────────────────────────────

async function sendRotationEmail(to, licenseKey, resendKey) {
  const html = `<!DOCTYPE html>
<html>
<head><meta charset="utf-8"/></head>
<body style="margin:0;padding:0;background:#fbfaf7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="max-width:480px;margin:48px auto;padding:0 24px 48px;">

  <div style="margin-bottom:32px;">
    <svg width="32" height="32" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect width="32" height="32" rx="7" fill="#1a1915"/>
      <path d="M9.5 23V11.5h2.6v1.5c.7-1.1 1.9-1.7 3.4-1.7 2.6 0 4.2 1.7 4.2 4.5V23h-2.7v-6.6c0-1.7-.9-2.6-2.4-2.6s-2.5 1-2.5 2.7V23H9.5Z" fill="#fbfaf7"/>
    </svg>
  </div>

  <h1 style="font-size:20px;font-weight:500;letter-spacing:-0.025em;color:#1a1915;margin:0 0 8px;">
    Your nable license key has been rotated
  </h1>
  <p style="font-size:14px;color:#54524a;line-height:1.65;margin:0 0 28px;">
    Your previous key is now invalid. Use the new key below.
  </p>

  <div style="background:#1a1915;border-radius:8px;padding:18px 20px;margin-bottom:24px;">
    <p style="font-family:'JetBrains Mono','Courier New',monospace;font-size:11.5px;color:#fbfaf7;word-break:break-all;margin:0;line-height:1.7;">
      ${licenseKey}
    </p>
  </div>

  <div style="background:#ebe8e0;border-radius:7px;padding:12px 16px;margin-bottom:24px;">
    <p style="font-family:'JetBrains Mono','Courier New',monospace;font-size:12px;color:#1a1915;word-break:break-all;margin:0;">
      FINOPS_LICENSE_KEY=${licenseKey}
    </p>
  </div>

  <p style="font-size:13px;color:#8b8879;line-height:1.6;margin:0;">
    If you did not request this rotation, contact
    <a href="mailto:hello@nable.sh" style="color:#1a1915;">hello@nable.sh</a> immediately.
  </p>

  <hr style="border:none;border-top:1px solid #e6e2d6;margin:28px 0 20px;"/>
  <p style="font-size:12px;color:#8b8879;margin:0;line-height:1.6;">
    <a href="https://nable.sh" style="color:#1a1915;">nable.sh</a> &middot;
    <a href="https://billing.stripe.com/p/login/eVq3cY8qQ" style="color:#8b8879;">Manage billing</a>
  </p>
</div>
</body>
</html>`;

  const res = await fetch(`${RESEND_API}/emails`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${resendKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      from: "nable <noreply@nable.sh>",
      to: [to],
      subject: "Your nable license key has been rotated",
      html,
    }),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Resend ${res.status}: ${text}`);
  }
  return res.json();
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

  const token = (body.token || "").trim();
  if (!token) {
    return new Response(JSON.stringify({ error: "Missing session token" }), {
      status: 401,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    });
  }

  const ACCOUNT_SECRET = process.env.ACCOUNT_SECRET;
  const LICENSE_SECRET = process.env.FINOPS_LICENSE_SECRET;
  const RESEND_KEY = process.env.RESEND_API_KEY;

  if (!ACCOUNT_SECRET || !LICENSE_SECRET) {
    console.error("Missing required environment variables for rotate-key");
    return new Response(JSON.stringify({ error: "Service misconfigured" }), {
      status: 500,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    });
  }

  const session = await verifySessionToken(ACCOUNT_SECRET, token);
  if (!session) {
    return new Response(
      JSON.stringify({ error: "Invalid or expired session. Please sign in again." }),
      {
        status: 401,
        headers: { "Content-Type": "application/json", ...CORS_HEADERS },
      }
    );
  }

  const { email, plan } = session;

  if (plan !== "pro" && plan !== "trial") {
    return new Response(
      JSON.stringify({ error: "Key rotation requires a Pro subscription." }),
      {
        status: 403,
        headers: { "Content-Type": "application/json", ...CORS_HEADERS },
      }
    );
  }

  let license_key;
  try {
    license_key = await generateLicenseKey(email, LICENSE_SECRET);
  } catch (err) {
    console.error("License key generation failed:", err.message);
    return new Response(
      JSON.stringify({ error: "Failed to generate new key. Please try again." }),
      {
        status: 500,
        headers: { "Content-Type": "application/json", ...CORS_HEADERS },
      }
    );
  }

  if (RESEND_KEY) {
    try {
      await sendRotationEmail(email, license_key, RESEND_KEY);
    } catch (err) {
      // Log but do not block; user already has the key on screen
      console.error("Rotation email failed:", err.message);
    }
  } else {
    console.error("RESEND_API_KEY not set; rotation email not sent");
  }

  return new Response(JSON.stringify({ ok: true, license_key }), {
    status: 200,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}
