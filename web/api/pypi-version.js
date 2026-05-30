export default async function handler(req, res) {
  try {
    const r = await fetch("https://pypi.org/pypi/finops-mcp/json");
    if (!r.ok) return res.status(r.status).json({ error: "upstream error" });
    const data = await r.json();
    const version = data.info?.version;
    res.setHeader("Cache-Control", "s-maxage=3600, stale-while-revalidate=300");
    return res.status(200).json({ version });
  } catch {
    return res.status(500).json({ error: "fetch failed" });
  }
}
