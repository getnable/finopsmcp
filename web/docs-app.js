/* ─────────────────────────────────────────────────────────────
   nable docs — data-driven sections + interactions
   Palette/markup match the inline sections in docs.html
   ───────────────────────────────────────────────────────────── */

/* ── icons ── */
const ICONS = {
  note: '<svg class="cico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="8"/></svg>',
  tip:  '<svg class="cico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 2.1V18h6v-1.2c0-.8.4-1.6 1-2.1A7 7 0 0 0 12 2z"/></svg>',
  warn: '<svg class="cico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12" y2="17"/></svg>',
};
const copyBtn = '<button class="code-copy" data-copy>Copy</button>';

/* small helper to build a code block */
function code(fname, body){
  return `<div class="code" data-code><div class="code-head"><span class="code-fname">${fname}</span>${copyBtn}</div><pre><code>${body}</code></pre></div>`;
}
function envBlock(lines){
  const body = lines.map(([k,v,c]) =>
    `<span class="t-key">${k}</span>=<span class="t-str">${v}</span>${c?`  <span class="t-dim"># ${c}</span>`:''}`
  ).join('\n');
  return code('.env', body);
}

/* provider logo, tinted monochrome via CSS mask. langfuse has no file → pbadge fallback */
function plogo(file){
  if(!file) return '';
  return `<span class="plogo" style="--m:url(/logos/${file})"></span>`;
}

/* a function-reference card: rows of [tool, description] */
function fnCard(rows, minw){
  const w = minw || 220;
  const body = rows.map(([fn, desc]) =>
    `<div class="field-row"><span class="fn" style="min-width:${w}px">${fn}</span><span class="fd">${desc}</span></div>`
  ).join('');
  return `<div class="ref-card"><div class="field-list">${body}</div></div>`;
}
/* an "Ask nable" card: rows of [question, hint] */
function askCard(rows){
  const body = rows.map(([q, h]) =>
    `<div class="field-row"><span class="fd" style="color:var(--fg)">&ldquo;${q}&rdquo;</span><span class="fd">${h}</span></div>`
  ).join('');
  return `<div class="ref-card"><div class="ask-label">Ask nable</div><div class="field-list">${body}</div></div>`;
}
/* a two-column "pattern → why" table */
function patternTable(headL, headR, rows, lw){
  const w = lw || 200;
  const head = `<div class="pt-head" style="grid-template-columns:${w}px 1fr"><span>${headL}</span><span>${headR}</span></div>`;
  const body = rows.map(([k,v]) =>
    `<div class="pt-row" style="grid-template-columns:${w}px 1fr"><div class="pt-k">${k}</div><div class="pt-v">${v}</div></div>`
  ).join('');
  return `<div class="ptable">${head}<div class="pt-body">${body}</div></div>`;
}

/* ── SaaS connectors ── (env vars verified against the finops-mcp implementation) */
const SAAS = [
  { id:'datadog', name:'Datadog', logo:'datadog.svg', crumb:'SaaS tools',
    blurb:'Real cost data via the Usage Metering API v2 — host counts, APM hosts, log ingestion, and dollar amounts where available. Supports the EU site.',
    steps:[
      ['Go to <strong>Organization Settings → API Keys</strong> → New Key. Name it <code class="ic">finops-mcp</code>.'],
      ['Go to <strong>Organization Settings → Application Keys</strong> → New Key.'],
      ['Run the wizard:', code('terminal','<span class="t-cmd">finops setup datadog</span>')],
    ],
    env:[['DATADOG_API_KEY','your-api-key'],['DATADOG_APP_KEY','your-app-key'],['DATADOG_SITE','datadoghq.com','or datadoghq.eu']] },

  { id:'snowflake', name:'Snowflake', logo:'snowflake.svg', crumb:'SaaS tools',
    blurb:'Queries <code class="ic">ACCOUNT_USAGE.METERING_HISTORY</code> for real credit consumption. Set your contract credit price to convert to USD. Read-only role.',
    steps:[
      ['Create a read-only role and user:', code('sql','<span class="t-cmd">GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE finops_role;</span>')],
      ['Run:', code('terminal','<span class="t-cmd">finops setup snowflake</span>')],
    ],
    env:[['SNOWFLAKE_ACCOUNT','xy12345.us-east-1'],['SNOWFLAKE_USER','finops_user'],['SNOWFLAKE_PASSWORD','your-password'],['SNOWFLAKE_WAREHOUSE','COMPUTE_WH'],['SNOWFLAKE_CREDIT_PRICE','3.00','your contract rate per credit'],['SNOWFLAKE_ROLE','ACCOUNTADMIN','optional, defaults to ACCOUNTADMIN']],
    callout:['note','Snowflake: "Object does not exist" means the role needs <code>IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE</code>. Run as ACCOUNTADMIN: <code>GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE your_role;</code>'] },

  { id:'github', name:'GitHub', logo:'github.svg', crumb:'SaaS tools',
    blurb:'Returns paid Actions minutes used and Copilot seat counts across your org via the billing endpoints. Requires org-level access.',
    steps:[
      ['Go to <strong>Settings → Developer settings → Personal access tokens → Fine-grained tokens</strong>. Select your org. Set permissions <code class="ic">read:billing</code> and <code class="ic">read:org</code>.'],
      ['Run:', code('terminal','<span class="t-cmd">finops setup github</span>')],
    ],
    env:[['GITHUB_TOKEN','github_pat_...'],['GITHUB_ORGS','your-org-name']] },

  { id:'stripe', name:'Stripe', logo:'stripe.svg', crumb:'SaaS tools',
    blurb:'Returns actual fees paid to Stripe via the Balance Transactions API, so SaaS revenue infrastructure shows up next to your cloud costs. Restricted read-only key, no write access needed.',
    steps:[
      ['Go to <strong>Stripe Dashboard → Developers → API Keys → Create restricted key</strong>. Enable <code class="ic">Balance → Read</code> only.'],
      ['Run:', code('terminal','<span class="t-cmd">finops setup stripe</span>')],
    ],
    env:[['STRIPE_SECRET_KEY','sk_live_...']] },

  { id:'twilio', name:'Twilio', logo:'twilio.svg', crumb:'SaaS tools',
    blurb:'Paginated usage records with real billing amounts for messaging, voice, and numbers. Uses your main Account SID and Auth Token.',
    steps:[
      ['Find your Account SID and Auth Token on the <strong>Twilio Console homepage</strong>.'],
      ['Run:', code('terminal','<span class="t-cmd">finops setup twilio</span>')],
    ],
    env:[['TWILIO_ACCOUNT_SID','ACxxxxxx'],['TWILIO_AUTH_TOKEN','your-auth-token']] },

  { id:'mongodb', name:'MongoDB Atlas', logo:'mongodb.svg', crumb:'SaaS tools',
    blurb:'Reads the Atlas Invoice API for cluster, backup, and data-transfer spend by project, with a line-item breakdown. Uses Digest Auth with an org-level API key.',
    steps:[
      ['Go to <strong>Atlas → Access Manager → Organization Access → API Keys → Create API Key</strong>. Role: <em>Organization Billing Viewer</em>.'],
      ['Run:', code('terminal','<span class="t-cmd">finops setup mongodb</span>')],
    ],
    env:[['MONGODB_ATLAS_PUBLIC_KEY','your-public-key'],['MONGODB_ATLAS_PRIVATE_KEY','your-private-key'],['MONGODB_ATLAS_ORG_IDS','your-org-id']] },

  { id:'cloudflare', name:'Cloudflare', logo:'cloudflare.svg', crumb:'SaaS tools',
    blurb:'Billing history and active subscriptions via the Cloudflare API, across zones and Workers. Scoped API token, read-only.',
    steps:[
      ['Go to <strong>Cloudflare Dashboard → My Profile → API Tokens → Create Token</strong>. Use the "Read billing info" template.'],
      ['Run:', code('terminal','<span class="t-cmd">finops setup cloudflare</span>')],
    ],
    env:[['CLOUDFLARE_API_TOKEN','your-api-token'],['CLOUDFLARE_ACCOUNT_ID','your-account-id']] },

  { id:'vercel', name:'Vercel', logo:'vercel.svg', crumb:'SaaS tools',
    blurb:'Invoice API with line items — bandwidth, builds, and seats by team. Requires a Vercel Enterprise plan; Pro and Hobby plans do not return billing data via the API.',
    steps:[
      ['Go to <strong>Vercel Dashboard → Settings → Tokens → Create</strong>. Scope: Full Account.'],
      ['Run:', code('terminal','<span class="t-cmd">finops setup vercel</span>')],
    ],
    env:[['VERCEL_TOKEN','your-token'],['VERCEL_TEAM_ID','team_xxxxxxx','optional, for team accounts']],
    callout:['warn','Invoice data is only available on <strong>Enterprise plans</strong>. Vercel Pro or Hobby plans will not return billing data via the API.'] },

  { id:'pagerduty', name:'PagerDuty', logo:'pagerduty.svg', crumb:'SaaS tools',
    blurb:'Returns seat count and user data to attribute incident-management spend per team. For actual dollar amounts, pair it with the invoice email parser — PagerDuty does not expose billing amounts via API.',
    steps:[
      ['Go to <strong>My Profile → User Settings → Create API User Token</strong>. Read-only access is sufficient.'],
      ['Set the env var manually, or run <code class="ic">finops setup</code> in interactive mode and select PagerDuty.'],
    ],
    env:[['PAGERDUTY_API_KEY','your-token']] },

  { id:'newrelic', name:'New Relic', logo:'newrelic.svg', crumb:'SaaS tools',
    blurb:'Returns data-ingest volume (GB) and full platform user counts via NerdGraph. Set your contract ingest price to convert to USD.',
    steps:[
      ['Go to <strong>New Relic → API Keys → Create key</strong>. Type: User key.'],
      ['Set the env vars manually, or run <code class="ic">finops setup</code> in interactive mode and select New Relic.'],
    ],
    env:[['NEW_RELIC_API_KEY','NRAK-...'],['NEW_RELIC_ACCOUNT_ID','1234567'],['NEW_RELIC_INGEST_PRICE_PER_GB','0.35','your contract rate per GB'],['NEW_RELIC_FULL_PLATFORM_PRICE','99','contract rate per full-platform user/mo']] },

  { id:'langfuse', name:'Langfuse', logo:null, badge:'lf', crumb:'SaaS tools',
    blurb:'Tracks LLM observability cost via the Langfuse Daily Metrics API — model cost, token usage, and trace volume per project, so AI spend lands beside cloud spend.',
    steps:[
      ['Go to <strong>Langfuse → Settings → API Keys</strong> and create a key pair. Copy the public and secret keys.'],
      ['Run:', code('terminal','<span class="t-cmd">finops setup langfuse</span>')],
    ],
    env:[['LANGFUSE_PUBLIC_KEY','pk-lf-...'],['LANGFUSE_SECRET_KEY','sk-lf-...'],['LANGFUSE_HOST','https://cloud.langfuse.com','optional, defaults to cloud.langfuse.com']] },

  { id:'databricks', name:'Databricks', logo:'databricks.svg', crumb:'SaaS tools',
    blurb:'DBU consumption and cost from the Databricks REST API — cluster-level spend, job costs, workspace totals, idle clusters, and autoscale efficiency. Uses the Billable Usage Download API when an account ID is set, otherwise estimates from cluster uptime and job runs.',
    steps:[
      ['In your workspace, go to <strong>Settings → Developer → Access Tokens → Generate new token</strong>. For service principals, use the OAuth M2M flow.'],
      ['Run:', code('terminal','<span class="t-cmd">finops setup databricks</span>')],
    ],
    env:[['DATABRICKS_HOST','https://adb-123.1.azuredatabricks.net'],['DATABRICKS_TOKEN','dapi...'],['DATABRICKS_ACCOUNT_ID','your-account-id','optional: enables exact billing via the Usage Download API'],['DATABRICKS_DBU_PRICE','0.40','optional, override with your contract rate (default $0.40/DBU)']],
    callout:['note','Without <code>DATABRICKS_ACCOUNT_ID</code>, costs are estimated from cluster uptime and job-run duration at list DBU rates. Set the account ID and an account-level token for exact figures from the Billable Usage API.'] },

  { id:'invoices', name:'Invoice emails', logo:null, badge:'inv', crumb:'SaaS tools',
    blurb:'For any vendor with no billing API (PagerDuty, New Relic, GitHub Enterprise, and the rest): nable connects to your billing inbox over IMAP, parses PDF and HTML invoices, and extracts real dollar amounts automatically.',
    steps:[
      ['Enable IMAP on your mailbox (Gmail: Settings → Forwarding and POP/IMAP). Create a dedicated <strong>billing@yourcompany.com</strong> and forward invoices there.'],
      ['On Gmail, create an <strong>App Password</strong> (requires 2FA): Google Account → Security → App passwords.'],
      ['Set the env vars below, or run <code class="ic">finops setup</code> to store them in the vault.'],
    ],
    env:[['FINOPS_IMAP_HOST','imap.gmail.com'],['FINOPS_IMAP_PORT','993'],['FINOPS_IMAP_USER','billing@yourcompany.com'],['FINOPS_IMAP_PASSWORD','your-app-password']] },
];

