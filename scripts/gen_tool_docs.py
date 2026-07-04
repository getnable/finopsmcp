#!/usr/bin/env python3
"""Generate the tool reference from server.py.

AST-parses every @mcp.tool() in src/finops/server.py and emits:
  web/tools.json   machine-readable tool index (name, summary, params, pro, category)
  web/tools.html   searchable static reference page, styled per DESIGN.md

Also prints a docstring QA report: tools whose summary is missing or too thin
to render well in an editor's tool picker.

Run:  python3 scripts/gen_tool_docs.py
The page and JSON are committed; re-run whenever tools change.
"""
from __future__ import annotations

import ast
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "src" / "finops" / "server.py"
OUT_JSON = ROOT / "web" / "tools.json"
OUT_HTML = ROOT / "web" / "tools.html"

# Ordered category rules: first regex that matches the tool name wins.
CATEGORY_RULES: list[tuple[str, str]] = [
    (r"forecast", "Forecasting"),
    (r"kubernetes|cluster|helm|workload|namespace|label_costs", "Kubernetes"),
    (r"databricks", "Databricks"),
    (r"azure", "Azure"),
    (r"gcp", "GCP"),
    (r"llm|_ai_|^optimize_ai|bedrock|langfuse|gpu|token|kendra|textract_costs", "AI & LLM spend"),
    (r"policy|estimate_change_cost|estimate_terraform_cost|estimate_helm", "Agent cost gate"),
    (r"budget|credit_status", "Budgets"),
    (r"anomal|alert|notification", "Anomalies & alerts"),
    (r"ticket|_pr$|terraform_tag_fixes", "Tickets & PRs"),
    (r"audit_|scan_|waste|idle|cleanup|graviton|spot|snapstart|multipart|deep_analysis|"
     r"rightsizing|recommend_|nonprod|ecr_cleanup", "Waste & rightsizing"),
    (r"savings|commitment|recommendation|verify_savings|effective_rate", "Savings & commitments"),
    (r"team|scorecard|attribution|unit_economics|business_metrics|tag_cost", "Teams & attribution"),
    (r"org|account|_ou_|top_spending", "Organizations & accounts"),
    (r"export|report|digest|snapshot|dashboard|notion|n8n|tableau|weekly_insight|"
     r"onboarding_email|invoice_emails|subscri", "Reports & exports"),
    (r"view|pin", "Saved views"),
    (r"cost|spend|provider|marketplace|transfer|traffic|benchmark|service|"
     r"slice|drivers|storage_info", "Cost visibility"),
    (r"api_key|profile|vault|whoami|connector|what_can|roi|saas", "Platform & admin"),
]


def categorize(name: str) -> str:
    for pattern, cat in CATEGORY_RULES:
        if re.search(pattern, name):
            return cat
    return "Other"


def first_paragraph(doc: str | None) -> str:
    if not doc:
        return ""
    para = doc.strip().split("\n\n")[0]
    return " ".join(line.strip() for line in para.splitlines()).strip()


def extract_tools() -> list[dict]:
    tree = ast.parse(SERVER.read_text())
    tools: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_tool = any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and d.func.attr == "tool"
            for d in node.decorator_list
        )
        if not is_tool:
            continue

        doc = ast.get_docstring(node)
        params = []
        args = node.args
        n_defaults = len(args.defaults)
        for i, a in enumerate(args.args):
            if a.arg in ("self", "ctx"):
                continue
            required = i < len(args.args) - n_defaults
            ann = ast.unparse(a.annotation) if a.annotation else ""
            params.append({"name": a.arg, "type": ann, "required": required})

        pro = False
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "require_pro"):
                pro = True
                break

        summary = first_paragraph(doc)
        tools.append({
            "name": node.name,
            "summary": summary,
            "params": params,
            "pro": pro,
            "category": categorize(node.name),
            "lineno": node.lineno,
        })
    tools.sort(key=lambda t: (t["category"], t["name"]))
    return tools


