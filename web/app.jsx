const { useState, useEffect, useRef } = React;

/* tweak defaults */
const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "palette": "graphite",
  "layout": "split",
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
    "--bg":"#0d0f10","--bg-1":"#111416","--bg-2":"#181c1f","--bg-3":"#1e2327",
    "--line":"#242a2e","--line-2":"#2e3539",
    "--fg":"#f0f2f3","--fg-2":"#94a3ab","--fg-3":"#56656d","--fg-4":"#2d3a40",
    "--accent":"#4db8d4","--accent-dim":"#2c7d91",
    "--warn":"#e6a840","--alert":"#e05c4b",
    "--success":"#3cba7a",
    "--grid":"rgba(255,255,255,.02)"
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

/* Scroll depth tracking */
function useScrollTracking() {
  useEffect(() => {
    if (!window.posthog) return;
    const sections = ['connectors', 'depth', 'architecture', 'pricing', 'faq', 'foot-cta'];
    const seen = new Set();
    const observer = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting && !seen.has(entry.target.id)) {
          seen.add(entry.target.id);
          posthog.capture('section_viewed', { section: entry.target.id });
        }
      });
    }, { threshold: 0.2 });
    sections.forEach(id => {
      const el = document.getElementById(id);
      if (el) observer.observe(el);
    });
    return () => observer.disconnect();
  }, []);
}

/* Email capture: posts to /api/subscribe */
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
      <p style={{fontFamily:"'Space Grotesk',system-ui,sans-serif",fontSize:12,color:"var(--accent)",letterSpacing:".02em",
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
          color:"var(--alert)",fontFamily:"'Space Grotesk',system-ui,sans-serif"}}>
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

/* Ticker */
function Ticker({ installs, version }){
  return (
    <div className="ticker">
      <div className="ticker-inner">
        <span className="seg">
          <span className="dot"></span>
          <b>nable</b>
          <span>runtime healthy</span>
        </span>
        <span className="sep">·</span>
        <span className="seg">4k+ PyPI downloads / mo</span>
        <span className="sep">·</span>
        <span className="seg">17 connectors · AWS · Azure · GCP +14</span>
        <span className="sep">·</span>
        <span className="seg">
          <a href="/about" style={{color:"var(--accent)",textDecoration:"none",fontWeight:500}}>
            About &amp; investors →
          </a>
        </span>
      </div>
    </div>
  );
}

/* Nav */
function Nav(){
  const [open, setOpen] = useState(false);

  function scrollTo(id){
    document.getElementById(id)?.scrollIntoView({behavior:'smooth'});
    setOpen(false);
  }

  return (
    <nav className="nav">
      <div className="nav-inner">
        <a href="/" className="logo">
          <LogoMark />
          <span><span style={{color:'var(--accent)'}}>n</span>able</span>
        </a>
        <ul>
          <li><button className="nav-link" onClick={()=>scrollTo('connectors')}>Connectors</button></li>
          <li><button className="nav-link" onClick={()=>scrollTo('pricing')}>Pricing</button></li>
          <li><button className="nav-link" onClick={()=>{ scrollTo('faq'); if(window.posthog) posthog.capture('nav_clicked',{item:'faq'}); }}>FAQ</button></li>
          <li><a href="/docs.html" onClick={()=>{ if(window.posthog) posthog.capture('docs_clicked',{location:'nav'}); }}>Docs</a></li>
          <li><a href="https://github.com/chaandannn/finopsmcp" target="_blank" rel="noopener noreferrer"
                 onClick={()=>{ if(window.posthog) posthog.capture('nav_clicked',{item:'github'}); }}>GitHub</a></li>
        </ul>
        <div className="right">
          <a href="/account.html" className="nav-signin">Sign in</a>
          <a href="https://calendar.app.google/2duYBqjLXaTmX5xC8" target="_blank" rel="noopener noreferrer" className="btn btn-ghost"
             onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'nav',cta:'book_demo'}); }}>Book a demo</a>
          <a href="/docs.html" className="btn btn-primary"
             onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'nav',cta:'start_free'}); }}>
            Get started free <span className="arr">→</span>
          </a>
        </div>
        <button
          className="nav-hamburger"
          aria-label={open ? "Close menu" : "Open menu"}
          aria-expanded={open}
          onClick={()=>setOpen(o=>!o)}
        >
          {open ? (
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <path d="M4 4L16 16M16 4L4 16" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
            </svg>
          ) : (
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <path d="M3 5h14M3 10h14M3 15h14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
            </svg>
          )}
        </button>
      </div>
      {open && (
        <div className="nav-mobile-menu">
          <button className="nav-mobile-item" onClick={()=>scrollTo('connectors')}>Connectors</button>
          <button className="nav-mobile-item" onClick={()=>{ scrollTo('pricing'); if(window.posthog) posthog.capture('nav_clicked',{item:'pricing'}); }}>Pricing</button>
          <button className="nav-mobile-item" onClick={()=>{ scrollTo('faq'); if(window.posthog) posthog.capture('nav_clicked',{item:'faq'}); }}>FAQ</button>
          <a className="nav-mobile-item" href="/docs.html" onClick={()=>{ setOpen(false); if(window.posthog) posthog.capture('docs_clicked',{location:'nav_mobile'}); }}>Docs</a>
          <a className="nav-mobile-item" href="https://github.com/chaandannn/finopsmcp" target="_blank" rel="noopener noreferrer"
             onClick={()=>{ setOpen(false); if(window.posthog) posthog.capture('nav_clicked',{item:'github'}); }}>GitHub</a>
          <div style={{marginTop:24,display:"flex",flexDirection:"column",gap:10}}>
            <a href="/account.html" className="btn btn-ghost" style={{justifyContent:"center"}} onClick={()=>setOpen(false)}>Sign in</a>
            <a href="/docs.html" className="btn btn-primary" style={{justifyContent:"center"}}
              onClick={()=>{ setOpen(false); if(window.posthog) posthog.capture('cta_clicked',{location:'nav_mobile',cta:'start_free'}); }}>
              Get started free <span className="arr">→</span>
            </a>
          </div>
        </div>
      )}
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
            <h1 className="display">
              Your cloud and AI bill, answered.
            </h1>
            <p className="lede">
              Ask your AWS, Azure, GCP and AI spend anything, inside Claude or Cursor. It runs on your machine, and no vendor holds your data.
            </p>
            <InstallRow />
            <p className="install-note">Free for solo use, no credit card · <a href="/docs.html#install" onClick={() => { if(window.posthog) posthog.capture('cta_clicked', { location:'hero', cta:'docs_install' }); }}>VS Code, Windsurf, Zed and more</a> · <a href="https://calendar.app.google/2duYBqjLXaTmX5xC8" target="_blank" rel="noopener noreferrer" onClick={() => { if(window.posthog) posthog.capture('cta_clicked', { location:'hero', cta:'book_demo' }); }}>or book a live demo</a></p>
            <TrustStrip />
            <div className="hero-mobile-cta">
              <div className="mini-console" aria-label="Example: nable answering a cost question">
                <div className="mc-bar"><span className="mc-dots"><i/><i/><i/></span><span className="mc-title">claude · mcp[nable]</span></div>
                <div className="mc-body">
                  <div className="mc-row"><span className="mc-who">YOU</span><span>Where are we wasting money on EC2?</span></div>
                  <div className="mc-row"><span className="mc-who mc-n">NABLE</span><span>11 instances under 15% CPU for 14 days. Rightsizing them saves <b className="mc-save">$1,840/mo</b>. Want the PR?</span></div>
                </div>
              </div>
              <p className="hmc-lead">nable sets up in your terminal, so do it on your laptop. Drop your email and we'll send the 60-second setup guide.</p>
              <EmailCapture source="hero_mobile" placeholder="your@email.com" btnLabel="Get the guide" />
              <div className="hmc-links">
                <a href="#pricing" className="hmc-pro"
                  onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'hero_mobile',cta:'pricing_40'}); }}>
                  See Team · $1,000/mo flat <span className="arr">→</span>
                </a>
                <a href="https://calendar.app.google/2duYBqjLXaTmX5xC8" target="_blank" rel="noopener noreferrer" className="hmc-book"
                  onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'hero_mobile',cta:'book_demo'}); }}>
                  Book a live demo <span className="arr">→</span>
                </a>
              </div>
            </div>
          </div>
          <div className="hero-right">
            <Console interaction={interaction} />
          </div>
        </div>
      </div>
    </header>
  );
}