/* ── Alerts & automation ── */
const ALERTS = [
  { id:'slack', name:'Slack alerts', crumb:'Alerts & automation',
    blurb:'Post anomaly findings and daily cost summaries to a Slack channel via an incoming webhook. No bot scopes needed — the simplest way to put nable in front of your team.',
    steps:[
      ['Go to <strong>api.slack.com/apps → Create app → Incoming Webhooks → Add webhook to workspace</strong>. Select your #finops channel.'],
      ['Copy the webhook URL and add it to your env.'],
    ],
    env:[['SLACK_WEBHOOK_URL','https://hooks.slack.com/services/T.../B.../...']] },

  { id:'teams', name:'Microsoft Teams', crumb:'Alerts & automation',
    blurb:'The same anomaly and summary alerts, delivered to a Teams channel via an Incoming Webhook connector.',
    steps:[
      ['In Teams, go to your channel → <strong>⋯ → Connectors → Incoming Webhook → Configure</strong>. Copy the webhook URL.'],
    ],
    env:[['TEAMS_WEBHOOK_URL','https://yourorg.webhook.office.com/webhookb2/...']] },

  { id:'email', name:'Email digest', crumb:'Alerts & automation',
    blurb:'A standalone HTML email every Monday at 09:00 UTC with last week’s spend, anomalies, and rightsizing recommendations. No AI-client session required; it fires from the scheduler.',
    steps:[
      ['Set the SMTP and recipient env vars below. Digests render and send straight from the scheduler.'],
    ],
    env:[['FINOPS_SMTP_HOST','smtp.gmail.com'],['FINOPS_SMTP_PORT','587'],['FINOPS_SMTP_USER','you@yourcompany.com'],['FINOPS_SMTP_PASSWORD','your-app-password'],['FINOPS_DIGEST_TO','team@yourcompany.com'],['FINOPS_WEEKLY_CRON','"0 9 * * 1"','optional - default Mon 09:00 UTC']],
    pbadge:'Team' },

  { id:'ticketing', name:'Jira / Linear / GitHub', crumb:'Alerts & automation',
    blurb:'Turn a waste finding into a tracked ticket in one step. When a high or medium-severity anomaly is detected, nable opens the issue with the resource, the savings estimate, and a suggested fix. Configure one or more; the first configured is the default.',
    steps:[
      ['<strong>Jira:</strong> create an API token at <strong>id.atlassian.com → Security → API tokens</strong>.'],
      ['<strong>Linear:</strong> create an API key at <strong>Settings → API → Personal API keys</strong>.'],
      ['<strong>GitHub Issues:</strong> a fine-grained token with <code class="ic">issues: write</code> on the target repo.'],
      ['In your editor, ask: <em>"Open a ticket for any EC2 waste over $200/mo."</em>'],
    ],
    env:[['JIRA_URL','https://yourorg.atlassian.net'],['JIRA_EMAIL','you@yourcompany.com'],['JIRA_API_TOKEN','your-api-token'],['JIRA_PROJECT_KEY','FINOPS'],['LINEAR_API_KEY','lin_api_...','if using Linear'],['LINEAR_TEAM_ID','your-team-id','if using Linear'],['GITHUB_ISSUES_REPO','yourorg/finops-alerts','if using GitHub Issues']],
    pbadge:'Team' },

  { id:'slack-bot', name:'Slack bot', crumb:'Alerts & automation',
    blurb:'A persistent bot you can talk to directly in Slack. DM <strong>@nable</strong> or mention it in any channel and it calls the same nable tools, replying in-thread with cost analysis, anomaly summaries, and rightsizing recs. Conversational, not just one-way alerts.',
    steps:[
      ['Install the extra: <code class="ic">pip install finops-mcp[slack]</code>. Go to <strong>api.slack.com/apps → Create app → From scratch</strong>, name it <em>nable</em>.'],
      ['Under <strong>OAuth &amp; Permissions</strong>, add bot scopes <code class="ic">app_mentions:read</code>, <code class="ic">chat:write</code>, <code class="ic">im:history</code>, <code class="ic">im:read</code>. Install and copy the Bot User OAuth Token.'],
      ['Under <strong>Socket Mode</strong>, enable it and generate an App-Level Token with <code class="ic">connections:write</code>. Under <strong>Event Subscriptions</strong>, subscribe to <code class="ic">app_mention</code> and <code class="ic">message.im</code>.'],
      ['Set the env vars and start the bot:', code('terminal','<span class="t-cmd">finops-slack</span>')],
    ],
    env:[['SLACK_BOT_TOKEN','xoxb-...'],['SLACK_APP_TOKEN','xapp-...'],['ANTHROPIC_API_KEY','sk-ant-...'],['SLACK_DAILY_CHANNEL','#finops','optional - sends daily digest at 09:00 UTC']],
    callout:['note','Uses Socket Mode — no public URL or reverse proxy needed. Keep it running in the background (systemd, screen, or Docker). <span class="ic">Team plan</span>.'] },

  { id:'pr-comments', name:'PR cost comments', crumb:'Alerts & automation',
    blurb:'Posts a cost estimate on GitHub pull requests when infrastructure files change (Terraform, CloudFormation, CDK, Helm). The comment updates in place on each push and only fires when the estimated impact exceeds your threshold.',
    steps:[
      ['Install the extra: <code class="ic">pip install finops-mcp[pr-comments]</code>. Create a fine-grained PAT with <code class="ic">pull_requests: write</code> on the target repos.'],
      ['Set the env vars and start the webhook server:', code('terminal','<span class="t-cmd">finops-pr-webhook</span>')],
      ['In your repo, <strong>Settings → Webhooks → Add webhook</strong>. Payload URL <code class="ic">http://your-host:8080/webhook/github</code>, content type <code class="ic">application/json</code>, event <strong>Pull requests</strong>.'],
    ],
    env:[['GITHUB_TOKEN','github_pat_...'],['GITHUB_WEBHOOK_SECRET','your-secret','recommended - validates webhook payloads'],['PR_COST_THRESHOLD_USD','10','skip comment if impact is under $10/mo'],['PR_WEBHOOK_PORT','8080','optional - default 8080']],
    callout:['tip','Cost estimates are factual and account-aware: current instance counts, estimated monthly delta, and your effective discount rate auto-detected from Cost Explorer. No editorial commentary.'] },

  { id:'cleanup', name:'Idle resource cleanup', crumb:'Alerts & automation',
    blurb:'Opt-in destructive remediation for unattached EBS volumes, unassociated Elastic IPs, stale snapshots, stopped EC2 instances, and load balancers with no healthy targets. Off by default; the scan always works and cleanup is dry-run unless you enable it.',
    steps:[
      ['Review what is idle first:', code('claude','<span class="t-cmd">list_idle_resources</span>(resource_types=[<span class="t-str">"ebs"</span>,<span class="t-str">"eip"</span>], min_idle_days=<span class="t-num">90</span>)')],
      ['Preview cleanup (dry-run is the default):', code('claude','<span class="t-cmd">cleanup_idle_resources</span>(resource_ids=[<span class="t-str">"vol-abc123"</span>], dry_run=<span class="t-num">True</span>)')],
      ['Enable destructive actions explicitly, then re-run with <code class="ic">dry_run=False</code>:', code('.env','<span class="t-key">FINOPS_CLEANUP_ENABLED</span>=<span class="t-str">true</span>')],
    ],
    env:[['FINOPS_CLEANUP_ENABLED','true','required for any delete/terminate'],['FINOPS_PROTECTED_TAGS','env=prod,protected=true,keep=yes','comma-separated key=value, never touched']],
    callout:['warn','Every destructive action requires explicit approval in your editor and is appended to <code>~/.finops-mcp/cleanup_audit.jsonl</code>. Resources tagged <code>env=prod</code>, <code>protected=true</code>, <code>do-not-delete=true</code>, or <code>finops-skip=true</code> are never touched. nable never deletes anything on its own.'] },
];

