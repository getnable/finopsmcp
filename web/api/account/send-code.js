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
  "Access-Control-Allow-Origin": "https://getnable.com",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

// ── Rate limiting (in-memory; resets on cold start, fine for edge) ───────────
// Throttled per IP and per target email so one caller cannot flood a victim's
// inbox or burn the Resend quota.
const rlMap = new Map();
const RL_WINDOW_MS = 60 * 60 * 1000; // 1 hour
const RL_MAX = 5;

function rateLimited(key) {
  const now = Date.now();
  const entry = rlMap.get(key) || { count: 0, resetAt: now + RL_WINDOW_MS };
  if (now > entry.resetAt) {
    entry.count = 0;
    entry.resetAt = now + RL_WINDOW_MS;
  }
  if (entry.count >= RL_MAX) return true;
  entry.count += 1;
  rlMap.set(key, entry);
  if (rlMap.size > 500) {
    for (const [k, v] of rlMap) {
      if (now > v.resetAt) rlMap.delete(k);
    }
  }
  return false;
}

// Durable, cross-instance limit via Vercel KV. The in-memory map above is
// per-warm-isolate, so an attacker spread across edge PoPs multiplies the cap
// and can still flood a victim's inbox / burn the Resend quota. When KV is
// configured this holds the cap globally; without it we fall back to the
// in-memory limiter (same behavior as before), matching verify-code.js.
const RL_WINDOW_S = Math.floor(RL_WINDOW_MS / 1000);

async function kvRateLimited(key) {
  const url = process.env.VERCEL_KV_REST_API_URL;
  const token = process.env.VERCEL_KV_REST_API_TOKEN;
  if (url && token) {
    try {
      const k = encodeURIComponent(`otp_send:${key}`);
      const res = await fetch(`${url}/incr/${k}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      const count = Number((await res.json()).result);
      if (count === 1) {
        await fetch(`${url}/expire/${k}/${RL_WINDOW_S}`, {
          headers: { Authorization: `Bearer ${token}` },
        });
      }
      if (Number.isFinite(count)) return count > RL_MAX;
    } catch {
      // KV unreachable: fall through to the in-memory cap rather than failing open.
    }
  }
  return rateLimited(key);
}

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
    <svg width="32" height="32" viewBox="0 0 120 120" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="2" y="2" width="116" height="116" rx="25" fill="#0a0a0c" stroke="#2c7d91" stroke-opacity=".55" stroke-width="3"/><path d="M40 84 L40 55 A20 20 0 0 1 80 55 L80 84" fill="none" stroke="#4db8d4" stroke-width="15" stroke-linecap="round" stroke-linejoin="round"/></svg>
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
    <a href="https://getnable.com" style="color:#1a1915;">getnable.com</a> &middot;
    <a href="mailto:chaaandannn@gmail.com" style="color:#8b8879;">chaaandannn@gmail.com</a>
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

  // Rate limit by caller IP and by target inbox. 200 on the email-keyed
  // limit so the response does not reveal whether the address is known.
  if ((await kvRateLimited(`ip:${ip}`)) || (await kvRateLimited(`em:${email}`))) {
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

  // Generate deterministic 6-digit OTP from HMAC(secret, email:bucket).
  // Because it's derived, not random, verify-code.js can recompute it
  // without any KV store. Valid for a 10-minute bucket window.
  const timeBucket = Math.floor(Date.now() / 600000);
  const mac = await hmacHex(ACCOUNT_SECRET, `otp:${email}:${timeBucket}`);
  const code = (parseInt(mac.slice(0, 8), 16) % 900000 + 100000).toString();

  if (RESEND_KEY) {
    try {
      const res = await fetch(`${RESEND_API}/emails`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${RESEND_KEY}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          from: "nable <hello@getnable.com>",
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