const CURSOR_DEEPLINK = "cursor://anysphere.cursor-deeplink/mcp/install?name=nable&config=eyJjb21tYW5kIjogInV2eCIsICJhcmdzIjogWyJmaW5vcHMtbWNwIl19";

const INSTALL_POPUPS = {
  claude: {
    title: "Install in Claude Desktop",
    steps: [
      <>In your terminal, run the command below. <code>finops welcome</code> writes your Claude Desktop config and stores credentials in your OS keychain.</>,
      <>Restart Claude Desktop. nable connects as a local MCP server.</>,
    ],
    cmdLabel: "In your terminal",
    cmd: "uvx --from finops-mcp finops welcome",
    altCmd: "pip install -U finops-mcp && finops welcome",
    note: "uv installs a matching Python for you, so this works on any setup. No uv? brew install uv. Runs on your machine, no nable backend.",
  },
  openai: {
    title: "Install in OpenAI Codex",
    steps: [
      <>In your terminal, install nable and store credentials in your OS keychain:</>,
      <>Add nable to your Codex MCP config below, then restart Codex.</>,
    ],
    cmdLabel: "In your terminal",
    cmd: "uvx --from finops-mcp finops welcome",
    altCmd: "pip install -U finops-mcp && finops welcome",
    toml: '[mcp_servers.nable]\ncommand = "uvx"\nargs = ["finops-mcp"]',
    tomlPath: "~/.codex/config.toml",
    note: "uv installs a matching Python automatically. The ChatGPT app needs a hosted connector, on the roadmap.",
  },
};

function CopyCmd({ cmd }){
  const [copied, setCopied] = useState(false);
  return (
    <button className="copycmd" onClick={() => {
      navigator.clipboard?.writeText(cmd);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
      if(window.posthog) posthog.capture('install_copied');
    }}>
      <span className="prompt">$</span>
      <span className="cmd">{cmd}</span>
      <span className="copylab">{copied ? "copied" : "copy"}</span>
    </button>
  );
}

function InstallPopup({ id, onClose }){
  const p = INSTALL_POPUPS[id];
  if(!p) return null;
  return (
    <div className="install-pop" role="dialog" aria-label={p.title}>
      <div className="install-pop-head">
        <span className="ipt">{p.title}</span>
        <button className="ipx" onClick={onClose} aria-label="Close">×</button>
      </div>
      <ol className="install-steps">
        {p.steps.map((s, i) => <li key={i}>{s}</li>)}
      </ol>
      {p.cmdLabel && (
        <span className="install-cmdlabel">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="M2.5 3.5L5 6l-2.5 2.5M6.5 8.5h3" strokeLinecap="round" strokeLinejoin="round"/></svg>
          {p.cmdLabel}
        </span>
      )}
      <CopyCmd cmd={p.cmd} />
      {p.altCmd && (
        <p className="install-alt">Already on Python 3.10+? <code>{p.altCmd}</code></p>
      )}
      {p.toml && (
        <div className="install-toml">
          <span className="tomlpath">Add to <code>{p.tomlPath}</code></span>
          <pre>{p.toml}</pre>
        </div>
      )}
      <p className="install-pop-note">{p.note}</p>
    </div>
  );
}

const _CHEV = <svg className="chev" width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><path d="M3 4.5l3 3 3-3" strokeLinecap="round" strokeLinejoin="round"/></svg>;