/* ─────────────────────────────────────────────────────────────
   Intelligence — the full capability catalog (16 outcome groups)
   plus the deep audits, anomaly/rightsizing/commitment internals,
   and the security / IAM / SSO reference, all as data.
   ───────────────────────────────────────────────────────────── */
const INTEL = [];

/* ── Overview: What nable does ── */
INTEL.push({ id:'features', name:'What nable does', crumb:'Intelligence',
  blurb:'nable is not just a connector that pipes billing data into an AI. It runs active analysis — anomaly detection, CloudWatch-based rightsizing, waste-pattern scanning, commitment modeling, AI/token unit economics, and forecasting — and surfaces it as 160+ read-only MCP tools your editor can query, reason about, and act on. Everything runs on your machine.',
  body:`<p class="body">You don’t call tools by name. You ask in plain language and nable picks the right one. The map below is grouped by what you’d actually ask, in 16 outcome areas. Each lights up as you connect the provider it needs.</p>
    <div class="cardgrid map-grid">
      <a class="ncard" href="#understand"><div class="nt">Understand your bill</div><div class="nd">Where the money goes, any provider.</div></a>
      <a class="ncard" href="#savings"><div class="nt">Find savings</div><div class="nd">Waste scanners + rightsizing, ranked by $/mo.</div></a>
      <a class="ncard" href="#commitments"><div class="nt">Commitments</div><div class="nd">RI and Savings Plan coverage and effective rate.</div></a>
      <a class="ncard" href="#catch"><div class="nt">Catch surprises</div><div class="nd">Anomalies + two-tier budgets.</div></a>
      <a class="ncard" href="#forecast"><div class="nt">Forecast</div><div class="nd">Per-account spend projection.</div></a>
      <a class="ncard" href="#credits"><div class="nt">Credits &amp; cash cliff</div><div class="nd">Promo-credit burn and the cliff date.</div></a>
      <a class="ncard" href="#ai-cost"><div class="nt" style="color:var(--accent)">AI / LLM cost</div><div class="nd">Token bill by model, unit economics.</div></a>
      <a class="ncard" href="#ai-commitments"><div class="nt" style="color:var(--accent)">AI commitments</div><div class="nd">PTUs, credits, rate cards.</div></a>
      <a class="ncard" href="#ai-forecast"><div class="nt" style="color:var(--accent)">AI forecast &amp; monitor</div><div class="nd">Token forecast, exhaustion date.</div></a>
      <a class="ncard" href="#fix"><div class="nt">Fix it</div><div class="nd">Opens the PR that applies the fix.</div></a>
      <a class="ncard" href="#audit"><div class="nt">Deep AWS audits</div><div class="nd">16 scanners for forgotten line items.</div></a>
      <a class="ncard" href="#gcp-audit"><div class="nt">Deep GCP audits</div><div class="nd">Resource-level GCP waste.</div></a>
      <a class="ncard" href="#azure-intel"><div class="nt">Azure deep dives</div><div class="nd">Advisor, VM rightsizing, reservations.</div></a>
      <a class="ncard" href="#kubernetes"><div class="nt">Kubernetes</div><div class="nd">Namespace cost, efficiency, no agent.</div></a>
      <a class="ncard" href="#saas-intel"><div class="nt">SaaS &amp; data platforms</div><div class="nd">Datadog, Snowflake, Databricks.</div></a>
      <a class="ncard" href="#share"><div class="nt">Share &amp; automate</div><div class="nd">Dashboards, exports, tickets, digests.</div></a>
    </div>
    <p class="body" style="font-size:13px;color:var(--fg-3)">Not sure what your stack unlocks? Ask <code class="ic">what can nable do</code> in your editor and it tailors the map to what you’ve connected.</p>` });

/* ── Understand your bill ── */
INTEL.push({ id:'understand', name:'Understand your bill', crumb:'Intelligence',
  blurb:'Ask anything about where the money goes, across every connected provider. nable breaks spend down by service, team, tag, or provider and explains what changed and why. No SQL, no dashboards.',
  body: askCard([
      ['What’s driving our spend this month?','top cost drivers vs last month, with the why'],
      ['Break down our AWS spend by service','last 30/60/90 days, any provider'],
      ['What do we spend by team or tag?','attribution by your tag rules'],
      ['Compare our cloud providers','AWS vs Azure vs GCP side by side'],
    ]) + fnCard([
      ['get_cost_summary','Total spend summarized by service, account, and region'],
      ['get_costs_by_service','Cost breakdown by service, optionally filtered to a keyword'],
      ['get_costs_by_team','Costs by engineering team using your tag attribution rules'],
      ['get_top_cost_drivers','The top N most expensive services across all providers'],
      ['explain_recent_cost_drivers','What drove cost changes across all providers in the last N days'],
      ['explain_cost_change','What recent cost changes actually mean for the business'],
      ['get_cost_trends','Cost trends over time, broken down by day or month'],
      ['get_cost_history','Historical daily cost for a provider + service, for trend context'],
      ['compare_providers','Side-by-side cost comparison across all configured providers'],
      ['get_total_spend_all_sources','Grand total across cloud + SaaS, your true total tech spend'],
      ['get_focus_costs','Unified cost data in FOCUS 2.0 format across all cloud providers'],
      ['get_tag_cost_breakdown_cur','Break AWS costs by a resource tag using CUR line items via Athena'],
      ['benchmark_costs','Compare your spend profile against anonymised peer-group medians'],
    ], 230) });

