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
  .card{max-width:560px;margin:0 auto;background:#fff;border-radius:14px;overflow:hidden;border:1px solid #e8e8e4}
  .header{background:#1a1915;padding:26px 40px;display:flex;align-items:center;gap:10px}
  .wordmark{color:#fbfaf7;font-size:17px;font-weight:600}
  .body{padding:36px 40px}
  h1{margin:0 0 8px;font-size:21px;font-weight:700;color:#1a1915;line-height:1.3}
  .sub{margin:0 0 32px;font-size:14px;line-height:1.65;color:#6b6b65}
  .step{margin-bottom:28px}
  .step-header{display:flex;align-items:center;gap:10px;margin-bottom:10px}
  .step-num{min-width:24px;height:24px;border-radius:50%;background:#1a1915;color:#fbfaf7;font-size:11px;font-weight:700;display:inline-flex;align-items:center;justify-content:center;flex-shrink:0}
  .step-label{font-size:14px;font-weight:600;color:#1a1915;margin:0}
  .code{background:#f4f3ef;border-radius:6px;padding:11px 14px;font-family:'JetBrains Mono','SF Mono',monospace;font-size:12.5px;color:#1a1915;margin:0 0 8px;word-break:break-all;display:block}
  .step-note{font-size:12px;color:#9a9a95;line-height:1.55;margin:0}
  .step-note a{color:#1a1915;font-weight:500}
  .aws-steps{background:#f4f3ef;border-radius:8px;padding:14px 16px;margin:8px 0;font-size:12.5px;color:#54524a;line-height:1.7}
  .aws-steps strong{color:#1a1915;display:block;margin-bottom:4px}
  .aws-step-line{padding-left:12px;border-left:2px solid #d4d3cd;margin:3px 0}
  .divider{border:none;border-top:1px solid #f0efe9;margin:28px 0}
  .example-box{background:#f4f3ef;border-radius:8px;padding:16px 18px;margin:0}
  .example-label{font-size:11px;font-weight:600;color:#9a9a95;text-transform:uppercase;letter-spacing:.06em;margin:0 0 10px}
  .example-q{font-size:13px;color:#1a1915;margin:0 0 6px;padding-left:12px;border-left:2px solid #d4d3cd}
  .example-q:last-child{margin-bottom:0}
  .btn{display:inline-block;background:#1a1915;color:#fbfaf7;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;font-size:13.5px;margin-top:24px}
  .footer{padding:20px 40px;border-top:1px solid #f0efe9;font-size:12px;color:#b0afa9;line-height:1.6}
  .footer a{color:#b0afa9}
</style></head>
<body>
<div class="card">
  <div class="header">
    <svg width="24" height="24" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" style="flex-shrink:0">
      <rect width="32" height="32" rx="7" fill="#fbfaf7"/>
      <path d="M9.5 23V11.5h2.6v1.5c.7-1.1 1.9-1.7 3.4-1.7 2.6 0 4.2 1.7 4.2 4.5V23h-2.7v-6.6c0-1.7-.9-2.6-2.4-2.6s-2.5 1-2.5 2.7V23H9.5Z" fill="#1a1915"/>
    </svg>
    <span class="wordmark" style="margin-left:8px">nable</span>
  </div>
  <div class="body">
    <h1>Set up nable in 5 minutes.</h1>
    <p class="sub">Three steps. The wizard handles the rest — no config file editing, no manual env vars.</p>

    <div class="step">
      <div class="step-header">
        <span class="step-num">1</span>
        <p class="step-label">Install and run the setup wizard</p>
      </div>
      <code class="code">pip install finops-mcp &amp;&amp; finops setup</code>
      <p class="step-note">The wizard walks through everything and auto-configures Claude Desktop at the end.</p>
    </div>

    <div class="step">
      <div class="step-header">
        <span class="step-num">2</span>
        <p class="step-label">Create an AWS access key (2 min)</p>
      </div>
      <p class="step-note" style="margin-bottom:8px">When the wizard asks for your AWS key, here's how to get one:</p>
      <div class="aws-steps">
        <strong>In the AWS Console:</strong>
        <div class="aws-step-line"><a href="https://console.aws.amazon.com/iam/home#/users" style="color:#1a1915;font-weight:500">console.aws.amazon.com/iam</a> → Users → your username</div>
        <div class="aws-step-line">Security credentials → Access keys → Create access key</div>
        <div class="aws-step-line">Choose "Other" → Create → copy both values into the wizard</div>
      </div>
      <p class="step-note">If you need a read-only IAM policy first, run <code style="font-family:monospace;font-size:11.5px;background:#f4f3ef;padding:1px 5px;border-radius:4px">finops setup aws --iam-template</code> and it generates one for you.</p>
    </div>

    <div class="step">
      <div class="step-header">
        <span class="step-num">3</span>
        <p class="step-label">Restart Claude Desktop and ask</p>
      </div>
      <code class="code">What are my AWS costs this month?</code>
      <p class="step-note">Also works with Cursor, Windsurf, and VS Code. Once you see a cost breakdown, you're live.</p>
    </div>

    <hr class="divider">

    <div class="example-box">
      <p class="example-label">What to ask once you're in</p>
      <p class="example-q">What drove our AWS bill up last month?</p>
      <p class="example-q">Which team is closest to their budget?</p>
      <p class="example-q">Show me instances I can rightsize right now.</p>
      <p class="example-q">What will our bill look like next month?</p>
    </div>

    <a href="https://getnable.com/docs" class="btn">Full setup guide →</a>
  </div>
  <div class="footer">
    You're getting this because you signed up at getnable.com.
    Reply to this email with any questions.<br>
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

// Simple in-memory rate limiter (resets on cold start — good enough for edge)
const rateLimitMap = new Map();
const RATE_LIMIT_WINDOW_MS = 60 * 60 * 1000; // 1 hour
const RATE_LIMIT_MAX = 5; // max 5 subscribes per IP per hour

function checkRateLimit(ip) {
  const now = Date.now();
  const entry = rateLimitMap.get(ip) || { count: 0, resetAt: now + RATE_LIMIT_WINDOW_MS };

  if (now > entry.resetAt) {
    // Window expired, reset
    entry.count = 0;
    entry.resetAt = now + RATE_LIMIT_WINDOW_MS;
  }

  if (entry.count >= RATE_LIMIT_MAX) {
    return false; // rate limited
  }

  entry.count++;
  rateLimitMap.set(ip, entry);
  return true; // allowed
}

// KV-backed rate limit (global across edge instances) with a hard daily cap.
// The in-memory limiter above only sees one warm instance, so IP-rotating bots
// slip past it; the KV global counter bounds total welcome emails/day no matter
// how many IPs or instances are in play, which stops this endpoint from being
// turned into a spam cannon on our sending domain. Falls back to in-memory when
// KV is not configured or unreachable, so a KV outage never blocks legit signups.
const SUBSCRIBE_DAILY_CAP = Number(process.env.SUBSCRIBE_DAILY_CAP) || 200;
const SUBSCRIBE_IP_HOURLY_CAP = 5;

async function kvRateLimit(ip) {
  const url = process.env.VERCEL_KV_REST_API_URL;
  const token = process.env.VERCEL_KV_REST_API_TOKEN;
  if (!url || !token) return null; // not configured → caller uses in-memory
  const auth = { headers: { Authorization: `Bearer ${token}` } };
  try {
    // Per-IP: cap per hour.
    const ipKey = encodeURIComponent(`sub_rl_ip:${ip}`);
    const ipCount = Number((await (await fetch(`${url}/incr/${ipKey}`, auth)).json()).result);
    if (ipCount === 1) await fetch(`${url}/expire/${ipKey}/3600`, auth);
    if (Number.isFinite(ipCount) && ipCount > SUBSCRIBE_IP_HOURLY_CAP) return { ok: false };

    // Global daily circuit breaker, bounds total sends/day across all IPs.
    const day = new Date().toISOString().slice(0, 10);
    const gKey = encodeURIComponent(`sub_rl_day:${day}`);
    const gCount = Number((await (await fetch(`${url}/incr/${gKey}`, auth)).json()).result);
    if (gCount === 1) await fetch(`${url}/expire/${gKey}/172800`, auth);
    if (Number.isFinite(gCount) && gCount > SUBSCRIBE_DAILY_CAP) return { ok: false };

    return { ok: true };
  } catch {
    return null; // KV error → fall back to in-memory
  }
}

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

  // Rate limiting via IP — edge runtime provides cf headers or x-forwarded-for
  const ip = req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() || "unknown";
  console.log(`subscribe request from ip=${ip}`);

  // Prune stale entries to prevent unbounded memory growth
  if (rateLimitMap.size > 100) {
    const now = Date.now();
    for (const [key, val] of rateLimitMap) {
      if (now > val.resetAt) rateLimitMap.delete(key);
    }
  }

  // Prefer the global KV limiter; fall back to in-memory if KV is unavailable.
  const kvVerdict = await kvRateLimit(ip);
  const allowed = kvVerdict ? kvVerdict.ok : checkRateLimit(ip);
  if (!allowed) {
    return new Response(JSON.stringify({ error: "Too many requests. Try again later." }), {
      status: 429,
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
        reply_to: "chandanirving@gmail.com",
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
