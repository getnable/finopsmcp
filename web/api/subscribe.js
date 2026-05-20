/**
 * POST /api/subscribe
 *
 * Captures an email address from any form on getnable.com, then:
 *   1. Adds the contact to Loops.so (triggers the onboarding drip sequence)
 *   2. Sends an immediate welcome email via Resend
 *   3. Identifies the user in PostHog server-side (optional, enriches DAU data)
 *
 * Required env vars (set in Vercel project settings):
 *   RESEND_API_KEY      — from resend.com (free tier: 3k emails/mo)
 *   LOOPS_API_KEY       — from loops.so   (free tier: 1k contacts)
 *
 * Optional:
 *   POSTHOG_API_KEY     — server-side identification for product analytics
 */

export const config = { runtime: "edge" };

const RESEND_API   = "https://api.resend.com";
const LOOPS_API    = "https://app.loops.so/api/v1";
const POSTHOG_API  = "https://us.i.posthog.com";

// ─── Welcome email HTML ───────────────────────────────────────────────────────

function welcomeHtml(email) {
  return `<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f9f9f7;margin:0;padding:40px 0}
  .card{max-width:520px;margin:0 auto;background:#fff;border-radius:12px;padding:40px;border:1px solid #e8e8e4}
  .logo{display:flex;align-items:center;gap:10px;margin-bottom:32px}
  .glyph{width:28px;height:28px;border-radius:7px;background:#1a1915;display:flex;align-items:center;justify-content:center;color:#fbfaf7;font-weight:700;font-size:14px}
  h1{margin:0 0 12px;font-size:22px;font-weight:600;color:#1a1915}
  p{margin:0 0 16px;font-size:15px;line-height:1.6;color:#4a4a45}
  .code{background:#f4f3ef;border-radius:6px;padding:14px 18px;font-family:'JetBrains Mono',monospace;font-size:13px;color:#1a1915;margin:20px 0}
  .btn{display:inline-block;background:#1a1915;color:#fbfaf7;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:500;font-size:14px;margin-top:8px}
  .footer{margin-top:32px;font-size:12px;color:#9a9a95;border-top:1px solid #e8e8e4;padding-top:20px}
</style></head>
<body>
<div class="card">
  <div class="logo"><div class="glyph">n</div><strong style="font-size:16px;color:#1a1915">nable</strong></div>
  <h1>You're in. Here's how to get started.</h1>
  <p>nable connects Claude to your real AWS, GCP, Azure, and SaaS billing data — so you can ask questions in plain English instead of writing SQL or clicking through dashboards.</p>
  <p><strong>Step 1 — install:</strong></p>
  <div class="code">pip install finops-mcp<br>finops setup</div>
  <p><strong>Step 2 — restart Claude Desktop, then ask:</strong></p>
  <div class="code">"What drove our AWS costs up this month?"<br>"Which team is over budget?"<br>"Show me rightsizing opportunities."</div>
  <a href="https://getnable.com/docs" class="btn">Read the setup guide →</a>
  <div class="footer">
    You're receiving this because you signed up at getnable.com.<br>
    <a href="https://getnable.com" style="color:#9a9a95">getnable.com</a> · <a href="mailto:hello@getnable.com" style="color:#9a9a95">hello@getnable.com</a>
  </div>
</div>
</body>
</html>`;
}

// ─── Handler ──────────────────────────────────────────────────────────────────

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "https://getnable.com",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

export default async function handler(req) {
  // Handle CORS preflight
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS_HEADERS });
  }

  if (req.method !== "POST") {
    return new Response(JSON.stringify({ error: "Method not allowed" }), {
      status: 405,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    });
  }

  // Basic rate limiting via IP — edge runtime provides cf headers or x-forwarded-for
  const ip = req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() || "unknown";
  // Log for monitoring; actual hard rate limiting requires Vercel KV or similar
  console.log(`subscribe request from ip=${ip}`);

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
  const source = body.source || "website";       // "free_tier" | "cta" | "footer" | "trial"
  const company = body.company || "";

  if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return new Response(JSON.stringify({ error: "Invalid email" }), {
      status: 400,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    });
  }

  const RESEND_KEY = process.env.RESEND_API_KEY;
  const LOOPS_KEY  = process.env.LOOPS_API_KEY;
  const PH_KEY     = process.env.POSTHOG_API_KEY;

  const results = await Promise.allSettled([

    // 1. Loops — add/update contact, triggers onboarding sequence
    LOOPS_KEY && fetch(`${LOOPS_API}/contacts/create`, {
      method: "POST",
      headers: { Authorization: `Bearer ${LOOPS_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        email,
        source,
        company,
        userGroup: "free",
        subscribed: true,
        // These properties drive conditional steps in the drip sequence
        signupDate: new Date().toISOString().slice(0, 10),
        plan: "free",
      }),
    }),

    // 2. Resend — immediate welcome email
    RESEND_KEY && fetch(`${RESEND_API}/emails`, {
      method: "POST",
      headers: { Authorization: `Bearer ${RESEND_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        from: "nable <hello@getnable.com>",
        to: [email],
        subject: "Get started with nable",
        html: welcomeHtml(email),
      }),
    }),

    // 3. PostHog — server-side identify so website + product events merge
    PH_KEY && fetch(`${POSTHOG_API}/capture/`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        api_key: PH_KEY,
        event: "signed_up",
        distinct_id: email,
        properties: { email, source, company, $set: { email, company, plan: "free" } },
        timestamp: new Date().toISOString(),
      }),
    }),
  ]);

  // Log any failures (visible in Vercel function logs)
  results.forEach((r, i) => {
    if (r.status === "rejected") {
      console.error(`subscribe step ${i} failed:`, r.reason);
    }
  });

  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}
