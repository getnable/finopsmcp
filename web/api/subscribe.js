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

export const config = { runtime: 'edge', maxDuration: 10 };

const RESEND_API   = "https://api.resend.com";
const LOOPS_API    = "https://app.loops.so/api/v1";
const POSTHOG_API  = "https://us.i.posthog.com";

// ─── Welcome email HTML ───────────────────────────────────────────────────────

function welcomeHtml(email) {
  return `<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f3ef;margin:0;padding:40px 0}
  .card{max-width:540px;margin:0 auto;background:#fff;border-radius:14px;overflow:hidden;border:1px solid #e8e8e4}
  .header{background:#1a1915;padding:28px 40px;display:flex;align-items:center;gap:10px}
  .glyph{width:30px;height:30px;border-radius:7px;background:#fbfaf7;display:inline-flex;align-items:center;justify-content:center;color:#1a1915;font-weight:800;font-size:15px;line-height:1;vertical-align:middle}
  .wordmark{color:#fbfaf7;font-size:17px;font-weight:600;vertical-align:middle}
  .body{padding:36px 40px}
  h1{margin:0 0 10px;font-size:22px;font-weight:700;color:#1a1915;line-height:1.3}
  .sub{margin:0 0 28px;font-size:15px;line-height:1.6;color:#6b6b65}
  .step{display:flex;gap:14px;margin-bottom:24px;align-items:flex-start}
  .step-num{min-width:26px;height:26px;border-radius:50%;background:#f4f3ef;border:1px solid #e0dfd9;color:#1a1915;font-size:12px;font-weight:700;display:inline-flex;align-items:center;justify-content:center;margin-top:1px;flex-shrink:0}
  .step-body{flex:1}
  .step-label{font-size:14px;font-weight:600;color:#1a1915;margin:0 0 8px}
  .code{background:#f4f3ef;border-radius:6px;padding:11px 14px;font-family:'SF Mono','JetBrains Mono',monospace;font-size:12.5px;color:#1a1915;margin:0;word-break:break-all}
  .step-note{margin:6px 0 0;font-size:12px;color:#9a9a95;line-height:1.5}
  .divider{border:none;border-top:1px solid #f0efe9;margin:28px 0}
  .example-box{background:#f4f3ef;border-radius:8px;padding:16px 18px;margin:0}
  .example-label{font-size:11px;font-weight:600;color:#9a9a95;text-transform:uppercase;letter-spacing:.06em;margin:0 0 10px}
  .example-q{font-size:13px;color:#1a1915;margin:0 0 6px;padding-left:12px;border-left:2px solid #d4d3cd}
  .example-q:last-child{margin-bottom:0}
  .btn{display:inline-block;background:#1a1915;color:#fbfaf7;padding:13px 26px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;margin-top:24px}
  .footer{padding:20px 40px;border-top:1px solid #f0efe9;font-size:12px;color:#b0afa9}
  .footer a{color:#b0afa9}
</style></head>
<body>
<div class="card">
  <div class="header">
    <span class="glyph">n</span>
    <span class="wordmark" style="margin-left:8px">nable</span>
  </div>
  <div class="body">
    <h1>You're two minutes from your first answer.</h1>
    <p class="sub">nable connects your AI editor directly to your cloud billing APIs. Ask questions in plain English, get answers in seconds, no dashboard required.</p>

    <div class="step">
      <span class="step-num">1</span>
      <div class="step-body">
        <p class="step-label">Install</p>
        <div class="code">pip install finops-mcp &amp;&amp; finops setup</div>
      </div>
    </div>

    <div class="step">
      <span class="step-num">2</span>
      <div class="step-body">
        <p class="step-label">Connect your cloud provider</p>
        <div class="code">finops setup aws</div>
        <p class="step-note">Generates a read-only IAM policy. Credentials stay in your OS keyring. Nothing leaves your machine.</p>
      </div>
    </div>

    <div class="step">
      <span class="step-num">3</span>
      <div class="step-body">
        <p class="step-label">Add to your editor's MCP config</p>
        <div class="code">{"mcpServers":{"finops":{"command":"finops-mcp"}}}</div>
        <p class="step-note">Works with Cursor, Claude, Windsurf, and VS Code.</p>
      </div>
    </div>

    <hr class="divider">

    <div class="example-box">
      <p class="example-label">Then ask anything</p>
      <p class="example-q">What drove our AWS bill up 40% last month?</p>
      <p class="example-q">Which team is closest to their budget limit?</p>
      <p class="example-q">Show me instances I can rightsize right now.</p>
      <p class="example-q">What's our Snowflake spend by warehouse?</p>
    </div>

    <a href="https://getnable.com/docs" class="btn">Full setup guide →</a>
  </div>
  <div class="footer">
    You're getting this because you signed up at getnable.com.
    Questions? Reply to this email.<br>
    <a href="https://getnable.com">getnable.com</a> · <a href="mailto:hello@getnable.com">hello@getnable.com</a>
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
    // Bots and scanners probe this endpoint with GET — redirect browsers cleanly
    // rather than serving a raw JSON error page.
    return Response.redirect("https://getnable.com", 302);
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
        subject: "Your finops-mcp setup (2 min)",
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
