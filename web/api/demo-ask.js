// Live fallback for the in-browser nable demo (web/demo.html).
//
// The page answers common questions from a canned library client-side. This
// function handles anything novel: it asks Claude Haiku to answer AS nable,
// using ONLY the demo account's data below. The visitor never connects anything
// and no real data is involved, so there is nothing of theirs to leak.
//
// Safe by design:
//   - Returns {answer:null, reason:"disabled"} when ANTHROPIC_API_KEY is unset,
//     so the page degrades to canned-only until you flip it on in Vercel.
//   - Only DEMO_CONTEXT (fixed, fake) is ever sent to the model.
//   - Output is capped, input is length-limited, requests are rate-limited.
//
// Durable cross-instance rate limiting wants Vercel KV / Upstash; the in-memory
// limiter here is per warm instance, which with Haiku + capped output keeps the
// worst case to pennies. Swap _rateLimited for a KV-backed check to harden.

const DEMO_CONTEXT = `
Account: acme-production (AWS only connected). This is fixed demo data.
Spend, last 30 days: $13,703/mo. Projected month-end ~$15,742 (day 27 of 31).
Top cost drivers:
  - Textract: $4,830/mo (highest)
  - Bedrock (AI/LLM): $3,817/mo, driven by an AnalyzeExpense classifier on gpt-4o-class models
  - DocumentDB: $1,906/mo
  - RDS: $740/mo
  - EC2: ~$610/mo (includes 1 stopped Windows Server still billing EBS)
  - CloudWatch Logs: ~$170/mo across 166 log groups with no retention policy
Identified savings: $2,392 - $4,032/mo:
  - Disable Textract in QA/staging/dev (4 textract Lambdas: prd/qa/stg/dev): $960 - $1,900/mo, low effort
  - Route the classifier Lambda to a cheaper model (Haiku): $500 - $1,200/mo, medium effort
  - Buy DocumentDB + RDS Reserved Instances: $925/mo, zero effort (AWS console)
  - Set CloudWatch log retention on 166 groups: $3/mo, low effort
  - Delete the stopped Windows Server EC2 (EBS still billing): $4/mo, zero effort
Efficiency score: 49.5 / 100. Biggest gaps: tag hygiene and commitment coverage.
Textract pricing per 1,000 pages: DetectDocumentText $1.50 (raw text); AnalyzeDocument tables $15, forms $50; AnalyzeExpense $10.
The biggest immediate, zero-risk win is buying the RIs ($925/mo back, no code change).
`.trim();

const SYSTEM = `You are nable, a local-first FinOps copilot, answering inside a public web demo.
You can see exactly ONE demo account, "acme-production". Its data:
${DEMO_CONTEXT}

How to answer:
- Use ONLY the numbers above. Never invent other figures or services.
- Answer like nable: lead with the dollar figure, name the driver, then the concrete fix. Keep it tight: a few short sentences or a short list, under ~150 words.
- If the question is outside this data or not about cost, say in one line what nable would do and which capability handles it (for example "ask nable to open a rightsizing PR" or "connect your OpenAI key to see token spend"), then steer back to the demo.
- This is illustrative demo data. Never claim it is the visitor's real bill.
- Plain sentences. **bold** and \`inline code\` are fine; no markdown headings.`;

const _hits = new Map(); // ip -> [timestamps]; per warm instance only

function _rateLimited(ip) {
  const now = Date.now();
  const windowMs = 10 * 60 * 1000;
  const max = 15; // questions per IP per 10 min
  const arr = (_hits.get(ip) || []).filter((t) => now - t < windowMs);
  arr.push(now);
  _hits.set(ip, arr);
  if (_hits.size > 5000) _hits.clear(); // bound memory on a hot instance
  return arr.length > max;
}

module.exports = async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(405).json({ error: "POST only" });
    return;
  }

  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    res.status(200).json({ answer: null, reason: "disabled" });
    return;
  }

  let body = req.body;
  if (typeof body === "string") {
    try { body = JSON.parse(body); } catch { body = {}; }
  }
  const question = ((body && body.question) || "").toString().slice(0, 500).trim();
  if (!question) {
    res.status(400).json({ error: "empty question" });
    return;
  }

  const ip = ((req.headers["x-forwarded-for"] || "").split(",")[0] || "").trim() || "unknown";
  if (_rateLimited(ip)) {
    res.status(429).json({ answer: null, reason: "rate_limited" });
    return;
  }

  try {
    const r = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-api-key": apiKey,
        "anthropic-version": "2023-06-01",
      },
      body: JSON.stringify({
        model: "claude-haiku-4-5-20251001",
        max_tokens: 500,
        system: SYSTEM,
        messages: [{ role: "user", content: question }],
      }),
    });
    if (!r.ok) {
      res.status(200).json({ answer: null, reason: "upstream_error" });
      return;
    }
    const data = await r.json();
    const answer = (data.content || [])
      .filter((c) => c.type === "text")
      .map((c) => c.text)
      .join("")
      .trim();
    res.status(200).json({ answer: answer || null });
  } catch (e) {
    res.status(200).json({ answer: null, reason: "error" });
  }
};