function InstallRow(){
  const [open, setOpen] = useState(null);
  const toggle = (id) => {
    setOpen(o => o === id ? null : id);
    if(window.posthog) posthog.capture('install_opened', { client: id });
  };
  return (
    <div className="installer" id="install">
      <span className="install-cmdlabel">
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="M2.5 3.5L5 6l-2.5 2.5M6.5 8.5h3" strokeLinecap="round" strokeLinejoin="round"/></svg>
        Run this in your terminal
      </span>
      <CopyCmd cmd="uvx --from finops-mcp finops welcome" />
      <div className="install-row">
        <a className="iclient is-primary" href={CURSOR_DEEPLINK}
          onClick={() => { if(window.posthog) posthog.capture('cta_clicked', { location:'hero', cta:'add_to_cursor' }); }}>
          <span>Install in <b>Cursor</b></span>
          <svg className="ic" width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><path d="M4 8l4-4m0 0H4.5M8 4v3.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
        </a>
        <button className={"iclient" + (open === 'claude' ? " is-open" : "")} aria-expanded={open === 'claude'} onClick={() => toggle('claude')}>
          <span>Install in <b>Claude</b></span>{_CHEV}
        </button>
        <button className={"iclient" + (open === 'openai' ? " is-open" : "")} aria-expanded={open === 'openai'} onClick={() => toggle('openai')}>
          <span>Install in <b>OpenAI</b></span>{_CHEV}
        </button>
      </div>
      {open && <InstallPopup id={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

function fmtNum(n){
  if(n >= 1000) return (n/1000).toFixed(1).replace(/\.0$/,"") + "k";
  return String(n);
}

function TrustStrip(){
  const items = [
    {lab:"built-in tools", val: "165+", sub:"cost, anomaly, rightsizing"},
    {lab:"providers", val:"17", sub:"AWS · Azure · GCP +"},
    {lab:"on our servers", val:"0 bytes", sub:"nable has no backend"},
  ];
  return (
    <div className="trust">
      {items.map((t,i) => (
        <div className="ti" key={i}>
          <span className="lab">{t.lab}</span>
          <span className="val mono">{t.val}</span>
        </div>
      ))}
    </div>
  );
}

/* Console (interactive demo terminal) */
const QUERIES = [
  {
    q: "Where are we wasting money on EC2?",
    response: (
      <>
        <p>Cross-referenced CloudWatch metrics with Compute Optimizer. 11 instances are sustained below 15% CPU over 14 days. Top four by savings:</p>
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
  // Render the first answer immediately on mount, so the proof is on screen
  // before any animation runs. Cycling queries animate normally after that.
  const [phase, setPhase] = useState("answered");
  const [typed, setTyped] = useState(QUERIES[0].q);
  const timers = useRef([]);
  const firstRun = useRef(true);

  useEffect(() => {
    if(firstRun.current){
      firstRun.current = false;
      return;
    }
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
        timers.current.push(setTimeout(() => setPhase("thinking"), 300));
        timers.current.push(setTimeout(() => setPhase("answered"), 1050));
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
        <span style={{color:"var(--fg-4)"}}>live walkthrough · click to explore</span>
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
    {n:"02 · Shift", h:"FinOps moved from a quarterly review to a real-time question.", p:"AI editors made conversational access to live data the default interface. Asking \"what spiked\" is now cheaper than building a dashboard. The dashboard era is the legacy era."},
    {n:"03 · Moat", h:"Local-first compounds with every connector.", p:"Credentials in the OS keyring. No data lake. No SOC-2 surface area. Each new connector is a feature shipment, not a security review. Enterprise sells itself."},
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

/* Depth */
function Depth(){
  const cards = [
    {
      n: "01",
      h: "Your biggest savings, in one question.",
      p: "Ask 'where am I wasting money?' and get a ranked list of every opportunity across your infrastructure, sorted by dollar impact. No dashboard to configure. No report to schedule. No knowing what to look for. Just results.",
      chips: ["ranked by $","works day one","no setup","19 scanners"],
    },
    {
      n: "02",
      h: "From recommendation to merged PR.",
      p: "Most tools stop at 'you should downsize that.' nable reads your Terraform, patches the file, and opens the pull request. After it merges, nable checks whether the saving actually landed and records the realized amount.",
      chips: ["Terraform","PR opened","saving verified","end-to-end"],
    },
    {
      n: "03",
      h: "AI spend tracked like a first-class cost.",
      p: "Bedrock, OpenAI, Anthropic. These don't fit in the usual cost buckets. nable tracks AI spend by model and by team, so it shows up as a first-class line in every report instead of a mystery lump buried in the bill.",
      chips: ["by model","by team","first-class","AI-native"],
    },
    {
      n: "04",
      h: "Always-on, or on demand.",
      p: "Ask in your editor whenever you want, or run `finops serve` for always-on monitoring that catches spikes 24/7. When spend jumps, nable attributes the anomaly to the team or service that caused it and alerts whoever owns it in Slack or Teams. Before finance notices.",
      chips: ["always-on or on-demand","team attribution","Slack / Teams","28-day baseline"],
    },
  ];

  return (
    <section id="depth" style={{borderTop:"1px solid var(--line)"}}>
      <div className="wrap">
        <div className="section-head">
          <div className="label">What's under the hood</div>
          <h2>Not a pipe.<br/><em>An analyst.</em></h2>
          <p>The value isn't connecting Claude to your bill. It's the analysis that runs before Claude ever responds.</p>
        </div>
        <div className="depth-grid">
          {cards.map((c,i) => (
            <div className="depth-card" key={i}>
              <span className="depth-n">{c.n}</span>
              <h3 className="depth-h">{c.h}</h3>
              <p className="depth-p">{c.p}</p>
              <div className="depth-chips">
                {c.chips.map((ch,j) => <span key={j}>{ch}</span>)}
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

/* AI cost */
function AiCost(){
  const copy = () => {
    if(navigator.clipboard) navigator.clipboard.writeText("uvx --from finops-mcp finops welcome");
    if(window.posthog) posthog.capture('cta_clicked',{location:'ai_cost',cta:'copy_install'});
  };
  return (
    <section id="ai" className="alt" style={{borderTop:"1px solid var(--line)"}}>
      <div className="wrap">
        <div className="ee-grid">
          <div className="ee-left">
            <div className="label">Your AI bill</div>
            <h2>Tools chart your AI spend.<br/><em>nable finds the waste.</em></h2>
            <p className="ee-lede">Most of an AI bill is input tokens billed at full price, plus calls sent to a frontier model a cheaper one would have answered the same way. nable reads the split from your real usage and shows you the cheapest way to get the same output. No caching guesswork.</p>
            <ul className="ee-points">
              <li><span className="ee-plus">+</span><span>Input, output and cache, <b>split from your actual bill</b></span></li>
              <li><span className="ee-plus">+</span><span>Flags <b>frontier-model calls</b> a cheaper model handles the same</span></li>
              <li><span className="ee-plus">+</span><span>Separates <b>what you can bank today</b> from what needs a closer look</span></li>
            </ul>
          </div>
          <div className="ee-right">
            <div className="aicost-panel">
              <div className="aicost-tag">Real numbers · real dollars · first scan</div>
              <div className="aicost-stat">
                <div className="aicost-big">89<span className="aicost-unit">%</span></div>
                <p>of an early user's Bedrock bill was input tokens, billed at full price with <b>no caching</b></p>
              </div>
              <div className="aicost-rule"></div>
              <div className="aicost-stat">
                <div className="aicost-big accent">$10.7k<span className="aicost-unit">/yr</span></div>
                <p><b>= $896/mo</b> in prompt-caching savings, about a quarter of the AI bill, on the first scan</p>
              </div>
              <div className="aicost-foot">From an early user's first scan. Real numbers, name withheld for now.</div>
              <div className="aicost-cta">
                <span className="aicost-cta-l">This is a small account. See your own number, free:</span>
                <code className="aicost-cmd" onClick={copy}>uvx --from finops-mcp finops welcome</code>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

/* Architecture */
function Architecture({ version }){
  return (
    <section id="arch" className="alt">
      <div className="wrap">
        <div className="section-head">
          <div className="label">Architecture</div>
          <h2>No vendor backend,<br/><em>by design.</em></h2>
          <p>nable is not SaaS. It runs on your own machine, holds credentials in the OS keyring, queries provider APIs directly, and surfaces tools to whichever AI editor is open. Your credentials never leave your machine, and your cost data never touches a nable server, there is no backend or data lake to breach. The figures you ask about go to your editor's own AI to answer the question, the same as any prompt, and nowhere else.</p>
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
                <span className="sub">nable</span>
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

/* How it works — Connect / Ask / Act */
const STEPS = [
  { n:"01", h:"Connect", p:"Point nable at AWS, Azure, GCP and 14 more sources. Credentials land in your OS keyring, never on our servers.", ex:"finops setup aws" },
  { n:"02", h:"Ask",     p:"Open Claude, Cursor, or any MCP editor and just ask. nable turns the question into live, read-only API calls.", ex:'"What drove our bill up last week?"' },
  { n:"03", h:"Act",     p:"Approve a rightsizing PR, open a ticket, post to Slack. Answers become actions, every one written to an audit log.", ex:'"Open a PR to downsize the idle instances."' },
];

function HowItWorks(){
  return (
    <section id="how" style={{borderTop:"1px solid var(--line)"}}>
      <div className="wrap">
        <div className="section-head">
          <div className="label">How it works</div>
          <h2>Live in <em>four minutes.</em></h2>
          <p>No data pipeline. No dashboard to build. A single MCP entry turns any AI editor into a FinOps console.</p>
        </div>
        <div className="steps">
          {STEPS.map((s,i) => (
            <div className="step" key={i}>
              <div className="step-n">{s.n}</div>
              <h3 className="step-h">{s.h}</h3>
              <p className="step-p">{s.p}</p>
              <div className="step-ex">{s.ex}</div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

/* One entry. Every editor. — tabbed runtime config */
const EDITOR_TABS = [
  { id:"terminal", label:"Terminal", bar:"bash", lines:[
    { k:"cmd", t:"$ uvx --from finops-mcp finops welcome" },
    { k:"dim", t:"  fetching finops-mcp + a matching python…" },
    { k:"ok",  t:"✓ runtime registered · ask nable in your editor" },
  ]},
  { id:"claudecode", label:"Claude Code", bar:"terminal claude cli · /plugin", lines:[
    { k:"dim", t:"# in the terminal claude cli, run one at a time" },
    { k:"cmd", t:"/plugin marketplace add chaandannn/finopsmcp" },
    { k:"cmd", t:"/plugin install nable@nable" },
    { k:"ok",  t:"✓ nable installed · ask in your editor" },
  ]},
  { id:"claude", label:"Claude Desktop", bar:"claude_desktop_config.json", lines:[
    { k:"p", t:"{" },
    { k:"p", t:'  "mcpServers": {' },
    { k:"p", t:'    "nable": {' },
    { k:"p", t:'      "command": "uvx",' },
    { k:"p", t:'      "args": ["finops-mcp"]' },
    { k:"p", t:"    }" },
    { k:"p", t:"  }" },
    { k:"p", t:"}" },
  ]},
  { id:"cursor", label:"Cursor", bar:"~/.cursor/mcp.json", lines:[
    { k:"p", t:"{" },
    { k:"p", t:'  "mcpServers": {' },
    { k:"p", t:'    "nable": { "command": "uvx", "args": ["finops-mcp"] }' },
    { k:"p", t:"  }" },
    { k:"p", t:"}" },
  ]},
];

function EveryEditor(){
  const [tab, setTab] = useState("terminal");
  const active = EDITOR_TABS.find(t => t.id === tab) || EDITOR_TABS[0];
  const copy = () => {
    if(navigator.clipboard) navigator.clipboard.writeText(active.lines.map(l => l.t).join("\n"));
    if(window.posthog) posthog.capture('cta_clicked',{location:'every_editor',cta:'copy_config',tab});
  };
  return (
    <section id="editors" className="alt" style={{borderTop:"1px solid var(--line)"}}>
      <div className="wrap">
        <div className="ee-grid">
          <div className="ee-left">
            <div className="label">Runtime</div>
            <h2>One entry.<br/><em>Every editor.</em></h2>
            <p className="ee-lede">nable speaks the Model Context Protocol, so the same runtime works in whatever your team already uses. Drop in the config, restart, and ask.</p>
            <ul className="ee-points">
              <li><span className="ee-plus">+</span><span><b>160+ tools</b> your AI can call, from a cost question to an open PR</span></li>
              <li><span className="ee-plus">+</span><span>Tracks <b>AI spend by model</b> alongside cloud, Kubernetes, and SaaS</span></li>
              <li><span className="ee-plus">+</span><span>Real API integrations, with <b>new connectors every month</b></span></li>
            </ul>
            <div className="ee-runs">RUNS IN <b>CLAUDE</b> · <b>CURSOR</b> · <b>VS CODE</b> · <b>ZED</b> · <b>WINDSURF</b> · <b>CLINE</b></div>
          </div>
          <div className="ee-right">
            <div className="ee-panel">
              <div className="ee-tabs">
                {EDITOR_TABS.map(t => (
                  <button key={t.id} className={"ee-tab" + (t.id===tab ? " on" : "")} onClick={()=>setTab(t.id)}>{t.label}</button>
                ))}
              </div>
              <div className="ee-bar">
                <span className="ee-dots"><i/><i/><i/></span>
                <span className="ee-file">{active.bar}</span>
                <span className="ee-copy" onClick={copy}>copy</span>
              </div>
              <pre className="ee-code">{active.lines.map((l,i) => (
                <div key={i} className={"ee-ln ee-" + l.k}>{l.t}</div>
              ))}</pre>
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
  {nm:"New Relic",  px:"Data ingest · user counts",      tag:"live"},
  {nm:"Databricks", px:"DBU usage · job costs",          tag:"live"},
  {nm:"OpenAI",     px:"Usage API · per-model spend",    tag:"live"},
  {nm:"Anthropic",  px:"Org usage · per-model spend",    tag:"live"},
  {nm:"Stripe",     px:"Billing meter · platform fees",  tag:"beta"},
  {nm:"PagerDuty",  px:"License spend · on-call costs",  tag:"beta"},
  {nm:"Coming soon",px:"Vote on the next connector",     tag:"soon"},
];

const LOGOS = [
  {n:"AWS",f:"aws"},{n:"Azure",f:"azure"},{n:"GCP",f:"gcp"},
  {n:"OpenAI",f:"openai",icon:true},{n:"Anthropic",f:"anthropic",icon:true},
  {n:"Stripe",f:"stripe"},{n:"Datadog",f:"datadog",icon:true},
  {n:"Snowflake",f:"snowflake"},{n:"GitHub",f:"github"},
];

function Connectors(){
  return (
    <section id="connectors" className="alt">
      <div className="wrap">
        <div className="section-head">
          <div className="label">Connectors</div>
          <h2>All 17 sources,<br/><em>one conversation.</em></h2>
          <p>Every connector is a real API integration, not a CSV export. New providers ship monthly.</p>
        </div>
      </div>
      <div className="logo-marquee">
        <div className="logo-track">
          {[...LOGOS, ...LOGOS, ...LOGOS].map((l,i) => (
            <img className={"logo-img" + (l.icon ? " is-icon" : "")} key={i} src={"/vendor/logos/" + l.f + ".svg"} alt={l.n} title={l.n} loading="lazy" />
          ))}
        </div>
      </div>
      <div className="wrap">
        <p className="logo-band-note">+ 8 more connectors · new providers ship monthly</p>
      </div>
    </section>
  );
}


/* Pricing */


function CheckIcon(){
  return (
    <svg width="15" height="15" viewBox="0 0 15 15" fill="none" aria-hidden="true" style={{flexShrink:0,marginTop:1}}>
      <circle cx="7.5" cy="7.5" r="7" stroke="currentColor" strokeWidth="1"/>
      <path d="M4.5 7.5L6.5 9.5L10.5 5.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}

// Pro checkout: $100/mo or $1,000/yr (the renamed previous "team" product).
const PRO_MONTHLY_LINK = "https://buy.stripe.com/9B600igyt1oO1d69V02Nq06";
const PRO_ANNUAL_LINK = "https://buy.stripe.com/bJe5kCbe97Nc0924AG2Nq07";
// Team checkout: $1,000/mo or $10,000/yr (2 months free). Adds the Slack bot.
const MONTHLY_STRIPE_LINK = "https://buy.stripe.com/3cI3cucid6J85tm3wC2Nq08";
const ANNUAL_STRIPE_LINK = "https://buy.stripe.com/14A6oG0zvgjI9JCffk2Nq09";

const BOOK_CALL_LINK = "https://calendar.app.google/2duYBqjLXaTmX5xC8";

// Comparison rows. value true -> check, false -> dash, string -> mono text.
const PRICE_ROWS = [
  { label: "Seats",                                          solo: "1",         pro: "unlimited",  team: "unlimited", ent: "unlimited" },
  { label: "Core FinOps: cost queries, anomalies, rightsizing, AI/LLM tracking, 17 connectors, local-first", solo: true, pro: true, team: true, ent: true },
  { label: "AWS cost data",                                  solo: "Cost Explorer", pro: "Explorer + CUR", team: "Explorer + CUR", ent: "Explorer + CUR" },
  { label: "Terraform remediation: patch + open the PR",     solo: false,       pro: true,         team: true,       ent: true },
  { label: "Slack / Teams alerts, digests + tickets (Jira, Linear, GitHub)", solo: false, pro: true, team: true,     ent: true },
  { label: "Budgets, commitments + BI dashboards",           solo: false,       pro: true,         team: true,       ent: true },
  { label: "Conversational Slack bot: ask the bill anything", solo: false,      pro: false,        team: true,       ent: true },
  { label: "RCA + chat remediation: drafts the fix, a human approves", solo: false, pro: false,    team: true,       ent: true },
  { label: "Managed AI included (or bring your own key)",    solo: false,       pro: false,        team: true,       ent: true },
  { label: "SSO + audit logs",                               solo: false,       pro: false,        team: false,      ent: true },
  { label: "Support",                                        solo: "Community", pro: "Email",      team: "Email",     ent: "Slack + SLA" },
];

function PCell({ v }){
  if (v === true)  return <span className="pcheck"><CheckIcon /></span>;
  if (v === false) return <span className="pdash">–</span>;
  return <span className="pval">{v}</span>;
}

// Mobile-only: stack the tiers into cards (the comparison table is unreadable on a phone).
function PricingCards({ annual, proPrice, proPer, proSub, proLink, proPlan, teamPrice, teamPer, teamSub, teamLink, teamPlan }){
  const tiers = [
    { key:"solo", name:"Solo", price:"Free", per:"forever", sub:null, rec:false, primary:false,
      cta:"Get started", href:"/docs.html", plan:"solo", ext:false },
    { key:"pro", name:"Pro", price:proPrice, per:proPer, sub:proSub, rec:false, primary:false,
      cta:annual?"Get annual":"Get Pro", href:proLink, plan:proPlan, ext:true },
    { key:"team", name:"Team", price:teamPrice, per:teamPer, sub:teamSub, rec:true, primary:true,
      cta:annual?"Get annual":"Get Team", href:teamLink, plan:teamPlan, ext:true },
    { key:"ent", name:"Enterprise", price:"Custom", per:"", sub:null, rec:false, primary:false,
      cta:"Contact us", href:BOOK_CALL_LINK, plan:"enterprise", ext:true },
  ];
  return (
    <div className="pcards">
      {tiers.map(t => (
        <div className={"pcard" + (t.rec ? " pcard-rec" : "")} key={t.key}>
          {t.rec && <div className="pcard-badge">Recommended</div>}
          <div className="pcard-name">{t.name}</div>
          <div className="pcard-price"><span className="pcard-amt">{t.price}</span>{t.per && <span className="pcard-per">{t.per}</span>}</div>
          {t.sub && <div className="pcard-sub">{t.sub}</div>}
          <a className={"btn " + (t.primary ? "btn-primary" : "btn-ghost") + " pcard-cta"}
             href={t.href} {...(t.ext ? {target:"_blank", rel:"noopener noreferrer"} : {})}
             onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'pricing_mobile',plan:t.plan,billing:annual?'annual':'monthly'}); }}>
            {t.cta}</a>
          <ul className="pcard-feats">
            {PRICE_ROWS.filter(r => r[t.key] !== false).map((r,i) => (
              <li key={i}><CheckIcon /><span>{r.label}{typeof r[t.key] === "string" ? <em className="pcard-val"> · {r[t.key]}</em> : null}</span></li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function Pricing(){
  const [annual, setAnnual] = useState(false);

  const proPrice  = annual ? "$1,000" : "$100";
  const proPer    = annual ? "/ yr flat" : "/ mo flat";
  const proSub    = annual ? "$83 / mo · 2 months free" : "7-day free trial";
  const proLink   = annual ? PRO_ANNUAL_LINK : PRO_MONTHLY_LINK;
  const proPlan   = annual ? "pro_annual" : "pro_monthly";

  const teamPrice = annual ? "$10,000" : "$1,000";
  const teamPer   = annual ? "/ yr flat" : "/ mo flat";
  const teamSub   = annual ? "$833 / mo · 2 months free" : "7-day free trial";
  const teamLink  = annual ? ANNUAL_STRIPE_LINK : MONTHLY_STRIPE_LINK;
  const teamPlan  = annual ? "team_annual" : "team_monthly";

  return (
    <section id="pricing">
      <div className="wrap">
        <div className="section-head">
          <div className="label">Pricing</div>
          <h2>Free to ask.<br/><em>Pay to remediate.</em></h2>
          <p>Solo is free forever. Pro adds the remediation layer: PRs, tickets, alerts, dashboards. Team adds the conversational Slack bot and managed AI. Enterprise adds SSO, audit logs, and an SLA.</p>

          {/* Billing toggle */}
          <div style={{display:"flex",alignItems:"center",gap:12,justifyContent:"center",marginTop:24}}>
            <span style={{fontSize:13,color:annual?"var(--fg-3)":"var(--fg)",fontWeight:annual?400:500,transition:"color .15s"}}>Monthly</span>
            <button
              onClick={()=>setAnnual(a=>!a)}
              style={{
                width:44,height:24,borderRadius:6,border:"1px solid var(--line-2)",
                background:annual?"var(--accent)":"var(--bg-2)",
                position:"relative",cursor:"pointer",transition:"background .2s",flexShrink:0,
              }}
              aria-label="Toggle annual billing"
            >
              <span style={{
                position:"absolute",top:3,left:annual?20:3,width:16,height:16,
                borderRadius:"50%",background:annual?"var(--bg)":"var(--fg-3)",
                transition:"left .2s, background .2s",display:"block",
              }}/>
            </button>
            <span style={{display:"flex",alignItems:"center",gap:6}}>
              <span style={{fontSize:13,color:annual?"var(--fg)":"var(--fg-3)",fontWeight:annual?500:400,transition:"color .15s"}}>Annual</span>
              <span style={{fontSize:11,fontWeight:500,color:"var(--success)",background:"rgba(60,186,122,.12)",padding:"2px 7px",borderRadius:2,letterSpacing:".03em"}}>SAVE 17%</span>
            </span>
          </div>
        </div>

        <div className="ptable-wrap">
          <div className="ptable">
            {/* header row */}
            <div className="ph ph-corner"></div>
            <div className="ph">
              <div className="pt-name">Solo</div>
              <div className="pt-price"><span className="pt-amt">Free</span><span className="pt-per">forever</span></div>
              <a className="btn btn-ghost pt-cta" href="/docs.html"
                 onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'pricing',plan:'solo'}); }}>Get started</a>
            </div>
            <div className="ph">
              <div className="pt-name">Pro</div>
              <div className="pt-price"><span className="pt-amt">{proPrice}</span><span className="pt-per">{proPer}</span></div>
              <div className="pt-sub">{proSub}</div>
              <a className="btn btn-ghost pt-cta" href={proLink} target="_blank" rel="noopener noreferrer"
                 onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'pricing',plan:proPlan,billing:annual?'annual':'monthly'}); }}>
                {annual ? "Get annual" : "Get Pro"}</a>
            </div>
            <div className="ph pcol-team">
              <div className="pt-rec">Recommended</div>
              <div className="pt-name">Team</div>
              <div className="pt-price"><span className="pt-amt">{teamPrice}</span><span className="pt-per">{teamPer}</span></div>
              <div className="pt-sub">{teamSub}</div>
              <a className="btn btn-primary pt-cta" href={teamLink} target="_blank" rel="noopener noreferrer"
                 onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'pricing',plan:teamPlan,billing:annual?'annual':'monthly'}); }}>
                {annual ? "Get annual" : "Get Team"}</a>
            </div>
            <div className="ph">
              <div className="pt-name">Enterprise</div>
              <div className="pt-price"><span className="pt-amt">Custom</span></div>
              <a className="btn btn-ghost pt-cta" href={BOOK_CALL_LINK} target="_blank" rel="noopener noreferrer"
                 onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'pricing',plan:'enterprise'}); }}>Contact us</a>
            </div>

            {/* feature rows */}
            {PRICE_ROWS.map((r,i) => (
              <React.Fragment key={i}>
                <div className="pr pr-label">{r.label}</div>
                <div className="pr pr-cell"><PCell v={r.solo} /></div>
                <div className="pr pr-cell"><PCell v={r.pro} /></div>
                <div className="pr pr-cell pcol-team"><PCell v={r.team} /></div>
                <div className="pr pr-cell"><PCell v={r.ent} /></div>
              </React.Fragment>
            ))}
          </div>
        </div>

        <PricingCards annual={annual} proPrice={proPrice} proPer={proPer} proSub={proSub} proLink={proLink} proPlan={proPlan} teamPrice={teamPrice} teamPer={teamPer} teamSub={teamSub} teamLink={teamLink} teamPlan={teamPlan} />

        <p className="pfoot">No credit card for Solo. Team trial requires a card, cancel any time.</p>
        <p className="pfoot pdemo">Weighing Team for your org?{" "}
          <a href="https://calendar.app.google/2duYBqjLXaTmX5xC8" target="_blank" rel="noopener noreferrer"
             onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'pricing',cta:'book_demo'}); }}>
            Book a 20-min demo</a> and we'll run it on your own bill.</p>
      </div>
    </section>
  );
}

/* Mid-page CTA */
function MidCta(){
  return (
    <section id="mid-cta" style={{borderTop:"1px solid var(--line)",borderBottom:"1px solid var(--line)"}}>
      <div className="wrap" style={{paddingTop:72,paddingBottom:72}}>
        <div style={{display:"flex",flexDirection:"column",alignItems:"center",gap:24,textAlign:"center"}}>
          <div>
            <h2 style={{marginBottom:10}}>Ready to stop guessing?</h2>
            <p style={{color:"var(--fg-2)",maxWidth:"46ch",margin:"0 auto",lineHeight:1.6}}>
              Minutes from install to your first real insight. Free forever for solo use.
            </p>
          </div>
          <div style={{display:"flex",alignItems:"center",gap:12,flexWrap:"wrap",justifyContent:"center"}}>
            <a href="/docs.html" className="btn btn-primary"
              onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'mid_cta',cta:'start_free'}); }}>
              Get started free <span className="arr">→</span>
            </a>
            <a href="/docs.html" className="btn btn-ghost"
              onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'mid_cta',cta:'docs'}); }}>
              Read the docs
            </a>
          </div>
          <p className="mono" style={{fontSize:11,color:"var(--fg-4)",letterSpacing:".05em"}}>
            uvx --from finops-mcp finops welcome
          </p>
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
          Stop staring at graphs.<br/>
          <em>Start closing tickets.</em>
        </h2>
        <div style={{marginTop:48,display:"flex",flexDirection:"column",alignItems:"center",gap:16}}>
          <div style={{display:"flex",alignItems:"center",gap:14}}>
            <a href="/docs.html" className="btn btn-primary" style={{padding:"14px 22px",fontSize:14}}
               onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'footer_cta',cta:'install'}); }}>
              Get started free <span className="arr">→</span>
            </a>
            <a href="https://calendar.app.google/2duYBqjLXaTmX5xC8"
               target="_blank" rel="noopener noreferrer"
               className="btn btn-ghost" style={{padding:"14px 22px",fontSize:14}}
               onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'footer_cta',cta:'book_demo'}); }}>
              Book a live demo
            </a>
          </div>
          <EmailCapture source="footer" placeholder="drop your email, we'll send the setup guide" btnLabel="Send it" center={true} />
        </div>
        <p className="mono" style={{marginTop:32,fontSize:12,color:"var(--fg-3)",letterSpacing:".04em"}}>
          $ uvx --from finops-mcp finops welcome
        </p>
        <p style={{marginTop:24,fontSize:13,color:"var(--fg-3)"}}>
          Building something? <a href="/about" style={{color:"var(--accent-dim)"}}>Read the founder note and investor thesis →</a>
        </p>
      </div>
    </section>
  );
}

