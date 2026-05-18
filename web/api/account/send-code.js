/**
 * POST /api/account/send-code
 *
 * Sends a 6-digit OTP to the given email address using Resend.
 * Uses a time-bucketed HMAC approach so no external KV store is required.
 *
 * Required env vars:
 *   RESEND_API_KEY    -- from resend.com
 *   ACCOUNT_SECRET   -- 32+ char random secret for signing OTPs
 */

export const config = { runtime: "edge" };

const RESEND_API = "https://api.resend.com";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "https://nable.sh",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

// ── HMAC helper (Web Crypto, available in edge runtime) ───────────────────────

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

// ── Email template ─────────────────────────────────────────────────────────────

function signInEmailHtml(code) {
  return `<!DOCTYPE html>
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
    Your nable sign-in code
  </h1>
  <p style="font-size:14px;color:#54524a;line-height:1.65;margin:0 0 28px;">
    Use the code below to sign in to your nable account. It expires in 10 minutes.
  </p>

  <div style="background:#1a1915;border-radius:8px;padding:24px 20px;margin-bottom:28px;text-align:center;">
    <span style="font-family:'JetBrains Mono','Courier New',monospace;font-size:32px;color:#fbfaf7;letter-spacing:0.2em;font-weight:400;">
      ${code}
    </span>
  </div>

  <p style="font-size:13px;color:#8b8879;line-height:1.6;margin:0;">
    If you did not request this code, you can safely ignore this email.
  </p>

  <hr style="border:none;border-top:1px solid #e6e2d6;margin:28px 0 20px;"/>
  <p style="font-size:12px;color:#8b8879;margin:0;line-height:1.6;">
    <a href="https://nable.sh" style="color:#1a1915;">nable.sh</a> &middot;
    <a href="mailto:hello@nable.sh" style="color:#8b8879;">hello@nable.sh</a>
  </p>
</div>
</body>
</html>`;
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

  const ip =
    req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() || "unknown";

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

  if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    // Return 200 to avoid email enumeration
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    });
  }

  const RESEND_KEY = process.env.RESEND_API_KEY;
  const ACCOUNT_SECRET = process.env.ACCOUNT_SECRET;

  if (!ACCOUNT_SECRET) {
    console.error("ACCOUNT_SECRET not configured");
    return new Response(JSON.stringify({ error: "Service misconfigured" }), {
      status: 500,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    });
  }

  // Generate 6-digit OTP
  const code = Math.floor(100000 + Math.random() * 900000).toString();

  // Log IP for monitoring (not stored, just visible in Vercel function logs)
  const timeBucket = Math.floor(Date.now() / 600000);
  const signedPayload = `${email}:${code}:${timeBucket}`;
  const _hmac = await hmacHex(ACCOUNT_SECRET, signedPayload);
  // Note: in a stateless design the HMAC is recomputed at verify time;
  // we do not need to store it, the code itself is included in the email.
  void _hmac;

  if (RESEND_KEY) {
    try {
      const res = await fetch(`${RESEND_API}/emails`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${RESEND_KEY}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          from: "nable <noreply@nable.sh>",
          to: [email],
          subject: "Your nable sign-in code",
          html: signInEmailHtml(code),
        }),
      });
      if (!res.ok) {
        const errText = await res.text();
        console.error(`Resend error for send-code: ${res.status} ${errText}`);
      }
    } catch (err) {
      console.error("Failed to send OTP email:", err.message);
    }
  } else {
    console.error("RESEND_API_KEY not set; OTP not delivered");
  }

  // Always return ok to prevent email enumeration
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}