/* ── Find savings ── */
INTEL.push({ id:'savings', name:'Find savings', crumb:'Intelligence',
  blurb:'Dozens of waste scanners and rightsizing engines, ranked by dollar impact. nable runs every scanner, sorts by estimated monthly savings, and tracks each recommendation from found to acted-on to verified.',
  body: askCard([
      ['What are our biggest savings opportunities?','runs every scanner, ranked by $/mo'],
      ['Show me rightsizing recommendations','EC2, RDS, Lambda, ECS, EKS'],
      ['Any idle resources to clean up?','idle load balancers, RDS, volumes, IPs'],
      ['What Graviton or spot can we move to?','20-40% on compute, up to 90% on spot'],
    ]) + fnCard([
      ['scan_waste_patterns','Scan for cost waste using nable’s proprietary pattern library'],
      ['run_full_cost_audit','Full optimization audit across all connected AWS resources'],
      ['get_rightsizing_recommendations','EC2 with low CPU over 14 days, with projected monthly savings'],
      ['list_idle_resources','Idle/wasted AWS resources costing money but doing nothing'],
      ['scan_graviton_migration_opportunities','EC2 that can move to Graviton (arm64) for 20-40%, ranked'],
      ['recommend_spot_adoption','On-demand EC2 to move to spot for 60-80%, with interruption data'],
      ['get_savings_summary','Realized-savings dashboard: found, acted on, and verified'],
      ['get_savings_ledger','Clean summary of savings found, acted on, and verified'],
      ['list_savings_recommendations','List tracked recommendations with their current status'],
      ['mark_recommendation_acted_on','Mark a recommendation implemented, queued for verification'],
      ['verify_savings','Auto-verify acted-on recommendations against the real AWS state'],
      ['cleanup_idle_resources','Real action: release idle resources, dry-run first, confirmation required'],
      ['get_nable_roi','ROI: savings found, acted on, verified vs the tool cost'],
      ['get_efficiency_scorecard','0–100 FinOps score across 5 dimensions, tracked over time'],
    ], 250) +
    calloutHTML('note','For the full AWS scanner catalog (gp2→gp3, idle NAT, orphaned snapshots, public IPv4, Lambda concurrency, and more) see <a class="ilink" href="#audit">Deep AWS audits</a>. To turn a recommendation into a PR see <a class="ilink" href="#fix">Fix it</a>.') });

/* ── Commitment analysis ── */
INTEL.push({ id:'commitments', name:'Commitment analysis', crumb:'Intelligence', pbadge:'Team',
  blurb:'Models Savings Plans and Reserved Instance coverage against your actual usage patterns. Shows your current effective discount rate, coverage gaps, and what you’d save by buying more, with ROI projections by term length.',
  body: fnCard([
      ['get_commitment_analysis','Current coverage, gaps, and purchase recommendations'],
      ['get_commitment_coverage_by_tag','Coverage breakdown per team / environment / service tag'],
      ['get_effective_rate_profile','Your blended and unblended effective rate vs on-demand'],
      ['get_savings_plan_showback','SP + RI savings attributed back to each team by tag (CUR required)'],
    ], 220) +
    `<h3 class="sub-h" id="sp-showback" data-toc>Savings Plan showback by team</h3>
    <p class="body">Savings Plans are purchased at the payer account level, so by default there is no way to know which team benefited. Your bill shows one blended rate; CloudHealth, Apptio, and Cost Explorer all approximate the allocation. nable reads two CUR fields most tools ignore:</p>
    ` + code('CUR fields','<span class="t-key">savingsplan_savings_plan_effective_cost</span>  <span class="t-com"># what the resource actually cost under SP rates</span>\n<span class="t-key">pricing_public_on_demand_cost</span>          <span class="t-com"># what it would have cost on-demand</span>') +
    `<p class="body">The difference is the real dollar savings that resource captured from the SP. Grouped by a tag (e.g. <code class="ic">team</code>), each team gets its effective discount rate and savings captured, read straight from your CUR so the attribution is line-item exact.</p>` +
    calloutHTML('warn','<strong>Approximation notice.</strong> Like CloudHealth and Apptio, this is an approximation: family-based SP allocation order and SP amortization timing are not fully deterministic from CUR alone. Results are within ~2–5% of the true allocation, well above Cost-Explorer-blended methods. Treat showback as a strong signal for team accountability, not an accounting-grade ledger entry.') +
    code('Example output','<span class="t-cmd">payments</span>    effective_cost: $8,240   on_demand_equiv: $11,800   <span class="t-key">savings: $3,560 (30.2%)</span>\n<span class="t-cmd">platform</span>    effective_cost: $14,100  on_demand_equiv: $19,300   <span class="t-key">savings: $5,200 (26.9%)</span>\n<span class="t-cmd">__untagged__</span> effective_cost: $2,100   on_demand_equiv: $2,900    savings: $800 (27.6%)') +
    calloutHTML('note','Requires CUR delivery to S3 + Athena. Set <code>CUR_S3_BUCKET</code>, <code>CUR_ATHENA_DATABASE</code>, <code>CUR_ATHENA_TABLE</code>, and <code>CUR_ATHENA_RESULTS_BUCKET</code>.') });

/* ── Catch surprises ── */
INTEL.push({ id:'catch', name:'Catch surprises', crumb:'Intelligence',
  blurb:'Anomaly detection and budgets so a spike never waits for the invoice. nable watches spend daily, attributes a spike to the team or service that drove it, and enforces budgets at two tiers. It also flags AI/Marketplace spend that AWS’s own anomaly detector cannot see.',
  body: askCard([
      ['Why did our bill spike?','anomaly detection with tag attribution'],
      ['Set a budget and alert me at 80%','two-tier: warn at 80%, block at 100%'],
      ['What AI spend is AWS not watching?','Bedrock/Marketplace blind spots'],
    ]) + fnCard([
      ['get_anomalies','Active (unacknowledged) anomalies from historical baselines'],
      ['get_account_anomalies','Accounts that spiked or dropped vs their prior period'],
      ['get_ai_billing_blind_spots','AWS AI/Marketplace spend invisible to AWS anomaly detection'],
      ['set_budget','Create/update a budget; alerts at alert_at_pct, fails CI at block_at_pct'],
      ['check_budget_status','Current spend vs budgets: spent, remaining, warning or exceeded'],
      ['list_budgets','List all configured budgets with limits and scopes'],
      ['set_alert_policy','Custom anomaly alert policy for a provider or service'],
      ['acknowledge_anomaly','Mark an anomaly known so it stops surfacing in digests'],
      ['take_snapshot_now','Manually fetch yesterday’s costs (normally daily at 01:00 UTC)'],
    ], 220) +
    calloutHTML('note','Detection internals (z-score, CUSUM drift, day-of-week seasonal normalisation) and ticket auto-filing are in <a class="ilink" href="#anomalies">Anomaly detection</a>. Slack/Teams alerts fire automatically; configure <code>SLACK_WEBHOOK_URL</code> or <code>TEAMS_WEBHOOK_URL</code>.') });

/* ── Forecast ── */
INTEL.push({ id:'forecast', name:'Forecast', crumb:'Intelligence',
  blurb:'Per-account projection so finance sees the curve early. nable forecasts cloud spend with Holt-Winters time-series modelling (trend plus seasonality), and uses Azure Cost Management’s native forecast for Azure.',
  body: askCard([
      ['Forecast our cloud spend next quarter','trend + seasonality, with confidence band'],
      ['Are we on track to blow the budget?','projected vs budget, by account'],
    ]) + fnCard([
      ['forecast_costs','Forecast future cloud spend using Holt-Winters time-series modelling'],
      ['forecast_azure_costs','Forecast Azure spend using Azure Cost Management’s own forecast model'],
    ], 200) });