/* Founder note */
function FounderNote(){
  return (
    <section id="founder" style={{borderTop:"1px solid var(--line)"}}>
      <div className="wrap" style={{maxWidth:680,paddingTop:80,paddingBottom:80}}>
        <div style={{fontFamily:"'Space Grotesk',system-ui,sans-serif",fontWeight:500,fontSize:11,color:"var(--accent-dim)",letterSpacing:".08em",textTransform:"uppercase",display:"flex",alignItems:"center",gap:10,marginBottom:24}}>
          <span style={{width:24,height:1,background:"var(--accent-dim)",display:"inline-block"}}></span>
          Why I built this
        </div>
        <p style={{fontSize:17,lineHeight:1.75,color:"var(--fg-2)",marginBottom:28}}>
          I built this because I spent most of my day bouncing between dashboards that barely showed what I actually needed, the AWS console, and Claude. I'd ask Claude a question, manually paste in numbers, get an answer, then go back and repeat the whole thing.
        </p>
        <p style={{fontSize:17,lineHeight:1.75,color:"var(--fg-2)",marginBottom:28}}>
          A lot of FinOps tools are shipping MCP integrations now. But they're all built for enterprise, priced for enterprise, and none of them fit the way I actually work. They give you visibility. They don't help you think.
        </p>
        <p style={{fontSize:17,lineHeight:1.75,color:"var(--fg-2)",marginBottom:36}}>
          nable solves the problems I actually had. The recommendations go deeper than anything I've seen out of the box, and for the first time I can actually reason through my own optimization opportunities instead of just staring at a graph.
        </p>
        <div style={{display:"flex",alignItems:"center",gap:14}}>
          <div style={{width:40,height:40,borderRadius:"50%",background:"var(--accent)",display:"flex",alignItems:"center",justifyContent:"center",flexShrink:0}}>
            <span style={{fontFamily:"var(--mono)",fontSize:13,fontWeight:600,color:"var(--bg)"}}>CB</span>
          </div>
          <div>
            <div style={{fontSize:14,fontWeight:500,color:"var(--fg)"}}>Chandan Bukkapatnam</div>
            <div style={{fontSize:13,color:"var(--fg-3)"}}>Founder · <a href="mailto:chandan@getnable.com" target="_blank" rel="noopener noreferrer" style={{color:"var(--accent)"}}>chandan@getnable.com</a></div>
          </div>
        </div>
      </div>
    </section>
  );
}

