const { useState, useEffect, useRef } = React;

/* tweak defaults */
const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "palette": "graphite",
  "layout": "editorial",
  "interaction": "cycling"
}/*EDITMODE-END*/;

const PALETTES = {
  onyx: {
    "--bg":"#0a0a0c","--bg-1":"#0f0f12","--bg-2":"#15151a","--bg-3":"#1d1d24",
    "--line":"#22222b","--line-2":"#2e2e38",
    "--fg":"#f4f4f0","--fg-2":"#a8a8a2","--fg-3":"#6e6e68","--fg-4":"#46463f",
    "--accent":"#5fe8a0","--accent-dim":"#3aa676",
    "--warn":"#ffb46b","--alert":"#ff7a6b",
    "--grid":"rgba(255,255,255,.03)"
  },
  graphite: {
    "--bg":"#15140f","--bg-1":"#1a1914","--bg-2":"#221f17","--bg-3":"#2a261c",
    "--line":"#2e2920","--line-2":"#3d3729",
    "--fg":"#f3efe6","--fg-2":"#bbb6a4","--fg-3":"#7e7a6c","--fg-4":"#4a4639",
    "--accent":"#e4a76b","--accent-dim":"#a07242",
    "--warn":"#ffb46b","--alert":"#d97757",
    "--grid":"rgba(255,255,255,.025)"
  },
  paper: {
    "--bg":"#fbfaf7","--bg-1":"#f6f4ee","--bg-2":"#eeebe2","--bg-3":"#e5e1d3",
    "--line":"#e3dfcf","--line-2":"#d2cdb9",
    "--fg":"#1a1915","--fg-2":"#4d4b42","--fg-3":"#85806f","--fg-4":"#b4ae9b",
    "--accent":"#1f8a5b","--accent-dim":"#3b6e3a",
    "--warn":"#b8533a","--alert":"#b8533a",
    "--grid":"rgba(0,0,0,.04)"
  },
  mono: {
    "--bg":"#ffffff","--bg-1":"#fafafa","--bg-2":"#f2f2f0","--bg-3":"#e8e8e5",
    "--line":"#e6e6e3","--line-2":"#d0d0cc",
    "--fg":"#0a0a0a","--fg-2":"#525252","--fg-3":"#8a8a85","--fg-4":"#b8b8b3",
    "--accent":"#0a0a0a","--accent-dim":"#3a3a3a",
    "--warn":"#666","--alert":"#0a0a0a",
    "--grid":"rgba(0,0,0,.035)"
  },
};

function applyPalette(name){
  const p = PALETTES[name] || PALETTES.graphite;
  const root = document.documentElement;
  Object.entries(p).forEach(([k,v]) => root.style.setProperty(k,v));
}

/* Email capture — posts to /api/subscribe */
function EmailCapture({ source = "hero", placeholder = "email", btnLabel = "Get started", center = false }){
  const [email, setEmail] = useState("");
  const [state, setState] = useState("idle"); // idle | loading | done | error

  async function submit(e){
    e.preventDefault();
    if(!email || state === "loading" || state === "done") return;
    setState("loading");
    try {
      const res = await fetch("/api/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, source }),
      });
      if(!res.ok) throw new Error("subscribe failed");
      if(window.posthog) posthog.capture("email_subscribed", { source });
      setState("done");
    } catch {
      setState("error");
      setTimeout(() => setState("idle"), 3000);
    }
  }

  if(state === "done"){
    return (
      <p className="mono" style={{fontSize:12,color:"var(--accent)",letterSpacing:".06em",
        textAlign: center ? "center" : "left", marginTop: 8}}>
        Check your inbox. Setup guide on its way.
      </p>
    );
  }

  return (
    <form className={"email-capture" + (center ? " center" : "")} onSubmit={submit}
          style={{margin: center ? "0 auto" : "0"}}>
      <input
        type="email"
        value={email}
        onChange={e => setEmail(e.target.value)}
        placeholder={placeholder}
        required
        autoComplete="email"
        aria-label="Email"
      />
      <button type="submit" disabled={state === "loading"}>
        {state === "loading" ? "..." : btnLabel} <span className="arr">→</span>
      </button>
      {state === "error" && (
        <span style={{position:"absolute",bottom:-20,left:0,fontSize:11,
          color:"var(--alert)",fontFamily:"'JetBrains Mono',monospace"}}>
          Something went wrong. Try again.
        </span>
      )}
    </form>
  );
}

