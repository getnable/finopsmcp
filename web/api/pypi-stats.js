// Vercel serverless function — proxies pypistats.org so the browser
// doesn't need to hit a third-party domain (avoids CORS + CSP issues).
// Cache-Control: 6 hours — PyPI stats update daily, no need to hammer it.
export default async function handler(req, res) {
  try {
    const upstream = await fetch(
      "https://pypistats.org/api/packages/finops-mcp/recent",
      { headers: { Accept: "application/json" } }
    );
    if (!upstream.ok) {
      return res.status(upstream.status).json({ error: "upstream error" });
    }
    const data = await upstream.json();
    res.setHeader("Cache-Control", "s-maxage=21600, stale-while-revalidate=3600");
    res.setHeader("Access-Control-Allow-Origin", "https://getnable.com");
    return res.status(200).json(data);
  } catch (err) {
    return res.status(500).json({ error: "fetch failed" });
  }
}