function Footer({ version }){
  return (
    <footer>
      <div className="wrap">
        <div className="foot">
          <div>
            <a href="#top" className="logo" style={{marginBottom:18}}>
              <LogoMark />
              <span>nable</span>
            </a>
            <p style={{color:"var(--fg-3)",fontSize:13,maxWidth:"34ch",lineHeight:1.55,marginTop:10}}>Your cloud and AI bill, answered. Made in Austin, TX.</p>
          </div>
          <div>
            <h5>Product</h5>
            <a href="#connectors">Connectors</a>
            <a href="#pricing">Pricing</a>
            <a href="https://calendar.app.google/2duYBqjLXaTmX5xC8" target="_blank" rel="noopener noreferrer"
               onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'footer_nav',cta:'book_demo'}); }}>Book a demo</a>
            <a href="#faq">FAQ</a>
          </div>
          <div>
            <h5>Resources</h5>
            <a href="/docs.html">Docs</a>
            <a href="/docs.html#quickstart">Quickstart</a>
            <a href="/docs.html#iam">IAM templates</a>
            <a href="/security">Security</a>
          </div>
          <div>
            <h5>Company</h5>
            <a href="/about">About</a>
            <a href="/about#investors">Investors</a>
            <a href="mailto:hello@getnable.com" target="_blank" rel="noopener noreferrer">Contact</a>
            <a href="https://github.com/chaandannn/finopsmcp" target="_blank" rel="noopener noreferrer">GitHub</a>
            <a href="https://www.linkedin.com/company/getnable/" target="_blank" rel="noopener noreferrer">LinkedIn</a>
          </div>
        </div>
        <div className="foot-meta">
          <span>2026 nable · <a href="/privacy" style={{color:"var(--fg-3)"}}>Privacy</a> · <a href="/terms" style={{color:"var(--fg-3)"}}>Terms</a></span>
          <span>nable · runtime healthy</span>
        </div>
      </div>
    </footer>
  );
}