function LogoMark(){
  return (
    <svg width="26" height="26" viewBox="0 0 32 32" className="mark-img" aria-hidden="true">
      <rect width="32" height="32" rx="7" fill="var(--accent)" />
      <path d="M9.5 23V11.5h2.6v1.5c.7-1.1 1.9-1.7 3.4-1.7 2.6 0 4.2 1.7 4.2 4.5V23h-2.7v-6.6c0-1.7-.9-2.6-2.4-2.6s-2.5 1-2.5 2.7V23H9.5Z" fill="var(--bg)"/>
    </svg>
  );
}

/* Nav */
function Nav(){
  return (
    <nav className="nav">
      <div className="nav-inner">
        <a href="#top" className="logo">
          <LogoMark />
          <span>nable</span>
        </a>
        <ul>
          <li><a href="#thesis">Thesis</a></li>
          <li><a href="#runtime">Runtime</a></li>
          <li><a href="#connectors">Connectors</a></li>
          <li><a href="#pricing">Pricing</a></li>
          <li><a href="/docs.html" onClick={()=>{ if(window.posthog) posthog.capture('docs_clicked',{location:'nav'}); }}>Docs</a></li>
        </ul>
        <div className="right">
          <a href="/account.html" className="btn btn-ghost">Sign in</a>
          <a href="https://buy.stripe.com/eVq14mbe9ffE3le3wC2Nq02"
             className="btn btn-primary"
             onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'nav',cta:'start_free'}); }}>
            Get started free <span className="arr">→</span>
          </a>
        </div>
      </div>
    </nav>
  );
}

/* Hero */
function Hero({ layout, interaction }){
  return (
    <header className={"hero " + (layout === "editorial" ? "editorial" : "")} id="top">
      <div className="hero-grid-bg"></div>
      <div className="wrap">
        <div className="hero-inner">
          <div className="hero-left">
            <div className="eyebrow"><span className="d"></span> FinOps · works in Claude, Cursor, Windsurf · v0.8.36</div>
            <h1 className="display">
              The cloud bill,<br/>
              <span className="strike">in a dashboard.</span><br/>
              <span className="accent">In your editor.</span>
            </h1>
            <p className="lede">
              Real billing data from AWS, Azure, GCP, and 14 SaaS tools, live in Claude or Cursor. Ask anything in plain English. Nothing leaves your machine.
            </p>
            <div className="hero-cta-row">
              <a href="https://buy.stripe.com/eVq14mbe9ffE3le3wC2Nq02"
                 className="btn btn-primary"
                 onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'hero',cta:'get_started_free'}); }}>
                Get started free <span className="arr">→</span>
              </a>
              <CopyInstall />
            </div>
            <TrustStrip />
          </div>
          <div className="hero-right">
            <Console interaction={interaction} />
          </div>
        </div>
      </div>
    </header>
  );
}

function CopyInstall(){
  const [copied, setCopied] = useState(false);
  return (
    <div className="install" role="group" aria-label="Install command">
      <span className="prompt">$</span>
      <span className="cmd">pip install finops-mcp</span>
      <button onClick={() => {
        navigator.clipboard?.writeText("pip install finops-mcp && finops setup");
        setCopied(true);
        setTimeout(()=>setCopied(false),1600);
        if(window.posthog) posthog.capture('install_copied');
      }}>
        {copied ? "copied" : "copy"}
      </button>
    </div>
  );
}

function TrustStrip(){
  const items = [
    {lab:"installs / mo", val:"4,127", sub:"+38% week over week"},
    {lab:"providers", val:"17", sub:"AWS · Azure · GCP +"},
    {lab:"local only", val:"0 bytes", sub:"sent to our servers"},
  ];
  return (
    <div className="trust" style={{marginTop:56,gridTemplateColumns:"repeat(3,1fr)"}}>
      {items.map((t,i) => (
        <div className="ti" key={i}>
          <span className="val mono">{t.val}<span className="sub">{t.sub}</span></span>
          <span className="lab">{t.lab}</span>
        </div>
      ))}
    </div>
  );
}