/* ── Credits & the cash cliff ── */
INTEL.push({ id:'credits', name:'Credits & the cash cliff', crumb:'Intelligence',
  blurb:'Track promo-credit burn and the day billing flips to real cash. AWS Activate credits hide cost pain until they run out, and AWS sends no native alert. nable estimates runway from observed burn (there is no API for the balance) and warns you before the cliff.',
  body: askCard([
      ['Are our AWS credits about to run out?','runway from observed burn, no API for balance'],
      ['When do credits flip to cash?','the cliff alert AWS never sends'],
    ]) + fnCard([
      ['get_credit_status','Track AWS Activate credit burn-down and detect the flip to cash'],
      ['get_ai_billing_blind_spots','AI/Marketplace spend AWS’s own detector misses (relevant near the cliff)'],
    ], 200) });

/* ── AI / LLM cost ── */
INTEL.push({ id:'ai-cost', name:'AI / LLM cost', crumb:'Intelligence',
  blurb:'The token bill almost nobody watches, by model, with unit economics. nable aggregates spend across OpenAI, Anthropic, AWS Bedrock, Azure OpenAI, Vertex, and LLM gateways, ties it to your business metrics for cost-per-customer and gross-margin impact, and gives you a ranked, dollar-quantified plan to cut it.',
  body: askCard([
      ['What are we spending on tokens, by model?','OpenAI, Anthropic, Bedrock, gateways'],
      ['What’s our cost per customer or per request?','AI unit economics tied to your metrics'],
      ['Where’s the AI waste?','cache hit rate, model sprawl, prompt efficiency'],
      ['Cut our AI spend','model-routing and caching recommendations'],
    ]) + fnCard([
      ['get_llm_costs','Aggregate AI/LLM spend across OpenAI, Anthropic, Bedrock, Azure OpenAI, Vertex'],
      ['get_llm_cost_by_model','Break down AI/LLM cost by model, with efficiency metrics'],
      ['get_llm_unit_economics_full','Cost per customer, MAU, API request, and gross-margin impact'],
      ['get_llm_unit_economics','Cost per unit of business value from AI APIs'],
      ['get_ai_kpis','Full AI cost health dashboard with actionable KPIs'],
      ['optimize_ai_spend','Ranked, dollar-quantified plan to cut your AI/LLM bill across providers'],
      ['recommend_bedrock_model_routing','Bedrock invocations that could route to cheaper models (Sonnet→Haiku)'],
      ['get_bedrock_costs','Break down Amazon Bedrock costs by model and token type'],
      ['get_gpu_infra_costs','Serverless-GPU spend across Modal, Together, and Replicate'],
      ['get_langfuse_model_costs','LLM spend and token usage by model from Langfuse'],
      ['get_langfuse_trace_volume','Daily trace and observation counts from Langfuse'],
      ['set_business_metrics','Store business metrics so nable can tie cost to outcomes'],
      ['get_business_metrics','Return stored business metrics and their trend over time'],
    ], 240) +
    calloutHTML('note','Works once any LLM provider key is configured (OpenAI, Anthropic, OpenRouter, LiteLLM, Modal, Together, Replicate). Bedrock cost also works from an AWS connection alone. See <a class="ilink" href="#langfuse">Langfuse</a> for trace-level model costs.') });

/* ── AI commitments & contracts ── */
INTEL.push({ id:'ai-commitments', name:'AI commitments & contracts', crumb:'Intelligence',
  blurb:'Reserved-Instance analysis for tokens. nable prices your usage against your actual negotiated terms — prepaid credits, Azure OpenAI PTUs, AWS Bedrock Provisioned Throughput, enterprise rate cards — not list price, which a provider dashboard cannot do.',
  body: askCard([
      ['Are we utilizing our Azure PTUs?','utilization, effective $/Mtok vs on-demand'],
      ['Should we buy provisioned throughput?','break-even on your stable token baseline'],
      ['Are we clearing our enterprise minimum?','flags committed volume you paid for unused'],
    ]) + fnCard([
      ['get_llm_commitment_analysis','Optimize token spend against prepaid credits, PTUs, Provisioned Throughput, and rate cards, priced on your actual terms'],
    ], 240) });

/* ── AI forecast & monitor ── */
INTEL.push({ id:'ai-forecast', name:'AI forecast & monitor', crumb:'Intelligence',
  blurb:'Project the token bill and get the credit/commitment exhaustion date. nable forecasts your daily token-cost series (Holt-Winters with linear and naive fallbacks by history length) and runs a daily monitor for spikes, drops, and commitments that need attention.',
  body: askCard([
      ['When will our credits run out at this rate?','exhaustion date from the token forecast'],
      ['Is our token bill accelerating?','projected spend + month-over-month growth'],
      ['Did our token spend spike?','daily anomaly + commitment attention'],
    ]) + fnCard([
      ['forecast_llm_costs','Forecast token spend and, given a balance, the date credits/commitment run out'],
      ['get_ai_spend_monitor','What the daily AI-spend monitor watches: token spike/drop plus commitments'],
    ], 200) +
    calloutHTML('note','The monitor runs daily on the scheduler and alerts via Slack. This tool returns the same view on demand.') });

/* ── Fix it ── */
INTEL.push({ id:'fix', name:'Fix it (close the loop)', crumb:'Intelligence',
  blurb:'Not just recommend, act. nable reads your Terraform state, patches the <code class="ic">.tf</code> source, and opens a GitHub PR that applies the fix. It can also estimate the cost of a Terraform plan or Helm diff before you merge.',
  body: askCard([
      ['Open a PR for the top rightsizing rec','reads tfstate, patches .tf, opens a GitHub PR'],
      ['Apply the missing tag fixes','writes tags straight into your Terraform'],
      ['Estimate the cost of this Terraform plan','diff the bill before you merge'],
    ]) + fnCard([
      ['open_rightsizing_pr','Apply rightsizing recs to Terraform source, optionally opening a GitHub PR'],
      ['audit_terraform_tags','Scan Terraform state for resources missing required tags'],
      ['generate_terraform_tag_fixes','Generate HCL patches for tag violations (diff only, no write)'],
      ['open_terraform_tag_pr','Apply tag fixes to .tf files and open a GitHub PR'],
      ['estimate_terraform_cost','Estimate the monthly AWS cost change from a Terraform plan before applying'],
      ['estimate_helm_diff_cost','Estimate the monthly cost impact of a helm diff or values.yaml change'],
    ], 230) +
    calloutHTML('note','PR creation requires <code>GITHUB_TOKEN</code> and a configured github_repo remote. Tag requirements come from <code>FINOPS_REQUIRED_TAGS</code> (default: team,environment,service).') });

