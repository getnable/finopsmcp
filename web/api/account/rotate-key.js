/**
 * POST /api/account/rotate-key
 *
 * Verifies a session token, generates a new license key, and emails it.
 * Only available to Pro subscribers.
 *
 * Required env vars:
 *   ACCOUNT_SECRET        -- must match verify-code.js
 *   FINOPS_LICENSE_PRIVATE_KEY -- Ed25519 seed (base64url) for signing license keys
 *   RESEND_API_KEY        -- from resend.com
 */

export const config = { runtime: "edge" };

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "https://getnable.com",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

const RESEND_API = "https://api.resend.com";

// ── Crypto helpers ────────────────────────────────────────────────────────────

const rotMap = new Map();
function rotationLimited(email) {
  const now = Date.now();
  const entry = rotMap.get(email) || { count: 0, resetAt: now + 3600000 };
  if (now > entry.resetAt) {
    entry.count = 0;
    entry.resetAt = now + 3600000;
  }
  if (entry.count >= 3) return true;
  entry.count += 1;
  rotMap.set(email, entry);
  return false;
}

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

// ── License key generation (v2, Ed25519, mirrors license.py) ─────────────────
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

async function generateLicenseKey(email, plan = "pro") {
  const d = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  const payload = b64url(JSON.stringify({ e: email, d, p: plan }));
  const seed = b64urlToBytes(process.env.FINOPS_LICENSE_PRIVATE_KEY);
  const pkcs8 = new Uint8Array(ED25519_PKCS8_PREFIX.length + seed.length);
  pkcs8.set(ED25519_PKCS8_PREFIX);
  pkcs8.set(seed, ED25519_PKCS8_PREFIX.length);
  const key = await crypto.subtle.importKey("pkcs8", pkcs8, { name: "Ed25519" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("Ed25519", key, new TextEncoder().encode(`2:${payload}`));
  return `FINOPS-2-${payload}-${bytesToB64url(new Uint8Array(sig))}`;
}

// ── Rotation notification email ────────────────────────────────────────────────

async function sendRotationEmail(to, licenseKey, resendKey) {
  const html = `<!DOCTYPE html>
<html>
<head><meta charset="utf-8"/></head>
<body style="margin:0;padding:0;background:#fbfaf7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="max-width:480px;margin:48px auto;padding:0 24px 48px;">

  <div style="margin-bottom:32px;">
    <svg width="32" height="32" viewBox="0 0 120 120" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="2" y="2" width="116" height="116" rx="25" fill="#0a0a0c" stroke="#2c7d91" stroke-opacity=".55" stroke-width="3"/><path d="M40 84 L40 55 A20 20 0 0 1 80 55 L80 84" fill="none" stroke="#4db8d4" stroke-width="15" stroke-linecap="round" stroke-linejoin="round"/></svg>
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

  <div style="margin-bottom:16px;">
    <p style="font-size:13px;color:#54524a;margin:0 0 8px;">
      <strong style="color:#1a1915;">Step 1, </strong>Set it in your environment:
    </p>
    <div style="background:#ebe8e0;border-radius:7px;padding:12px 16px;">
      <code style="font-family:'JetBrains Mono','Courier New',monospace;font-size:12px;color:#1a1915;word-break:break-all;">
        FINOPS_LICENSE_KEY=${licenseKey}
      </code>
    </div>
  </div>

  <div style="margin-bottom:24px;">
    <p style="font-size:13px;color:#54524a;margin:0 0 8px;">
      <strong style="color:#1a1915;">Step 2, </strong>Update the key in your Claude Desktop config (<code style="font-family:'JetBrains Mono','Courier New',monospace;font-size:11.5px;">~/Library/Application Support/Claude/claude_desktop_config.json</code>):
    </p>
    <div style="background:#ebe8e0;border-radius:7px;padding:12px 16px;">
      <code style="font-family:'JetBrains Mono','Courier New',monospace;font-size:12px;color:#1a1915;word-break:break-all;">
        "env": {<br/>
        &nbsp;&nbsp;"FINOPS_LICENSE_KEY": "${licenseKey}"<br/>
        }
      </code>
    </div>
  </div>

  <p style="font-size:13px;color:#8b8879;line-height:1.6;margin:0;">
    If you did not request this rotation, contact
    <a href="mailto:hello@getnable.com" style="color:#1a1915;">hello@getnable.com</a> immediately.
  </p>

  <hr style="border:none;border-top:1px solid #e6e2d6;margin:28px 0 20px;"/>
  <p style="font-size:12px;color:#8b8879;margin:0;line-height:1.6;">
    <a href="https://getnable.com" style="color:#1a1915;">getnable.com</a> &middot;
    <a href="https://getnable.com/account" style="color:#8b8879;">Manage billing</a>
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
      from: "nable <hello@getnable.com>",
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
  const LICENSE_PRIVATE_KEY = process.env.FINOPS_LICENSE_PRIVATE_KEY;
  const RESEND_KEY = process.env.RESEND_API_KEY;

  if (!ACCOUNT_SECRET || !LICENSE_PRIVATE_KEY) {
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

  // A session holder can still loop this and flood their own inbox or burn
  // Resend quota; cap rotations per email per hour.
  if (rotationLimited(email)) {
    return new Response(
      JSON.stringify({ error: "Too many rotations. Try again in an hour." }),
      {
        status: 429,
        headers: { "Content-Type": "application/json", ...CORS_HEADERS },
      }
    );
  }

  if (plan !== "pro" && plan !== "team" && plan !== "trial") {
    return new Response(
      JSON.stringify({ error: "Key rotation requires a Pro or Team subscription." }),
      {
        status: 403,
        headers: { "Content-Type": "application/json", ...CORS_HEADERS },
      }
    );
  }

  let license_key;
  try {
    license_key = await generateLicenseKey(email, plan === "team" ? "team" : "pro");
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
