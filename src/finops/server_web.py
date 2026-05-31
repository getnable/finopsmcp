"""
nable web dashboard server.

Serves a self-contained HTML dashboard that auto-refreshes every 60 seconds.
Any browser on the same network can access it -- no credentials or installation needed.

Usage:
    finops serve [--port 8080] [--host 0.0.0.0] [--open]
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

log = logging.getLogger(__name__)


# ── Data fetcher ─────────────────────────────────────────────────────────────

async def _fetch_dashboard_data() -> dict[str, Any]:
    """Pull live data from nable connectors. Returns zeros on any error."""
    result: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account_id": "",
        "total_spend_mtd": 0.0,
        "total_spend_last_month": 0.0,
        "delta_pct": 0.0,
        "top_services": [],
        "opportunities_count": 0,
        "opportunities_total_saving": 0.0,
        "savings_achieved_mtd": 0.0,
        "anomalies_open": 0,
        "budget_pct_used": 0.0,
        "recent_opportunities": [],
        "recent_savings": [],
        "error": None,
        "connected_providers": [],
    }

    try:
        from datetime import date, timedelta
        from .connectors.aws import AWSConnector
        from .connectors.azure import AzureConnector
        from .connectors.gcp import GCPConnector
        from .connectors.saas.datadog import DatadogConnector
        from .connectors.saas.snowflake import SnowflakeConnector

        _cloud = {
            "aws": AWSConnector(),
            "azure": AzureConnector(),
            "gcp": GCPConnector(),
        }
        _saas: dict[str, Any] = {}
        try:
            _saas["datadog"] = DatadogConnector()
        except Exception:
            pass
        try:
            _saas["snowflake"] = SnowflakeConnector()
        except Exception:
            pass

        all_connectors = {**_cloud, **_saas}

        # Find configured providers
        configured: dict[str, Any] = {}
        for name, connector in all_connectors.items():
            try:
                if await connector.is_configured():
                    configured[name] = connector
            except Exception:
                pass

        result["connected_providers"] = list(configured.keys())

        if not configured:
            result["error"] = "No providers configured. Run 'finops setup' to connect a provider."
            return result

        # MTD: first of this month to today
        today = date.today()
        mtd_start = today.replace(day=1)
        # Last month: full month
        last_month_end = mtd_start - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)

        async def _sum_costs(start: date, end: date) -> tuple[float, dict[str, float], str]:
            """Returns (total, by_service, account_id)."""
            total = 0.0
            by_service: dict[str, float] = {}
            account_id = ""
            for name, connector in configured.items():
                try:
                    summary = await connector.get_cost_summary(start, end)
                    total += summary.total_usd
                    for svc, amt in summary.by_service.items():
                        by_service[svc] = by_service.get(svc, 0.0) + amt
                    if not account_id and summary.by_account:
                        account_id = next(iter(summary.by_account))
                except Exception:
                    pass
            return total, by_service, account_id

        mtd_total, mtd_services, account_id = await _sum_costs(mtd_start, today)
        last_total, _, _ = await _sum_costs(last_month_start, last_month_end)

        result["account_id"] = account_id
        result["total_spend_mtd"] = round(mtd_total, 2)
        result["total_spend_last_month"] = round(last_total, 2)

        if last_total > 0:
            result["delta_pct"] = round((mtd_total - last_total) / last_total * 100, 1)

        # Top 5 services by MTD spend
        sorted_svcs = sorted(mtd_services.items(), key=lambda x: -x[1])[:5]
        result["top_services"] = [
            {
                "service": svc,
                "amount": round(amt, 2),
                "pct": round(amt / mtd_total * 100, 1) if mtd_total > 0 else 0.0,
            }
            for svc, amt in sorted_svcs
        ]

    except Exception as exc:
        log.warning("Dashboard data fetch failed: %s", exc)
        result["error"] = str(exc)

    # Savings recommendations
    try:
        from .recommendations.savings_tracker import get_summary, list_recommendations
        summary = get_summary(days=30)
        result["opportunities_count"] = summary.get("open_count", 0)
        result["opportunities_total_saving"] = round(
            summary.get("open_monthly_usd", 0.0), 2
        )
        result["savings_achieved_mtd"] = round(
            summary.get("verified_monthly_usd", 0.0) + summary.get("acted_monthly_usd", 0.0),
            2,
        )

        recs = list_recommendations(status="open", limit=5)
        result["recent_opportunities"] = [
            {
                "description": r.get("description", ""),
                "monthly_saving": round(r.get("estimated_monthly_savings_usd", 0.0), 2),
                "resource": r.get("resource_name", r.get("resource_id", "")),
            }
            for r in recs
        ]

        acted = list_recommendations(status="acted_on", limit=5)
        result["recent_savings"] = [
            {
                "description": r.get("description", ""),
                "monthly_saving": round(r.get("estimated_monthly_savings_usd", 0.0), 2),
                "resource": r.get("resource_name", r.get("resource_id", "")),
            }
            for r in acted
        ]
    except Exception as exc:
        log.debug("Savings tracker unavailable: %s", exc)

    # Anomalies
    try:
        from .anomaly.detector import get_open_anomaly_count
        result["anomalies_open"] = get_open_anomaly_count()
    except Exception:
        pass

    # Budget usage
    try:
        from .budget.enforcer import get_budget_usage_pct
        result["budget_pct_used"] = round(get_budget_usage_pct(), 1)
    except Exception:
        pass

    return result


# ── Dashboard HTML ────────────────────────────────────────────────────────────

_DASHBOARD_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>nable dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d0f10;
  --surface:#15191c;
  --surface2:#1c2126;
  --border:#252b30;
  --fg:#e8ecef;
  --fg2:#9ba8b4;
  --fg3:#5a6472;
  --accent:#4db8d4;
  --green:#3dca7e;
  --red:#e05c5c;
  --yellow:#e8a94a;
  --radius-sm:8px;
  --radius:12px;
  --font:'Instrument Sans',system-ui,sans-serif;
}
html,body{background:var(--bg);color:var(--fg);font-family:var(--font);font-size:15px;line-height:1.5;min-height:100vh}
body{padding:24px 16px 48px}
.container{max-width:960px;margin:0 auto}

/* Header */
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:32px;gap:16px;flex-wrap:wrap}
.logo{font-size:18px;font-weight:600;color:var(--fg);letter-spacing:-.01em}
.logo span{color:var(--accent)}
.last-updated{font-size:12px;color:var(--fg3)}

/* Stat cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px}
.card-label{font-size:11px;font-weight:500;color:var(--fg3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
.card-value{font-size:26px;font-weight:600;line-height:1;color:var(--fg)}
.card-sub{font-size:12px;color:var(--fg3);margin-top:6px}
.card-sub.up{color:var(--red)}
.card-sub.down{color:var(--green)}
.card-sub.neutral{color:var(--fg3)}

/* Section */
.section{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px;margin-bottom:16px}
.section-title{font-size:13px;font-weight:600;color:var(--fg2);text-transform:uppercase;letter-spacing:.06em;margin-bottom:16px}

/* Table */
table{width:100%;border-collapse:collapse}
th{font-size:11px;font-weight:500;color:var(--fg3);text-align:left;padding:0 0 8px;text-transform:uppercase;letter-spacing:.04em}
th:last-child,td:last-child{text-align:right}
td{padding:10px 0;border-top:1px solid var(--border);font-size:14px;color:var(--fg)}
.bar-cell{width:120px}
.bar-bg{height:4px;background:var(--surface2);border-radius:2px;overflow:hidden}
.bar-fill{height:100%;background:var(--accent);border-radius:2px;transition:width .4s}

/* Opportunity list */
.opp-list{list-style:none}
.opp-list li{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:10px 0;border-top:1px solid var(--border);font-size:14px}
.opp-list li:first-child{border-top:none;padding-top:0}
.opp-desc{color:var(--fg);flex:1}
.opp-saving{color:var(--green);font-weight:500;white-space:nowrap}

/* Error banner */
.error-banner{background:#1f1515;border:1px solid #3d2020;border-radius:var(--radius-sm);padding:12px 16px;color:var(--red);font-size:13px;margin-bottom:16px}

/* Footer */
footer{text-align:center;margin-top:40px;font-size:12px;color:var(--fg3)}
footer a{color:var(--fg3);text-decoration:none}
footer a:hover{color:var(--accent)}

/* Responsive */
@media(max-width:480px){
  .card-value{font-size:22px}
  body{padding:16px 12px 40px}
}
</style>
</head>
<body>
<div class="container">
  <header>
    <div class="logo"><span>n</span>able dashboard</div>
    <div class="last-updated" id="ts">Loading...</div>
  </header>

  <div id="error-banner" class="error-banner" style="display:none"></div>

  <div class="cards" id="cards">
    <div class="card">
      <div class="card-label">Spend MTD</div>
      <div class="card-value" id="stat-mtd">...</div>
      <div class="card-sub" id="stat-delta"></div>
    </div>
    <div class="card">
      <div class="card-label">Savings found</div>
      <div class="card-value" id="stat-opp-saving">...</div>
      <div class="card-sub" id="stat-opp-count"></div>
    </div>
    <div class="card">
      <div class="card-label">Savings achieved</div>
      <div class="card-value" id="stat-verified">...</div>
      <div class="card-sub neutral">last 30 days</div>
    </div>
    <div class="card">
      <div class="card-label">Open anomalies</div>
      <div class="card-value" id="stat-anomalies">...</div>
      <div class="card-sub" id="stat-budget"></div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Top services this month</div>
    <table>
      <thead>
        <tr>
          <th>Service</th>
          <th class="bar-cell"></th>
          <th>Amount</th>
          <th>%</th>
        </tr>
      </thead>
      <tbody id="services-body">
        <tr><td colspan="4" style="color:var(--fg3);padding:16px 0">Loading...</td></tr>
      </tbody>
    </table>
  </div>

  <div class="section" id="opps-section">
    <div class="section-title">Open opportunities</div>
    <ul class="opp-list" id="opps-list">
      <li><span style="color:var(--fg3)">Loading...</span></li>
    </ul>
  </div>

  <div class="section" id="savings-section">
    <div class="section-title">Recent savings</div>
    <ul class="opp-list" id="savings-list">
      <li><span style="color:var(--fg3)">Loading...</span></li>
    </ul>
  </div>
</div>

<footer>
  Powered by <a href="https://getnable.com" target="_blank" rel="noopener">nable</a>
</footer>

<script>
function fmt(n){
  if(n>=1000000) return '$'+(n/1000000).toFixed(1)+'M';
  if(n>=1000) return '$'+(n/1000).toFixed(1)+'k';
  return '$'+n.toLocaleString('en-US',{minimumFractionDigits:0,maximumFractionDigits:0});
}

async function refresh(){
  try{
    const r=await fetch('/api/data');
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d=await r.json();

    // Timestamp
    const ts=new Date(d.generated_at);
    document.getElementById('ts').textContent='Updated '+ts.toLocaleTimeString();

    // Error banner
    const banner=document.getElementById('error-banner');
    if(d.error){
      banner.textContent=d.error;
      banner.style.display='block';
    } else {
      banner.style.display='none';
    }

    // Stat cards
    document.getElementById('stat-mtd').textContent=fmt(d.total_spend_mtd||0);
    const delta=d.delta_pct||0;
    const deltaEl=document.getElementById('stat-delta');
    if(delta>0){
      deltaEl.textContent='+'+delta.toFixed(1)+'% vs last month';
      deltaEl.className='card-sub up';
    } else if(delta<0){
      deltaEl.textContent=delta.toFixed(1)+'% vs last month';
      deltaEl.className='card-sub down';
    } else {
      deltaEl.textContent='vs last month';
      deltaEl.className='card-sub neutral';
    }

    document.getElementById('stat-opp-saving').textContent=fmt(d.opportunities_total_saving||0);
    document.getElementById('stat-opp-count').textContent=(d.opportunities_count||0)+' opportunities';
    document.getElementById('stat-verified').textContent=fmt(d.savings_achieved_mtd||0);

    const anom=d.anomalies_open||0;
    document.getElementById('stat-anomalies').textContent=anom;
    const budgetEl=document.getElementById('stat-budget');
    if(d.budget_pct_used>0){
      budgetEl.textContent='Budget '+d.budget_pct_used.toFixed(0)+'% used';
      budgetEl.className=d.budget_pct_used>=80?'card-sub up':'card-sub neutral';
    } else {
      budgetEl.textContent='';
    }

    // Services table
    const tbody=document.getElementById('services-body');
    if(d.top_services&&d.top_services.length>0){
      tbody.innerHTML=d.top_services.map(s=>`
        <tr>
          <td>${s.service}</td>
          <td class="bar-cell">
            <div class="bar-bg"><div class="bar-fill" style="width:${s.pct}%"></div></div>
          </td>
          <td>${fmt(s.amount)}</td>
          <td>${s.pct.toFixed(1)}%</td>
        </tr>
      `).join('');
    } else {
      tbody.innerHTML='<tr><td colspan="4" style="color:var(--fg3);padding:16px 0">No data available</td></tr>';
    }

    // Opportunities
    const oppsList=document.getElementById('opps-list');
    if(d.recent_opportunities&&d.recent_opportunities.length>0){
      oppsList.innerHTML=d.recent_opportunities.map(o=>`
        <li>
          <span class="opp-desc">${o.description||o.resource}</span>
          <span class="opp-saving">${fmt(o.monthly_saving)}/mo</span>
        </li>
      `).join('');
    } else {
      oppsList.innerHTML='<li><span style="color:var(--fg3)">No open opportunities. Run a waste scan to surface recommendations.</span></li>';
    }

    // Savings
    const savingsList=document.getElementById('savings-list');
    if(d.recent_savings&&d.recent_savings.length>0){
      savingsList.innerHTML=d.recent_savings.map(s=>`
        <li>
          <span class="opp-desc">${s.description||s.resource}</span>
          <span class="opp-saving">${fmt(s.monthly_saving)}/mo</span>
        </li>
      `).join('');
    } else {
      savingsList.innerHTML='<li><span style="color:var(--fg3)">No acted-on savings yet.</span></li>';
    }

  } catch(err){
    document.getElementById('ts').textContent='Failed to load data';
    console.error(err);
  }
}

refresh();
setInterval(()=>location.reload(),60000);
</script>
</body>
</html>
"""