/* ── AWS deep audit (leads with depth) ── */
INTEL.push({ id:'audit', name:'AWS deep audit', crumb:'Intelligence',
  blurb:'Trusted Advisor and Compute Optimizer flag the obvious: low-CPU instances, unattached volumes, idle IPs. nable goes a layer deeper. It correlates multi-day CloudWatch time-series, live resource config, and current AWS pricing to find waste the billing API never itemizes and those tools never surface.',
  body:`<p class="body">The signals below the billing line: an NLB silently charged $0.01/GB because cross-zone is enabled, KMS API calls multiplying on every S3 request because Bucket Keys are off, a custom metric namespace exploding because someone keyed it by <code class="ic">pod_id</code>.</p>
    <p class="body">Every check is multi-signal. It does not flag a low-CPU instance and stop. It joins CPU percentiles (p95/p99) against network egress to skip batch jobs, checks <code class="ic">launch_time</code> so a two-day-old box is not called waste, verifies a backing resource still exists before calling an alarm orphaned, and reads RDS <code class="ic">DatabaseConnections</code> to separate a truly idle database from one merely over-provisioned. When a heuristic and Compute Optimizer both flag the same instance, you get one finding, not two. The whole sweep runs in parallel across every opted-in region (8-worker pool), sorted by estimated monthly savings.</p>
    <h3 class="sub-h" id="audit-below" data-toc>Below the billing API</h3>
    <p class="body">The waste Cost Explorer bundles into a generic line item and Trusted Advisor never scans for. Each one cross-references resource config with a CloudWatch metric to reconstruct a cost AWS does not break out.</p>` +
    patternTable('What we catch','Why AWS misses it', [
      ['NLB cross-zone tax','The $0.01/GB surcharge applies only when cross-zone load balancing is enabled. Cost Explorer rolls it into general EC2 data transfer. We read <code class="ic">cross_zone.enabled</code>, pull <code class="ic">ProcessedBytes</code>, and model the cost at a conservative 50% cross-AZ fraction.'],
      ['EFS cross-AZ mounts','$0.02/GB when an instance mounts EFS from another AZ, buried in EC2 transfer with no per-mount-target breakdown. We match mount-target security groups to instances in other AZs and sum <code class="ic">DataReadIOBytes</code> + <code class="ic">DataWriteIOBytes</code> for an upper-bound estimate.'],
      ['KMS overpay (no Bucket Keys)','SSE-KMS without <code class="ic">BucketKeyEnabled</code> fires GenerateDataKey on every PUT and Decrypt on every GET at $0.03/10k. Cost Explorer does not surface KMS call volume per bucket. We find SSE-KMS buckets with Bucket Keys off and size the up-to-99% reduction from <code class="ic">AllRequests</code>.'],
      ['CloudWatch metric cardinality','Compute Optimizer never enumerates custom namespaces. We sample 20 metrics per namespace, count high-cardinality dimensions (<code class="ic">pod_id</code>, <code class="ic">trace_id</code>, <code class="ic">request_id</code>), and price volume above the 10k free tier at $0.30/metric/month.'],
      ['Cross-region snapshot replicas','AWS Backup does not flag a replicated snapshot whose source volume was deleted, or copies fanned out to more than three regions. We group snapshots by <code class="ic">VolumeId</code> across regions and flag orphaned, excess, and stale copies separately.'],
      ['Orphaned CloudWatch alarms','Neither Compute Optimizer nor Trusted Advisor check whether an alarm points at a deleted resource. We flag alarms in INSUFFICIENT_DATA over 7 days, then confirm the EC2 instance or SQS queue is actually gone before marking safe-to-delete.'],
      ['S3 Transfer Acceleration waste','A $0.04–0.08/GB surcharge AWS never warns is unused. We flag TA buckets moving under 1 GB/month, sitting in us-east-1, or already behind CloudFront.'],
      ['Public IPv4 on stopped boxes','Since Feb 2024, $0.005/hr per address, charged on EIPs attached to stopped instances. We cross-reference <code class="ic">describe_addresses</code> against instance state to catch the ones still billing while parked.'],
    ], 220) +
    `<h3 class="sub-h" id="audit-rate" data-toc>Rate and commitment intelligence</h3>
    <p class="body">Reads CUR line-item fields Cost Explorer does not expose to reconstruct your real economics, not list price.</p>` +
    fnCard([
      ['get_effective_rate_profile','Auto-detects your real discount by comparing OnDemandCostEquivalent (list) against AmortizedCost (paid), surfacing EDP / MOSA / negotiated rates with no manual input. Per-service breakdown, confidence rated on spend volume.'],
      ['get_commitment_coverage_by_tag','Coverage for a tag slice even when tagging is incomplete: measures the tagged portion directly, infers the untagged residual algebraically, blends with a confidence rating.'],
      ['get_ri_waste_detail','Queries RIFee line items via Athena for per-reservation unused upfront and recurring fees, sorted by wasted dollars.'],
      ['get_savings_plan_showback','Attributes actual SP and RI savings back to each team by tag, line-item exact. Method in <a class="ilink" href="#sp-showback">Savings Plan showback</a>.'],
      ['scan_graviton_migration_opportunities','Maps running x86 instances to Graviton equivalents (m5→m7g, c5→c7g) against exact us-east-1 rates, ranked by savings.'],
      ['audit_spot_diversification','Reads each ASG’s MixedInstancesPolicy and scores pool risk by instance-type count and allocation strategy.'],
      ['recommend_database_savings_plans','Isolates RDS / Aurora instance-hour spend, measures coverage, and sizes 1yr/3yr commitments against the uncovered baseline.'],
    ], 240) +
    `<h3 class="sub-h" id="audit-instance" data-toc>Per-instance deep analysis</h3>
    <p class="body">When you want to interrogate a single resource, not run a sweep. <code class="ic">get_instance_deep_analysis</code> pulls a 14-day CloudWatch time-series for one EC2 instance (CPU avg / max / p95 / p99, NetworkIn/Out, DiskRead/Write), compares it to the Compute Optimizer recommendation, and returns its own threshold-based action: stop if avg CPU is under 5%, downsize if avg is under 20% and p99 stays under 50%.</p>
    <h3 class="sub-h" id="audit-scanners" data-toc>Targeted scanners</h3>
    <p class="body">Each below-the-billing-API check is also a standalone scanner the agent can run on its own or compose with the others.</p>` +
    patternTable('Scanner','What it catches', [
      ['audit_nlb_cross_zone_costs','NLBs with cross-zone enabled driving the $0.01/GB inter-AZ surcharge, modeled from ProcessedBytes'],
      ['audit_efs_cross_az_mounts','EFS mounted across AZs, paying inter-AZ transfer on every read (upper-bound estimate)'],
      ['scan_s3_bucket_key_opportunities','SSE-KMS buckets without Bucket Keys, overpaying KMS API calls on every request'],
      ['audit_cloudwatch_metric_cardinality','High-cardinality custom metrics (pod_id, trace_id) inflating the CloudWatch bill'],
      ['audit_cloudwatch_orphaned_alarms','Alarms pointing at deleted resources, verified gone before flagging safe-to-delete'],
      ['audit_cloudwatch_logs_ia_opportunities','Log groups that belong in Infrequent Access storage, sized from 30-day IncomingBytes'],
      ['audit_ebs_snapshot_replication','Cross-region snapshot copies: orphaned, excess (&gt;3 regions), and stale'],
      ['audit_s3_intelligent_tiering','Intelligent-Tiering on small-object buckets where the monitoring fee exceeds savings, plus TA that is not paying off'],
      ['audit_public_ipv4_addresses','Every public IPv4 (billed $0.005/hr since Feb 2024), idle ones and those on stopped instances'],
      ['audit_rds_manual_snapshots','Manual RDS snapshots piling up beyond the automated retention window'],
      ['scan_lambda_concurrency_waste','Idle provisioned concurrency, plus SnapStart cold-start candidates'],
      ['identify_nonprod_scheduling_opportunities','Non-prod that could be parked nights and weekends'],
      ['get_idle_load_balancers','Load balancers with no targets, and databases with no connections'],
    ], 260) +
    `<h3 class="sub-h" id="audit-obvious" data-toc>And the obvious things AWS already nags you about</h3>
    <p class="body">Caught in the same <code class="ic">audit_aws_waste</code> pass, so you do not need a second tool for the basics. These are the Trusted Advisor freebies; they ride along, they do not define the audit.</p>` +
    patternTable('Pattern','What it catches', [
      ['gp2 → gp3','EBS volumes on gp2. Same performance, 20% cheaper on gp3'],
      ['Unattached EBS','Volumes not mounted to any instance, paying for provisioned GB'],
      ['Orphaned snapshots','EBS snapshots &gt;30 days old with no AMI reference'],
      ['Idle NAT Gateways','&lt;1 GB/day throughput over 7 days. $32/mo base charge wasted'],
      ['ChargedBackup','RDS backup retention &gt;7 days, accumulates silently'],
      ['CloudTrail data events','Data events ($2/100k) enabled across all regions unnecessarily'],
      ['Infinite log retention','CloudWatch Log Groups with no retention policy, grows forever'],
      ['Lambda memory','Configured memory &gt;2× p99 actual usage, direct billing waste'],
    ], 200) +
    `<h3 class="sub-h" id="audit-usage" data-toc>Usage in Claude / Cursor</h3>` +
    code('claude','<span class="t-com"># Full audit across all regions</span>\n<span class="t-cmd">audit_aws_waste</span>()\n\n<span class="t-com"># Target specific checks and region</span>\n<span class="t-cmd">audit_aws_waste</span>(regions=[<span class="t-str">"us-east-1"</span>], checks=[<span class="t-str">"ebs"</span>,<span class="t-str">"nat"</span>,<span class="t-str">"rds_backups"</span>])\n\n<span class="t-com"># Deep CloudWatch analysis for a specific instance</span>\n<span class="t-cmd">get_instance_deep_analysis</span>(instance_id=<span class="t-str">"i-0abc123"</span>, lookback_days=<span class="t-num">14</span>)\n\n<span class="t-com"># Below-the-billing-API check: NLB cross-zone surcharge</span>\n<span class="t-cmd">audit_nlb_cross_zone_costs</span>()') +
    calloutHTML('note','Results are sorted by estimated monthly savings. Each finding includes resource ID, region, waste type, severity, and an explanation. The reconstructed costs (NLB cross-zone, EFS cross-AZ, snapshot replicas) are conservative upper bounds: AWS does not expose the exact per-AZ split, so each finding states its assumption.') });