/* Console (interactive demo terminal) */
const QUERIES = [
  {
    q: "Compute spend across all providers, April vs March.",
    response: (
      <>
        <p>Normalized to USD across the three clouds. Pulled from each provider's billing API just now.</p>
        <div className="ttable">
          <div className="r hd"><span>Provider · service</span><span>April</span><span>delta MoM</span></div>
          <div className="r"><span>AWS · EC2 + Fargate</span><span className="v num">$18,420</span><span className="d up num">+18.6%</span></div>
          <div className="r"><span>Azure · Virtual Machines</span><span className="v num">$6,310</span><span className="d up num">+4.2%</span></div>
          <div className="r"><span>GCP · Compute Engine</span><span className="v num">$4,870</span><span className="d down num">-3.4%</span></div>
          <div className="r total"><span>Total compute</span><span className="v num">$29,600</span><span className="d up num">+12.0%</span></div>
        </div>
        <p style={{marginTop:14}}>Three new <span className="mono" style={{color:"var(--fg)"}}>c6i.4xlarge</span> in <span className="mono" style={{color:"var(--fg)"}}>us-east-1</span> account for $1,890 of the AWS delta. Want me to tag them and open an audit ticket?</p>
      </>
    )
  },
  {
    q: "Any anomalies this week?",
    response: (
      <>
        <p><span className="anomaly">Datadog spike detected.</span> Usage is up <span style={{color:"var(--alert)"}}>+127%</span> vs your same-weekday baseline. Z-score 4.8 against the 28-day window.</p>
        <div className="ttable">
          <div className="r hd"><span>Tag driver</span><span>Delta</span><span>% of spike</span></div>
          <div className="r"><span>team=platform</span><span className="d up num">+$2,290</span><span className="d up num">78%</span></div>
          <div className="r"><span>team=infra</span><span className="d up num">+$480</span><span className="d up num">16%</span></div>
          <div className="r"><span>(untagged)</span><span className="d up num">+$180</span><span className="d up num">6%</span></div>
        </div>
        <p style={{marginTop:12}}>Opened <span className="mono" style={{color:"var(--fg)"}}>JIRA-2841</span>, paged @sre, posted to <span className="mono" style={{color:"var(--fg)"}}>#cost-alerts</span>. <span style={{color:"var(--accent)"}}>Drift contained.</span></p>
      </>
    )
  },
  {
    q: "Which EC2 instances should we downsize?",
    response: (
      <>
        <p>Cross-referenced CloudWatch metrics with Compute Optimizer. 11 instances are sustained below 15% CPU over 14 days. Top six by savings:</p>
        <div className="ttable">
          <div className="r hd"><span>Instance / current</span><span>Recommended</span><span>Save / mo</span></div>
          <div className="r"><span>i-0a3f · m5.4xlarge</span><span className="v num">m6i.xlarge</span><span className="d down num">$412</span></div>
          <div className="r"><span>i-0c91 · r5.2xlarge</span><span className="v num">r6i.large</span><span className="d down num">$298</span></div>
          <div className="r"><span>i-0e7d · m5.2xlarge</span><span className="v num">m6i.large</span><span className="d down num">$201</span></div>
          <div className="r"><span>i-0b44 · c5.4xlarge</span><span className="v num">c6i.xlarge</span><span className="d down num">$184</span></div>
          <div className="r total"><span>11 instances</span><span className="v num">-</span><span className="d down num">$1,840 / mo</span></div>
        </div>
        <p style={{marginTop:12}}>Net annualized savings: <span style={{color:"var(--accent)"}}>$22,080</span>. Generate PRs against your IaC repo?</p>
      </>
    )
  },
  {
    q: "What's our effective discount rate this quarter?",
    response: (
      <>
        <p>Blended across Savings Plans, RIs, and committed-use discounts on GCP. Coverage measured against on-demand list:</p>
        <div className="ttable">
          <div className="r hd"><span>Commitment</span><span>Coverage</span><span>Effective rate</span></div>
          <div className="r"><span>AWS · Savings Plans (1y)</span><span className="v num">82%</span><span className="d down num">-24.1%</span></div>
          <div className="r"><span>AWS · RIs (RDS, ElastiCache)</span><span className="v num">71%</span><span className="d down num">-31.8%</span></div>
          <div className="r"><span>GCP · CUDs (compute)</span><span className="v num">64%</span><span className="d down num">-20.4%</span></div>
          <div className="r total"><span>Blended effective discount</span><span className="v num">-</span><span className="d down num">-26.7%</span></div>
        </div>
        <p style={{marginTop:12}}>You'd unlock another <span style={{color:"var(--accent)"}}>$8,200 / mo</span> by raising Compute SP coverage to 92%. Model it?</p>
      </>
    )
  },
];