CATEGORY_ORDER = [
    "Cost visibility", "Waste & rightsizing", "Savings & commitments",
    "AI & LLM spend", "Agent cost gate", "Kubernetes", "Budgets",
    "Anomalies & alerts", "Forecasting", "Teams & attribution",
    "Organizations & accounts", "Azure", "GCP", "Databricks",
    "Tickets & PRs", "Reports & exports", "Saved views",
    "Platform & admin", "Other",
]


def render_html(tools: list[dict]) -> str:
    by_cat: dict[str, list[dict]] = {}
    for t in tools:
        by_cat.setdefault(t["category"], []).append(t)

    sections = []
    toc = []
    for cat in CATEGORY_ORDER:
        items = by_cat.get(cat)
        if not items:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", cat.lower()).strip("-")
        toc.append(
            f'<a href="#{slug}">{html.escape(cat)}'
            f'<span class="n">{len(items)}</span></a>'
        )
        rows = []
        for t in items:
            req = [p["name"] for p in t["params"] if p["required"]]
            opt = [p["name"] for p in t["params"] if not p["required"]]
            sig_bits = req + [f"{o}?" for o in opt]
            sig = ", ".join(sig_bits[:6]) + (", …" if len(sig_bits) > 6 else "")
            badge = '<span class="pro">PRO</span>' if t["pro"] else ""
            rows.append(f"""
<div class="tool" data-name="{html.escape(t['name'])}" data-text="{html.escape((t['name'] + ' ' + t['summary']).lower())}">
  <div class="t-head"><code class="t-name">{html.escape(t['name'])}</code>{badge}</div>
  <p class="t-sum">{html.escape(t['summary']) or '<em>No description.</em>'}</p>
  {f'<code class="t-sig">({html.escape(sig)})</code>' if sig_bits else ''}
</div>""")
        sections.append(
            f'<section id="{slug}"><h2>{html.escape(cat)} '
            f'<span class="count">{len(items)}</span></h2>{"".join(rows)}</section>'
        )

    total = len(tools)
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tool reference ({total}) · nable docs</title>
<meta name="description" content="Complete reference for all {total} MCP tools in nable: cloud cost visibility, waste audits, AI spend, the agent cost gate, Kubernetes, budgets, and more.">
<link rel="icon" href="/favicon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@100..900&display=swap" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/fontsource/css/geist-mono@latest/index.css" rel="stylesheet">
<style>
:root{{--bg:#000;--bg-1:#0a0a0c;--bg-2:#121214;--line:#232327;--line-2:#2d2d32;
--fg:#f0f2f3;--fg-2:#94a3ab;--fg-3:#56656d;--accent:#4db8d4;--accent-dim:#2c7d91}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--fg);font-family:'Geist',system-ui,sans-serif;line-height:1.55}}
a{{color:var(--accent);text-decoration:none}}
.wrap{{max-width:1060px;margin:0 auto;padding:0 24px}}
header{{border-bottom:1px solid var(--line);padding:18px 0;position:sticky;top:0;background:rgba(0,0,0,.88);backdrop-filter:blur(8px);z-index:5}}
header .wrap{{display:flex;align-items:center;gap:14px}}
.brand{{font-weight:600;letter-spacing:-.02em;color:var(--fg)}}
.crumb{{color:var(--fg-3);font-size:13px}}
.crumb a{{color:var(--fg-2)}}
h1{{font-size:clamp(30px,3.6vw,44px);letter-spacing:-.035em;font-weight:400;line-height:1.08;margin:52px 0 10px}}
.sub{{color:var(--fg-2);max-width:640px;margin-bottom:26px}}
.sub code{{font-family:'Geist Mono',monospace;font-size:.92em;color:var(--fg)}}
#q{{width:100%;max-width:460px;background:var(--bg-1);border:1px solid var(--line);border-radius:6px;
padding:10px 14px;color:var(--fg);font-family:'Geist',sans-serif;font-size:14px;outline:none}}
#q:focus{{border-color:var(--accent-dim)}}
.meta{{color:var(--fg-3);font-size:13px;margin:10px 0 34px}}
.toc{{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 40px}}
.toc a{{border:1px solid var(--line);border-radius:2px;padding:4px 10px;font-size:12.5px;color:var(--fg-2)}}
.toc a:hover{{border-color:var(--line-2);color:var(--fg)}}
.toc .n{{color:var(--fg-3);margin-left:6px}}
section{{margin-bottom:44px}}
h2{{font-size:20px;font-weight:500;letter-spacing:-.02em;padding-bottom:10px;border-bottom:1px solid var(--line);margin-bottom:6px}}
h2 .count{{color:var(--fg-3);font-weight:400;font-size:14px;margin-left:8px}}
.tool{{padding:14px 0;border-bottom:1px solid var(--line)}}
.tool:last-child{{border-bottom:none}}
.t-head{{display:flex;align-items:center;gap:10px}}
.t-name{{font-family:'Geist Mono','JetBrains Mono',monospace;font-size:13.5px;color:var(--accent)}}
.pro{{font-size:10px;font-weight:600;letter-spacing:.08em;color:var(--accent-dim);border:1px solid var(--accent-dim);border-radius:2px;padding:1px 6px}}
.t-sum{{color:var(--fg-2);font-size:14px;margin-top:4px;max-width:820px}}
.t-sig{{display:block;font-family:'Geist Mono',monospace;font-size:12px;color:var(--fg-3);margin-top:5px}}
footer{{border-top:1px solid var(--line);color:var(--fg-3);font-size:13px;padding:28px 0 48px;margin-top:30px}}
.hidden{{display:none}}
</style></head>
<body>
<header><div class="wrap">
  <a class="brand" href="/">nable</a>
  <span class="crumb"><a href="/docs.html">Docs</a> / Tool reference</span>