/* ── GCP deep audit ── */
INTEL.push({ id:'gcp-audit', name:'GCP deep audit', crumb:'Intelligence',
  blurb:'The same idea as the AWS audit, for Google Cloud. The billing export tells you what you spent. It never tells you a persistent disk is unattached or a VM has been idle for two weeks. <code class="ic">audit_gcp_waste</code> enumerates Compute Engine across every zone and joins Cloud Monitoring to find it.',
  body: patternTable('Pattern','What it catches', [
      ['Unattached disks','Persistent disks with no instance attached, billed per provisioned GB'],
      ['Idle static IPs','Reserved external IPs not in use. GCP bills these at about $7.30/mo each'],
      ['Old snapshots','Snapshots past an age threshold, storage you keep paying for'],
      ['Idle VMs','Running instances under about 5% CPU over 14 days, joined from Cloud Monitoring'],
    ], 200) +
    `<h3 class="sub-h" id="gcp-audit-usage" data-toc>Usage in Claude / Cursor</h3>` +
    code('claude','<span class="t-com"># Full GCP waste audit across all projects</span>\n<span class="t-cmd">audit_gcp_waste</span>()\n\n<span class="t-com"># Target specific projects and checks</span>\n<span class="t-cmd">audit_gcp_waste</span>(projects=[<span class="t-str">"my-prod"</span>], checks=[<span class="t-str">"disks"</span>,<span class="t-str">"ips"</span>])') +
    calloutHTML('note','Needs <code>GCP_PROJECT_IDS</code> set (the wizard prompts for it) plus <code>roles/compute.viewer</code> and <code>roles/monitoring.viewer</code>. See <a class="ilink" href="#iam">Least-privilege IAM</a>.') });

/* ── Azure deep dives ── */
INTEL.push({ id:'azure-intel', name:'Azure deep dives', crumb:'Intelligence',
  blurb:'Advisor, VM rightsizing, and reservation utilization, native to Azure. nable reads Cost Management, Azure Monitor, and the Capacity API directly rather than approximating from the export.',
  body: askCard([
      ['Show Azure cost by dimension','resource group, service, tag'],
      ['Get Azure Advisor recommendations','plus VM rightsizing'],
      ['Are our Azure reservations utilized?','reservation utilization detail'],
    ]) + fnCard([
      ['get_azure_cost_by_dimension','Break Azure spend down by service, resource group, location, or meter'],
      ['get_azure_advisor_recommendations','Azure Advisor cost recs with Microsoft-computed annual savings'],
      ['get_azure_vm_rightsizing','Idle and oversized Azure VMs from Azure Monitor CPU, with dollar cost'],
      ['get_azure_reservation_utilization','Azure reservation utilization summaries from the Capacity API'],
      ['get_azure_budgets','Read budgets you set in Azure and report consumption against each'],
      ['get_resource_cost_breakdown_azure','Per-resource Azure cost detail via the Cost Management Query API'],
      ['forecast_azure_costs','Forecast Azure spend using Azure Cost Management’s own model'],
    ], 230) +
    calloutHTML('note','See <a class="ilink" href="#azure">Azure setup</a> for the service-principal and read-only role wiring.') });

/* ── Kubernetes ── */
INTEL.push({ id:'kubernetes', name:'Kubernetes', crumb:'Intelligence',
  blurb:'Cluster and namespace cost, efficiency, and waste, with no agent to install. nable attributes node costs to namespaces, workloads, and labels straight from your kubeconfig, scores efficiency, and finds the requested-vs-used waste gap.',
  body: askCard([
      ['Break down Kubernetes cost by namespace','and by workload'],
      ['How efficient is our cluster?','requested vs used, the waste gap'],
      ['Compare our clusters','cost and efficiency side by side'],
    ]) + fnCard([
      ['get_kubernetes_costs','Node costs attributed to namespaces, workloads, and labels, with waste'],
      ['get_kubernetes_namespace_breakdown','Single-namespace deep dive: workloads, pod count, CPU/memory efficiency'],
      ['get_workload_costs','Workload cost with efficiency grades, filterable by namespace/kind'],
      ['get_label_costs','Aggregate K8s cost by any pod label, for chargeback by team/app/env'],
      ['get_cluster_efficiency','0–100 efficiency score with grade and dollar-ranked recommendations'],
      ['compare_kubernetes_clusters','Compare cost and efficiency across all configured clusters'],
      ['get_helm_release_costs','Cost by Helm release; detects orphaned releases wasting money'],
      ['get_kubernetes_cost_trends','Cluster spend trend over time: growing, shrinking, or stable'],
      ['list_kubernetes_contexts','List kubeconfig contexts and which one is active'],
      ['create_kubernetes_waste_tickets','File tickets for idle nodes, over-provisioned workloads, orphaned releases'],
    ], 240) });

/* ── SaaS & data platforms ── */
INTEL.push({ id:'saas-intel', name:'SaaS & data platforms', crumb:'Intelligence',
  blurb:'Datadog, Snowflake, Databricks, and more, in the same cost view as your cloud. Fold the software-vendor bill into one total, and dig into DBU consumption by job and cluster.',
  body: askCard([
      ['What are our Databricks DBU costs?','by job and cluster, with efficiency'],
      ['Summarize our SaaS spend','every connected SaaS provider'],
    ]) + fnCard([
      ['get_saas_spend_summary','All SaaS spend (Datadog, Snowflake, GitHub, etc.) separate from infra'],
      ['get_databricks_costs','Databricks workspace cost breakdown for a date range'],
      ['get_databricks_dbu_breakdown','DBU consumption by cluster, job, and cluster type'],
      ['get_databricks_job_costs','Cost and DBU breakdown by Databricks job run'],
      ['get_databricks_cluster_efficiency','Audit all Databricks clusters for efficiency issues and waste'],
      ['get_marketplace_costs','Break down AWS Marketplace costs by product and vendor'],
    ], 240) +
    calloutHTML('note','Each provider needs its connector configured. See the <a class="ilink" href="#datadog">SaaS tools</a> setup sections (Datadog, Snowflake, Databricks, and more).') });

/* ── Anomaly detection (internals) ── */
INTEL.push({ id:'anomalies', name:'Anomaly detection', crumb:'Intelligence',
  blurb:'Multi-signal detection using z-score, CUSUM drift, and day-of-week seasonal normalisation. When something spikes, nable drills into Cost Explorer by tag and tells you which team, environment, or service drove it, and by how much.',
  body: fnCard([
      ['get_anomalies','All anomalies across every connected provider, last 7-30 days'],
      ['get_account_anomalies','Per-account breakdown, useful for multi-account AWS orgs'],
      ['acknowledge_anomaly','Mark as known/expected so it stops surfacing in digests'],
      ['create_anomaly_tickets','Auto-file Jira / Linear / GitHub issues for unacknowledged anomalies'],
    ], 180) +
    calloutHTML('note','Slack and Teams alerts fire automatically when a new anomaly is detected, no polling needed. Configure <code>SLACK_WEBHOOK_URL</code> or <code>TEAMS_WEBHOOK_URL</code> to enable.') });

/* ── Rightsizing (internals) ── */
INTEL.push({ id:'rightsizing', name:'Rightsizing', crumb:'Intelligence',
  blurb:'Combines AWS Compute Optimizer recommendations with nable’s own CloudWatch analysis. Surfaces instances where actual CPU / memory / connection utilization is consistently below the provisioned level, with a specific recommended type and estimated savings.',
  body: fnCard([
      ['get_rightsizing_recommendations','EC2, RDS, and Lambda rightsizing in one call'],
      ['create_rightsizing_tickets','Auto-file tickets for the top N recommendations by savings'],
      ['get_instance_deep_analysis','Per-instance CloudWatch p95/p99 utilization deep dive'],
    ], 200) });

/* ── Share & automate ── */
INTEL.push({ id:'share', name:'Share & automate', crumb:'Intelligence',
  blurb:'Get findings out of chat: dashboards, exports, tickets, alerts, and digests. nable can stand up a browser dashboard, file a Jira/Linear/GitHub ticket, publish to Notion, push to n8n, and send scheduled digests, all without Claude in the loop once configured.',
  body: askCard([
      ['Export this to CSV','opens clean in Excel or Sheets'],
      ['Start the team dashboard','browser dashboard, no Claude required'],
      ['File a Jira ticket for this anomaly','or Linear or GitHub'],
      ['Send a weekly digest every Monday','top drivers by email or Slack'],
    ]) + fnCard([
      ['export_cost_report','Export a cost report as HTML (printable to PDF) and/or CSV'],
      ['export_cost_report_csv','Run the full audit and export results to CSV'],
      ['start_dashboard_server','Local web dashboard, localhost by default (expose=true for the network)'],
      ['generate_account_dashboard','Generate a cost dashboard for an account and open it in your browser'],
      ['create_ticket','Create a Jira/Linear/GitHub ticket with a custom title and body'],
      ['create_anomaly_tickets','Auto-file tickets for active high/medium anomalies without one'],
      ['create_rightsizing_tickets','File tickets for rightsizing recs on over-provisioned resources'],
      ['subscribe_to_report','Scheduled report delivered to Slack channels and/or email'],
      ['send_weekly_digest_now','Send the weekly email digest now: spend, anomalies, top recs'],
      ['send_digest_now','Trigger a cost digest to Slack and/or Teams now'],
      ['push_weekly_insight','Push a rich weekly cost-intelligence summary to Slack now'],
      ['publish_cost_report_to_notion','Run the audit and publish results to your team’s Notion page'],
      ['push_to_n8n','Run the audit and push results to your n8n workflow via webhook'],
      ['what_can_nable_do','Show everything nable can do, tailored to what you’ve connected'],
    ], 230) +
    calloutHTML('note','Scheduled digests, report subscriptions, and ticket auto-creation are <span class="ic">Team</span> features. Manual exports, dashboards, and single tickets are free.') });