function Console({ interaction }){
  const [idx, setIdx] = useState(0);
  const [phase, setPhase] = useState("typing");
  const [typed, setTyped] = useState("");
  const timers = useRef([]);

  useEffect(() => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
    setTyped(""); setPhase("typing");
    const q = QUERIES[idx].q;
    let i = 0;
    function step(){
      if(i <= q.length){
        setTyped(q.slice(0,i));
        i++;
        timers.current.push(setTimeout(step, 18 + Math.random()*22));
      } else {
        timers.current.push(setTimeout(() => setPhase("thinking"), 350));
        timers.current.push(setTimeout(() => setPhase("answered"), 1500));
      }
    }
    step();
    return () => timers.current.forEach(clearTimeout);
  }, [idx]);

  useEffect(() => {
    if(interaction !== "cycling") return;
    if(phase !== "answered") return;
    const t = setTimeout(() => setIdx(i => (i+1) % QUERIES.length), 6500);
    return () => clearTimeout(t);
  }, [phase, interaction, idx]);

  return (
    <div className="console" id="runtime">
      <div className="console-bar">
        <div className="dots"><i></i><i></i><i></i></div>
        <span className="title">claude · mcp[nable] · ~/projects/platform-infra</span>
        <span className="status">runtime active</span>
      </div>
      <div className="console-body">
        <div className="msg">
          <div className="av you">you</div>
          <div className="bubble user">
            <p>{typed}<span className="cursor"></span></p>
          </div>
        </div>
        {phase === "thinking" && (
          <div className="msg">
            <div className="av ai">nable</div>
            <div className="bubble"><div className="thinking"><i></i><i></i><i></i></div></div>
          </div>
        )}
        {phase === "answered" && (
          <div className="msg">
            <div className="av ai">nable</div>
            <div className="bubble">{QUERIES[idx].response}</div>
          </div>
        )}
      </div>
      <div className="q-pager">
        <span>query {String(idx+1).padStart(2,"0")} / {String(QUERIES.length).padStart(2,"0")}</span>
        <span style={{marginLeft:14,color:"var(--fg-4)"}}>·</span>
        <span style={{marginLeft:14}}>{interaction === "cycling" ? "auto-advancing" : "manual"}</span>
        <div className="dots" role="tablist">
          {QUERIES.map((_,i) => (
            <i key={i} className={i===idx?"on":""} onClick={()=>setIdx(i)} role="tab" aria-selected={i===idx} tabIndex={0}></i>
          ))}
        </div>
      </div>
    </div>
  );
}