# ── HTTP request handler ──────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # type: ignore[override]
        log.debug("web: " + format, *args)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            self._send(200, "text/html; charset=utf-8", _DASHBOARD_HTML.encode())
        elif path == "/health":
            body = json.dumps({"status": "ok"}).encode()
            self._send(200, "application/json", body)
        elif path == "/api/data":
            try:
                loop = asyncio.new_event_loop()
                data = loop.run_until_complete(_fetch_dashboard_data())
                loop.close()
            except Exception as exc:
                data = {"error": str(exc), "generated_at": datetime.now(timezone.utc).isoformat()}
            body = json.dumps(data).encode()
            self._send(200, "application/json", body)
        else:
            self._send(404, "text/plain", b"Not found")


# ── Local IP detection ────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Best-effort local network IP. Falls back to 127.0.0.1."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


# ── Server start helpers ──────────────────────────────────────────────────────

def _make_server(host: str, port: int) -> HTTPServer:
    """Create an HTTPServer, incrementing port on conflict."""
    for attempt in range(10):
        try:
            server = HTTPServer((host, port + attempt), _Handler)
            if attempt > 0:
                log.info("Port %d in use, using %d instead.", port, port + attempt)
            return server
        except OSError:
            continue
    raise OSError(f"Could not bind to any port in range {port}-{port + 9}")


def start_server_background(host: str = "0.0.0.0", port: int = 8080) -> tuple[HTTPServer, int]:
    """Start the dashboard server in a daemon background thread."""
    server = _make_server(host, port)
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, actual_port


def run_server(host: str = "0.0.0.0", port: int = 8080, open_browser: bool = False) -> None:
    """Run the dashboard server in the foreground (blocking)."""
    server = _make_server(host, port)
    actual_port = server.server_address[1]
    local_ip = _local_ip()

    print(f"\n  nable dashboard running at http://{host}:{actual_port}")
    if host == "0.0.0.0":
        print(f"  Share this URL with your team: http://{local_ip}:{actual_port}")
    print("  Press Ctrl+C to stop.\n")

    if open_browser:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{actual_port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        server.shutdown()