/* FAQ */
const FAQ_ITEMS = [
  {
    q: "How is this different from just asking Claude?",
    a: "Without nable, you copy numbers from dashboards and paste them into Claude. That works for simple questions. But Claude won't know to cross-reference CloudWatch metrics against Compute Optimizer, run Z-score detection against a 28-day baseline, model your Savings Plan coverage gap, or read your Terraform state to find which resource needs changing. nable ships all of that analysis pre-built. When it surfaces a rightsizing rec, it goes further: reads your Terraform state, patches the .tf file, and opens the PR. The finding and the fix happen in the same conversation."
  },
  {
    q: "Where do my credentials and billing data go?",
    a: "Your credentials are stored in your OS keyring (macOS Keychain, Windows Credential Manager, or libsecret on Linux) and never leave your machine. Cost data is cached in a local SQLite database on your machine. nable has no backend, so we never see your cost data or credentials, and there is no vendor data lake to breach. One honest caveat: when you ask a question in your AI editor, the cost figures nable returns are sent to your editor's own AI model to answer the question, the same as anything else you put in that chat. That is the editor's model, not a nable server, and if your org needs zero AI exposure you can use the local dashboard (finops serve) or CLI, which never touch a model. We also collect anonymous, opt-out usage telemetry (which tools you call, your plan tier, and how many providers you connect) via PostHog, never cost figures, account IDs, or credentials."
  },
  {
    q: "What editors does it work with?",
    a: "Claude Desktop, Cursor, Windsurf, Zed, and anything that supports MCP. The setup wizard configures your editor automatically. If you use multiple editors, run the wizard once per editor."
  },
  {
    q: "How long does setup take?",
    a: "A few minutes. Run `uvx --from finops-mcp finops welcome` (uv fetches a matching Python and runs the setup wizard, no PATH setup needed), or `pip install -U finops-mcp && finops welcome` if you're already on Python 3.10+. The wizard connects Claude, connects your cloud, and shows your first cost number right in the terminal. Want to see it first? `uvx --from finops-mcp finops welcome --demo` runs it on sample data."
  },
  {
    q: "Is the free tier actually free?",
    a: "Yes. No credit card, no expiry. The free tier includes cost queries, anomaly detection, rightsizing recommendations, and all 17 connectors. Pro adds remediation PRs, tickets, digests and commitment analysis. Team adds the conversational Slack bot."
  },
  {
    q: "I only have one AWS account. Is this worth it?",
    a: "Yes. Rightsizing and anomaly detection alone are usually worth it. Most people find savings in the first session. You can add more providers later."
  },
  {
    q: "Do you support multiple AWS accounts or organizations?",
    a: "Yes. Run `finops setup aws --add` to connect additional accounts. You can query across all of them in a single conversation. Org-wide rollups across accounts are included in Pro."
  },
  {
    q: "Does it work in AWS GovCloud?",
    a: "Yes. nable runs entirely on your machine and queries your cloud provider APIs directly. There are no nable servers in the middle, no data lake, and no SaaS authorization required. It works with GovCloud regions (us-gov-west-1, us-gov-east-1) the same as commercial regions."
  },
];