/* Thesis */
function Thesis(){
  const cards = [
    {n:"01 · TAM", h:"Cloud spend is the #2 line item in modern software.", p:"$700B+ annual cloud + SaaS spend, growing 18% YoY. Every dollar is unaccountable until someone reconciles 8 dashboards and a CSV. That reconciliation work is the wedge."},
    {n:"02 · Shift", h:"FinOps moved from a quarterly review to a real-time question.", p:"AI editors made plain-English access to live data the default interface. Asking \"what spiked\" is now cheaper than building a dashboard. The dashboard era is the legacy era."},
    {n:"03 · Moat", h:"Local-first compounds with every connector.", p:"Credentials in the OS keyring. No data lake. No SOC-2 surface area. The next 17 connectors are a feature shipment, not a security review. Enterprise sells itself."},
  ];
  return (
    <section id="thesis">
      <div className="wrap">
        <div className="section-head">
          <div className="label">Thesis</div>
          <h2>The dashboard <em>was</em> the product.<br/>The interface is the product now.</h2>
          <p>Three forces converge in 2026. nable is the runtime where they meet.</p>
        </div>
        <div className="thesis">
          {cards.map((c,i) => (
            <div className="thesis-card" key={i}>
              <span className="n">{c.n}</span>
              <h3>{c.h}</h3>
              <p>{c.p}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

/* Architecture */
function Architecture(){
  return (
    <section id="arch">
      <div className="wrap">
        <div className="section-head">
          <div className="label">Architecture</div>
          <h2>Headless by design.<br/><em>Your data never moves.</em></h2>
          <p>nable is not SaaS. It runs on the engineer's machine, holds credentials in the OS keyring, queries provider APIs directly, and surfaces tools to whichever AI editor is open. We never see your bill.</p>
        </div>
        <div className="arch">
          <div className="arch-grid"></div>
          <div className="arch-row">
            <div className="arch-col">
              <span className="lab">your editor</span>
              <div className="arch-node">
                <h4>Claude · Cursor · Zed</h4>
                <span className="sub">MCP client</span>
                <div className="chips"><span>tools/list</span><span>tools/call</span></div>
              </div>
            </div>
            <div className="arch-arrow"><span>stdio</span><span className="line"></span><span>jsonrpc</span></div>
            <div className="arch-col">
              <span className="lab">runtime · local</span>
              <div className="arch-node center">
                <h4>nable runtime</h4>
                <span className="sub">finops-mcp / 0.8.36</span>
                <div className="chips"><span>keyring</span><span>fernet</span><span>read-only</span><span>audit-log</span></div>
              </div>
            </div>
            <div className="arch-arrow"><span>https</span><span className="line"></span><span>signed</span></div>
            <div className="arch-col">
              <span className="lab">provider apis</span>
              <div className="arch-node">
                <h4>17 connectors</h4>
                <span className="sub">cost · usage · billing</span>
                <div className="chips"><span>AWS CE/CUR</span><span>Azure CM</span><span>GCP BQ</span><span>+14</span></div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

/* Question marquee */
const QUESTIONS = [
  "What drove our AWS bill up 40% last month?",
  "Which Kubernetes namespace is over-provisioned?",
  "Which EC2 instances should we downsize?",
  "Compare our cloud spend vs SaaS spend.",
  "Create a Jira ticket for any EC2 waste over $200/mo.",
  "Which team is spending the most on Datadog?",
  "What will our AWS bill look like next month?",
  "Show me RDS instances with low CPU.",
  "What's our effective discount rate from Savings Plans?",
  "Find idle NAT Gateways and tag the owners.",
];

function QMarquee(){
  return (
    <section className="tight" style={{padding:"0",borderTop:"none"}}>
      <div className="qmarq">
        <div className="track">
          {[...QUESTIONS, ...QUESTIONS].map((q,i) => (
            <span className="q" key={i}>{q}</span>
          ))}
        </div>
      </div>
    </section>
  );
}

/* Connectors */
const CONNECTORS = [
  {nm:"AWS",        px:"Cost Explorer · CUR via S3",     tag:"live"},
  {nm:"Azure",      px:"Cost Management API",            tag:"live"},
  {nm:"GCP",        px:"Cloud Billing · BigQuery",       tag:"live"},
  {nm:"Datadog",    px:"Usage Metering v2",              tag:"live"},
  {nm:"Snowflake",  px:"ACCOUNT_USAGE.METERING",         tag:"live"},
  {nm:"Langfuse",   px:"Daily metrics · cost / token",   tag:"live"},
  {nm:"MongoDB",    px:"Atlas Invoice API",              tag:"live"},
  {nm:"Twilio",     px:"Usage Records API",              tag:"live"},
  {nm:"Cloudflare", px:"Billing API",                    tag:"live"},
  {nm:"GitHub",     px:"Actions mins · Copilot seats",   tag:"live"},
  {nm:"Vercel",     px:"Invoice API · enterprise",       tag:"live"},
  {nm:"PagerDuty",  px:"Seat count",                     tag:"live"},
  {nm:"New Relic",  px:"Data ingest · user counts",      tag:"live"},
  {nm:"Linear",     px:"Seat plan · usage rollup",       tag:"live"},
  {nm:"OpenAI",     px:"Usage API · per-model spend",    tag:"live"},
  {nm:"Anthropic",  px:"Org usage · per-model spend",    tag:"live"},
  {nm:"Stripe",     px:"Billing meter · platform fees",  tag:"beta"},
];

function Connectors(){
  return (
    <section id="connectors">
      <div className="wrap">
        <div className="section-head">
          <div className="label">Connectors</div>
          <h2>Seventeen sources.<br/><em>One conversation.</em></h2>
          <p>Every connector is a real API integration, not a CSV export. New providers ship monthly. Suggest one and we'll quote a date.</p>
        </div>
        <div className="conn-wrap">
          <div>
            <div className="mono" style={{fontSize:11,color:"var(--fg-3)",letterSpacing:".08em",textTransform:"uppercase",marginBottom:16}}>Roadmap</div>
            <ul style={{listStyle:"none",display:"flex",flexDirection:"column",gap:12,fontSize:14,color:"var(--fg-2)",lineHeight:1.5}}>
              <li style={{display:"flex",justifyContent:"space-between",borderBottom:"1px solid var(--line)",paddingBottom:10}}><span>Render · Fly · Railway</span><span className="mono" style={{fontSize:11,color:"var(--accent-dim)"}}>Q2 '26</span></li>
              <li style={{display:"flex",justifyContent:"space-between",borderBottom:"1px solid var(--line)",paddingBottom:10}}><span>Supabase · Neon · PlanetScale</span><span className="mono" style={{fontSize:11,color:"var(--accent-dim)"}}>Q2 '26</span></li>
              <li style={{display:"flex",justifyContent:"space-between",borderBottom:"1px solid var(--line)",paddingBottom:10}}><span>OCI · IBM Cloud</span><span className="mono" style={{fontSize:11,color:"var(--accent-dim)"}}>Q3 '26</span></li>
              <li style={{display:"flex",justifyContent:"space-between"}}><span>SAP Concur · NetSuite</span><span className="mono" style={{fontSize:11,color:"var(--fg-3)"}}>requested</span></li>
            </ul>
          </div>
          <div className="conn-grid">
            {CONNECTORS.map((c,i) => (
              <div className="conn" key={i}>
                <span className="nm">{c.nm}</span>
                <span className="px">{c.px}</span>
                <span className={"tag " + (c.tag === "beta" ? "beta" : "")}>{c.tag}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

/* Telemetry bento */
function Telemetry(){
  const pts = [12,18,14,22,28,24,32,30,38,42,36,48,52,58];
  const path = pts.map((p,i) => `${i===0?"M":"L"} ${i*(100/(pts.length-1))} ${60-p}`).join(" ");
  return (
    <section id="telemetry" className="tight">
      <div className="wrap">
        <div className="section-head">
          <div className="label">By the numbers</div>
          <h2>Adoption signal.<br/><em>Live.</em></h2>
          <p>Pulled from PyPI, Stripe, and our telemetry endpoint, refreshed nightly. No marketing math.</p>
        </div>
        <div className="bento">
          <div className="bento-cell tall">
            <span className="lab">monthly installs · pypi</span>
            <span className="big mono">4,127<span className="delta">+38% WoW</span></span>
            <p>Trajectory consistent with bottom-up dev-tool growth. 67% of paid trials originate from a prior unpaid install.</p>
            <div className="sparkline">
              <svg viewBox="0 0 100 60" preserveAspectRatio="none">
                <defs>
                  <linearGradient id="g1" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--accent)" stopOpacity=".25"/>
                    <stop offset="100%" stopColor="var(--accent)" stopOpacity="0"/>
                  </linearGradient>
                </defs>
                <path d={path + ` L 100 60 L 0 60 Z`} fill="url(#g1)"/>
                <path d={path} fill="none" stroke="var(--accent)" strokeWidth="1.5" vectorEffect="non-scaling-stroke"/>
                {pts.map((p,i) => <circle key={i} cx={i*(100/(pts.length-1))} cy={60-p} r="1.2" fill="var(--accent)"/>)}
              </svg>
            </div>
          </div>
          <div className="bento-cell">
            <span className="lab">paid conversion</span>
            <span className="big mono">14.2%</span>
            <p>Installs to Team plan within 30 days.</p>
          </div>
          <div className="bento-cell span-end">
            <span className="lab">net retention</span>
            <span className="big mono">132%<span className="delta">trailing 6mo</span></span>
            <p>Driven by multi-account / multi-cloud expansion.</p>
          </div>
          <div className="bento-cell row-end">
            <span className="lab">median savings · first 60 days</span>
            <span className="big mono">$3,840<span className="delta">/ mo</span></span>
            <p>Across teams who shipped at least one rightsizing recommendation.</p>
          </div>
          <div className="bento-cell row-end span-end">
            <span className="lab">ttfa · time to first answer</span>
            <span className="big mono">4 min<span className="delta">install to insight</span></span>
            <p>Wizard auto-configures the MCP client. Median end-to-end.</p>
          </div>
        </div>
      </div>
    </section>
  );
}

/* Pricing */
function Pricing(){
  return (
    <section id="pricing">
      <div className="wrap">
        <div className="section-head">
          <div className="label">Pricing</div>
          <h2>Free to ask.<br/><em>Pay to ship.</em></h2>
          <p>Solo is free forever. Team adds the automation layer: the things you'd otherwise hire a contractor to build and maintain.</p>
        </div>
        <div className="pricing">
          <div className="tier">
            <span className="name">Solo</span>
            <span className="amt mono">$0<span className="sm">/ forever</span></span>
            <p className="desc">Ask questions of your own clouds. Read-only. Unlimited queries.</p>
            <ul>
              <li>All 17 connectors</li>
              <li>AWS deep audit (gp2, NAT, log retention...)</li>
              <li>Rightsizing recommendations in Claude</li>
              <li>z-score anomaly findings</li>
              <li className="dim">Slack / Teams alerts</li>
              <li className="dim">Ticket auto-creation</li>
            </ul>
            <div className="cta">
              <a href="https://buy.stripe.com/eVq14mbe9ffE3le3wC2Nq02"
                 className="btn btn-ghost" style={{width:"100%",justifyContent:"center"}}
                 onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'pricing',tier:'solo'}); }}>
                Install
              </a>
            </div>
          </div>
          <div className="tier feat">
            <span className="name">Team</span>
            <span className="amt mono">$19.99<span className="sm">/ user / mo</span></span>
            <p className="desc">For finance + platform teams who need the spend to actually go down, not just be visible.</p>
            <ul>
              <li>Everything in Solo</li>
              <li>Anomaly alerts to Slack or Teams the moment spend spikes</li>
              <li>PR cost comments: dollar impact of Terraform changes before merge</li>
              <li>Auto-create Jira, Linear, or GitHub tickets from anomalies and rightsizing findings</li>
              <li>Budget enforcement: warn at 80%, block queries at 100%</li>
              <li>Kubernetes cost by namespace, workload, and Helm release</li>
              <li>RI / SP / CUD break-even modeling: buy or wait</li>
              <li>Cost attribution by team, service, and tag across all providers</li>
              <li>Multi-account org rollup with per-account drill-down</li>
              <li>Weekly email digest, no AI session required</li>
            </ul>
            <div className="cta">
              <a href="https://buy.stripe.com/eVq14mbe9ffE3le3wC2Nq02"
                 className="btn btn-primary" style={{width:"100%",justifyContent:"center"}}
                 onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'pricing',tier:'team'}); }}>
                Start free <span className="arr">→</span>
              </a>
            </div>
          </div>
          <div className="tier">
            <span className="name">Enterprise</span>
            <span className="amt mono">Custom</span>
            <p className="desc">SSO, on-prem connector hosting, private MCP registry, dedicated success.</p>
            <ul>
              <li>SSO · SCIM · audit log streaming</li>
              <li>On-prem / VPC connector runners</li>
              <li>Private MCP tool registry</li>
              <li>Custom connectors built to spec</li>
              <li>Dedicated FinOps engineer</li>
              <li>Procurement-ready MSA</li>
            </ul>
            <div className="cta">
              <a href="mailto:chandanirving@gmail.com?subject=nable Enterprise"
                 className="btn btn-ghost" style={{width:"100%",justifyContent:"center"}}
                 onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'pricing',tier:'enterprise'}); }}>
                Talk to founders
              </a>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

/* Foot CTA */
function FootCta(){
  return (
    <section className="foot-cta" id="cta">
      <div className="foot-cta-grid"></div>
      <div className="wrap" style={{position:"relative"}}>
        <div className="eyebrow" style={{marginBottom:32,display:"inline-flex"}}><span className="d"></span> Free tier · no credit card</div>
        <h2 className="display">
          Stop building dashboards.<br/>
          <em>Start asking questions.</em>
        </h2>
        <div style={{marginTop:48,display:"flex",flexDirection:"column",alignItems:"center",gap:16}}>
          <div style={{display:"flex",alignItems:"center",gap:14}}>
            <a href="https://buy.stripe.com/eVq14mbe9ffE3le3wC2Nq02"
               className="btn btn-primary" style={{padding:"14px 22px",fontSize:14}}
               onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'footer_cta',cta:'install'}); }}>
              Get started free <span className="arr">→</span>
            </a>
            <a href="mailto:chandanirving@gmail.com?subject=nable - talk to founders"
               className="btn btn-ghost" style={{padding:"14px 22px",fontSize:14}}
               onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'footer_cta',cta:'talk_to_founders'}); }}>
              Talk to founders
            </a>
          </div>
          <EmailCapture source="footer" placeholder="drop your email, we'll send the setup guide" btnLabel="Send it" center={true} />
        </div>
        <p className="mono" style={{marginTop:32,fontSize:12,color:"var(--fg-3)",letterSpacing:".04em"}}>
          $ pip install finops-mcp &amp;&amp; finops setup
        </p>
      </div>
    </section>
  );
}