/* ─────────────────────────────────────────────────────────────
   render
   ───────────────────────────────────────────────────────────── */
function renderSteps(steps){
  return `<div class="steps">` + steps.map((s,i) => {
    const text = s[0], extra = s[1] || '';
    return `<div class="step"><div class="step-num">${i+1}</div><div class="step-body"><p class="body" style="margin:${extra?'0 0 10px':'0'}">${text}</p>${extra}</div></div>`;
  }).join('') + `</div>`;
}
function calloutHTML(kind, html){
  return `<div class="callout ${kind}">${ICONS[kind]||ICONS.note}<div>${html}</div></div>`;
}
function badgePill(label){
  return `<span class="pbadge-team">${label}</span>`;
}

function renderConnector(c){
  const mark = c.logo ? plogo(c.logo) : (c.badge?`<span class="pbadge">${c.badge}</span>`:'');
  let html = `<section class="doc-section" id="${c.id}" data-title="${c.name}" data-crumb="${c.crumb}">`;
  html += `<h2 class="sec-h">${mark}${c.name}${c.pbadge?badgePill(c.pbadge):''}</h2>`;
  html += `<p class="body">${c.blurb}</p>`;
  if(c.steps){ html += `<h3 class="sub-h" id="${c.id}-setup" data-toc>Setup</h3>` + renderSteps(c.steps); }
  if(c.env){ html += `<h3 class="sub-h" id="${c.id}-env" data-toc>Manual env vars</h3>` + envBlock(c.env); }
  if(c.callout){ html += calloutHTML(c.callout[0], c.callout[1]); }
  html += `</section>`;
  return html;
}
function renderIntel(c){
  let html = `<section class="doc-section" id="${c.id}" data-title="${c.name}" data-crumb="${c.crumb}">`;
  html += `<h2 class="sec-h">${c.name}${c.pbadge?badgePill(c.pbadge):''}</h2><p class="body">${c.blurb}</p>`;
  if(c.body) html += c.body;
  html += `</section>`;
  return html;
}
function sidebarLink(id, name){
  return `<a href="#${id}" class="nav-link" data-spy="${id}">${name}</a>`;
}

/* inject SaaS */
(function(){
  const sec = document.getElementById('saas-sections');
  const sb  = document.getElementById('sb-saas');
  sec.innerHTML = SAAS.map((c,i) => renderConnector(c) + (i<SAAS.length-1?'<hr class="sec-divider"/>':'')).join('');
  sb.innerHTML = SAAS.map(c => sidebarLink(c.id, c.name)).join('');
})();
/* inject Alerts */
(function(){
  const sec = document.getElementById('alerts-sections');
  const sb  = document.getElementById('sb-alerts');
  sec.innerHTML = ALERTS.map((c,i) => renderConnector(c) + (i<ALERTS.length-1?'<hr class="sec-divider"/>':'')).join('');
  sb.innerHTML = ALERTS.map(c => sidebarLink(c.id, c.name)).join('');
})();
/* inject Intelligence */
(function(){
  const sec = document.getElementById('intel-sections');
  const sb  = document.getElementById('sb-intel');
  sec.innerHTML = INTEL.map((c,i) => renderIntel(c) + (i<INTEL.length-1?'<hr class="sec-divider"/>':'')).join('');
  sb.innerHTML = INTEL.map(c => sidebarLink(c.id, c.name)).join('');
})();

/* ── copy buttons ── */
document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-copy]');
  if(!btn) return;
  const block = btn.closest('[data-code]');
  const text = block.querySelector('pre code').innerText;
  navigator.clipboard?.writeText(text).then(() => {
    btn.textContent = 'Copied';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 1500);
  });
});

/* ── scrollspy: left nav + breadcrumb ── */
const sections = Array.from(document.querySelectorAll('.doc-section'));
const spyLinks = new Map();
document.querySelectorAll('.nav-link[data-spy]').forEach(a => spyLinks.set(a.dataset.spy, a));
const crumbCur = document.getElementById('crumbCur');
const crumbGroup = document.querySelector('.breadcrumb').children[2];
let activeId = null;

function setActive(id){
  if(id === activeId) return;
  activeId = id;
  spyLinks.forEach(a => a.classList.remove('active'));
  const link = spyLinks.get(id);
  if(link){
    link.classList.add('active');
    const sb = document.getElementById('sidebar');
    const lr = link.getBoundingClientRect(), sr = sb.getBoundingClientRect();
    if(lr.top < sr.top + 60 || lr.bottom > sr.bottom - 20){
      sb.scrollTop += (lr.top - sr.top) - sr.height/2 + 40;
    }
  }
  const sec = document.getElementById(id);
  if(sec){
    crumbCur.textContent = sec.dataset.title || '';
    crumbGroup.textContent = sec.dataset.crumb || 'Getting started';
  }
  buildTOC(id);
}

/* ── right TOC built from active section's [data-toc] headings ── */
const tocList = document.getElementById('tocList');
let tocLinks = [];
function buildTOC(id){
  const sec = document.getElementById(id);
  if(!sec){ tocList.innerHTML = '<li class="toc-empty">—</li>'; tocLinks = []; return; }
  const items = Array.from(sec.querySelectorAll('[data-toc]'));
  if(!items.length){ tocList.innerHTML = '<li class="toc-empty">—</li>'; tocLinks = []; return; }
  tocList.innerHTML = items.map(el => {
    const tid = el.id;
    const label = el.textContent.trim();
    return `<li><a href="#${tid}" data-toclink="${tid}">${label}</a></li>`;
  }).join('');
  tocLinks = Array.from(tocList.querySelectorAll('a'));
}

/* IntersectionObserver for top-level sections */
const secObserver = new IntersectionObserver((entries) => {
  const visible = entries.filter(en => en.isIntersecting)
    .sort((a,b) => a.boundingClientRect.top - b.boundingClientRect.top);
  if(visible.length){ setActive(visible[0].target.id); }
}, { rootMargin: `-${64+20}px 0px -65% 0px`, threshold: 0 });
sections.forEach(s => secObserver.observe(s));

/* TOC active sub-heading on scroll */
function updateTOCActive(){
  if(!tocLinks.length) return;
  const top = 64 + 30;
  let current = tocLinks[0];
  for(const a of tocLinks){
    const el = document.getElementById(a.dataset.toclink);
    if(el && el.getBoundingClientRect().top - top <= 0) current = a;
  }
  tocLinks.forEach(a => a.classList.toggle('on', a === current));
}
let ticking = false;
window.addEventListener('scroll', () => {
  if(!ticking){ requestAnimationFrame(() => { updateTOCActive(); ticking = false; }); ticking = true; }
}, { passive:true });

/* init */
setActive('quickstart');
updateTOCActive();

/* ── mobile sidebar ── */
const sidebar = document.getElementById('sidebar');
const scrim = document.getElementById('sbScrim');
const menuBtn = document.getElementById('menuBtn');
function closeSidebar(){ sidebar.classList.remove('open'); scrim.classList.remove('open'); }
menuBtn?.addEventListener('click', () => { sidebar.classList.toggle('open'); scrim.classList.toggle('open'); });
scrim?.addEventListener('click', closeSidebar);
sidebar.addEventListener('click', (e) => { if(e.target.closest('.nav-link')) closeSidebar(); });

/* ── search box (placeholder: focus jump list) ── */
document.getElementById('searchBox')?.addEventListener('click', () => {
  const q = prompt('Jump to section:');
  if(!q) return;
  const needle = q.toLowerCase();
  const match = sections.find(s => (s.dataset.title||'').toLowerCase().includes(needle));
  if(match){ location.hash = '#' + match.id; }
});
document.addEventListener('keydown', (e) => {
  if((e.metaKey||e.ctrlKey) && e.key.toLowerCase() === 'k'){ e.preventDefault(); document.getElementById('searchBox').click(); }
});