function FAQ(){
  const [open, setOpen] = useState(null);
  return (
    <section id="faq" className="alt">
      <div className="wrap" style={{maxWidth:720,paddingTop:80,paddingBottom:80}}>
        <div style={{fontFamily:"'Space Grotesk',system-ui,sans-serif",fontWeight:500,fontSize:11,color:"var(--accent-dim)",letterSpacing:".08em",textTransform:"uppercase",display:"flex",alignItems:"center",gap:10,marginBottom:18}}>
          <span style={{width:24,height:1,background:"var(--accent-dim)",display:"inline-block"}}></span>
          FAQ
        </div>
        <h2 style={{marginBottom:48}}>Questions we actually get.</h2>
        <div style={{display:"flex",flexDirection:"column"}}>
          {FAQ_ITEMS.map((item, i) => {
            const isOpen = open === i;
            return (
              <div key={i} style={{
                borderBottom:"1px solid var(--line)",
              }}>
                <button
                  className="faq-q"
                  onClick={()=>setOpen(isOpen ? null : i)}
                  style={{
                    width:"100%",
                    display:"flex",
                    justifyContent:"space-between",
                    alignItems:"center",
                    padding:"22px 0",
                    background:"none",
                    border:"none",
                    color: isOpen ? "var(--fg)" : "var(--fg-2)",
                    fontFamily:"'Space Grotesk',system-ui,sans-serif",
                    fontSize:16,
                    fontWeight:500,
                    textAlign:"left",
                    cursor:"pointer",
                    gap:16,
                    transition:"color .15s",
                  }}
                  aria-expanded={isOpen}
                >
                  <span>{item.q}</span>
                  <span className="faq-plus" style={{
                    flexShrink:0,
                    width:22,
                    height:22,
                    borderRadius:"50%",
                    border:"1px solid var(--line-2)",
                    display:"flex",
                    alignItems:"center",
                    justifyContent:"center",
                    color:"var(--fg-3)",
                    fontSize:16,
                    transition:"transform .2s",
                    transform: isOpen ? "rotate(45deg)" : "none",
                  }}>+</span>
                </button>
                {isOpen && (
                  <p style={{
                    fontSize:15,
                    lineHeight:1.7,
                    color:"var(--fg-2)",
                    paddingBottom:20,
                    margin:0,
                  }}>{item.a}</p>
                )}
              </div>
            );
          })}
        </div>
        <div style={{marginTop:48,display:"flex",alignItems:"center",gap:12}}>
          <span style={{fontSize:14,color:"var(--fg-3)"}}>Still have questions?</span>
          <a href="mailto:hello@getnable.com?subject=nable%20question"
             target="_blank" rel="noopener noreferrer"
             style={{fontSize:14,color:"var(--accent)",textDecoration:"none",fontWeight:500}}>
            Email us directly →
          </a>
        </div>
      </div>
    </section>
  );
}