function Footer(){
  return (
    <footer>
      <div className="wrap">
        <div className="foot">
          <div>
            <a href="#top" className="logo" style={{marginBottom:18}}>
              <LogoMark />
              <span>nable</span>
            </a>
            <p style={{color:"var(--fg-3)",fontSize:13,maxWidth:"34ch",lineHeight:1.55,marginTop:10}}>Your cloud bill, in your editor. Made in Austin, TX.</p>
          </div>
          <div>
            <h5>Product</h5>
            <a href="#runtime">Runtime</a>
            <a href="#connectors">Connectors</a>
            <a href="#pricing">Pricing</a>
            <a href="#">Changelog</a>
            <a href="#">Status</a>
          </div>
          <div>
            <h5>Resources</h5>
            <a href="/docs.html">Docs</a>
            <a href="/docs.html#quickstart">Quickstart</a>
            <a href="/docs.html#iam">IAM templates</a>
            <a href="/docs.html#security">Security brief</a>
          </div>
          <div>
            <h5>Company</h5>
            <a href="#">About</a>
            <a href="#">Investors</a>
            <a href="mailto:chandanirving@gmail.com">Contact</a>
          </div>
        </div>
        <div className="foot-meta">
          <span>2026 nable, inc. · all rights reserved</span>
          <span>finops-mcp / 0.8.36 · runtime healthy</span>
        </div>
      </div>
    </footer>
  );
}