</div></header>
<main class="wrap">
  <h1>Tool reference</h1>
  <p class="sub">Every tool nable exposes to Claude, Cursor, and any MCP client.
  You never call these by name, just ask, and nable picks the right one.
  This page is generated from the source of <code>finops-mcp</code> on every release.</p>
  <input id="q" type="search" placeholder="Filter {total} tools… (e.g. kubernetes, idle, budget)" autocomplete="off">
  <p class="meta"><span id="shown">{total}</span> of {total} tools · read-only unless marked · PRO = paid tier</p>
  <nav class="toc">{''.join(toc)}</nav>
  {''.join(sections)}
</main>
<footer><div class="wrap">Generated from server.py · <a href="/docs.html">Back to docs</a> · <a href="/tools.json">tools.json</a></div></footer>
<script>
const q=document.getElementById('q'),tools=[...document.querySelectorAll('.tool')],
secs=[...document.querySelectorAll('section')],shown=document.getElementById('shown');
q.addEventListener('input',()=>{{
  const v=q.value.trim().toLowerCase();let n=0;
  tools.forEach(t=>{{const hit=!v||t.dataset.text.includes(v);t.classList.toggle('hidden',!hit);if(hit)n++;}});
  secs.forEach(s=>s.classList.toggle('hidden',!s.querySelector('.tool:not(.hidden)')));
  shown.textContent=n;
}});
</script>
</body></html>"""


def main() -> None:
    tools = extract_tools()
    OUT_JSON.write_text(json.dumps(
        {"count": len(tools), "tools": tools}, indent=1))
    OUT_HTML.write_text(render_html(tools))
    cats: dict[str, int] = {}
    for t in tools:
        cats[t["category"]] = cats.get(t["category"], 0) + 1
    print(f"{len(tools)} tools -> {OUT_HTML.relative_to(ROOT)}, {OUT_JSON.relative_to(ROOT)}")
    for cat in CATEGORY_ORDER:
        if cat in cats:
            print(f"  {cats[cat]:>3}  {cat}")

    weak = [t for t in tools if len(t["summary"]) < 45]
    if weak:
        print(f"\nQA: {len(weak)} tools with thin or missing docstrings:")
        for t in weak:
            print(f"  server.py:{t['lineno']:>5}  {t['name']}  ({len(t['summary'])} chars)")


if __name__ == "__main__":
    main()