/* Tweaks panel */
const PALETTE_OPTIONS = [
  {value:"onyx",     label:"Onyx",     swatch:["#0a0a0c","#5fe8a0","#15151a"]},
  {value:"graphite", label:"Graphite", swatch:["#0d0f10","#4db8d4","#181c1f"]},
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
              fontFamily:"'Space Grotesk',system-ui,sans-serif",fontSize:12,cursor:"pointer",
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
  const [version, setVersion] = useState(null);
  useScrollTracking();
  useEffect(() => {
    applyPalette(t.palette);
    function onTweaks(e){ setT(e.detail); }
    window.addEventListener("nable:tweaks", onTweaks);
    return () => window.removeEventListener("nable:tweaks", onTweaks);
  }, []);
  useEffect(() => {
    fetch("/api/pypi-version")
      .then(r => r.ok ? r.json() : null)
      .then(d => { if(d?.version) setVersion(d.version); })
      .catch(() => {});
  }, []);

  return (
    <>
      <Nav />
      <Hero layout={t.layout} interaction={t.interaction} />
      <HowItWorks />
      <EveryEditor />
      <Depth />
      <AiCost />
      <Connectors />
      <Architecture version={version} />
      <Pricing />
      <MidCta />
      <FAQ />
      <FootCta />
      <Footer version={version} />
      <Tweaks />
    </>
  );
}

ReactDOM.createRoot(document.getElementById("app")).render(<App />);