/* Tweaks panel */
const PALETTE_OPTIONS = [
  {value:"onyx",     label:"Onyx",     swatch:["#0a0a0c","#5fe8a0","#15151a"]},
  {value:"graphite", label:"Graphite", swatch:["#15140f","#e4a76b","#221f17"]},
  {value:"paper",    label:"Paper",    swatch:["#fbfaf7","#1f8a5b","#e3dfcf"]},
  {value:"mono",     label:"Mono",     swatch:["#ffffff","#0a0a0a","#e6e6e3"]},
];

function PaletteSwatches({ value, onChange }){
  return (
    <div style={{display:"grid",gridTemplateColumns:"repeat(2,1fr)",gap:8,marginTop:6}}>
      {PALETTE_OPTIONS.map(o => {
        const on = o.value === value;
        return (
          <button key={o.value} type="button" onClick={()=>onChange(o.value)}
            style={{
              display:"flex",alignItems:"center",gap:8,padding:"7px 9px",
              border:"1px solid",borderColor: on ? "var(--accent)" : "rgba(255,255,255,.12)",
              borderRadius:7,background:"rgba(255,255,255,.03)",color:"var(--fg)",
              fontFamily:"'DM Sans',sans-serif",fontSize:12,cursor:"pointer",
              boxShadow: on ? "0 0 0 2px rgba(95,232,160,.18)" : "none",
              transition:".15s"
            }}>
            <span style={{display:"flex",borderRadius:4,overflow:"hidden",border:"1px solid rgba(255,255,255,.08)",flexShrink:0}}>
              {o.swatch.map((c,i) => <span key={i} style={{width:10,height:18,background:c,display:"block"}}/>)}
            </span>
            <span>{o.label}</span>
          </button>
        );
      })}
    </div>
  );
}

function Tweaks(){
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  useEffect(() => { applyPalette(t.palette); }, [t.palette]);
  useEffect(() => { window.dispatchEvent(new CustomEvent("nable:tweaks", {detail:t})); }, [t]);

  return (
    <TweaksPanel title="Tweaks">
      <TweakSection label="Theme">
        <PaletteSwatches value={t.palette} onChange={(v)=>setTweak("palette",v)} />
      </TweakSection>
      <TweakSection label="Layout">
        <TweakRadio label="Hero arrangement" value={t.layout}
          options={[{value:"split",label:"Split"},{value:"editorial",label:"Editorial"}]}
          onChange={(v)=>setTweak("layout",v)} />
      </TweakSection>
      <TweakSection label="Interaction">
        <TweakRadio label="Console queries" value={t.interaction}
          options={[{value:"cycling",label:"Auto"},{value:"static",label:"Manual"}]}
          onChange={(v)=>setTweak("interaction",v)} />
      </TweakSection>
    </TweaksPanel>
  );
}

/* App */
function App(){
  const [t, setT] = useState(TWEAK_DEFAULTS);
  useEffect(() => {
    applyPalette(t.palette);
    function onTweaks(e){ setT(e.detail); }
    window.addEventListener("nable:tweaks", onTweaks);
    return () => window.removeEventListener("nable:tweaks", onTweaks);
  }, []);

  return (
    <>
      <Nav />
      <Hero layout={t.layout} interaction={t.interaction} />
      <QMarquee />
      <Thesis />
      <Architecture />
      <Connectors />
      <Pricing />
      <FootCta />
      <Footer />
      <Tweaks />
    </>
  );
}

ReactDOM.createRoot(document.getElementById("app")).render(<App />);
