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
      <p style={{fontFamily:"'Instrument Sans',system-ui,sans-serif",fontSize:12,color:"var(--accent)",letterSpacing:".02em",
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
          color:"var(--alert)",fontFamily:"'Instrument Sans',system-ui,sans-serif"}}>
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
          <b>finops-mcp</b>
          <span>{version ? `v${version}` : "v0.8.36"} · runtime healthy</span>
        </span>
        <span className="sep">·</span>
        <span className="seg">{installs ? fmtNum(installs) : "4k+"} installs / mo via PyPI</span>
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
          <span>nable</span>
        </a>
        <ul>
          <li><button className="nav-link" onClick={()=>scrollTo('connectors')}>Connectors</button></li>
          <li><button className="nav-link" onClick={()=>scrollTo('pricing')}>Pricing</button></li>
          <li><button className="nav-link" onClick={()=>{ scrollTo('faq'); if(window.posthog) posthog.capture('nav_clicked',{item:'faq'}); }}>FAQ</button></li>
          <li><a href="/docs.html" onClick={()=>{ if(window.posthog) posthog.capture('docs_clicked',{location:'nav'}); }}>Docs</a></li>
          <li><a href="/about">About</a></li>
          <li><a href="https://github.com/chaandannn/finopsmcp" target="_blank" rel="noopener noreferrer" onClick={()=>{ if(window.posthog) posthog.capture('nav_clicked',{item:'github'}); }}>GitHub</a></li>
        </ul>
        <div className="right">
          <a href="/account.html" className="btn btn-ghost">Sign in</a>
          <button className="btn btn-primary"
             onClick={()=>{
               scrollTo('install');
               if(window.posthog) posthog.capture('cta_clicked',{location:'nav',cta:'start_free'});
             }}>
            Get started free <span className="arr">→</span>
          </button>
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
          <a className="nav-mobile-item" href="/about" onClick={()=>setOpen(false)}>About</a>
          <a className="nav-mobile-item" href="https://github.com/chaandannn/finopsmcp" target="_blank" rel="noopener noreferrer" onClick={()=>{ setOpen(false); if(window.posthog) posthog.capture('nav_clicked',{item:'github'}); }}>GitHub</a>
          <div style={{marginTop:24,display:"flex",flexDirection:"column",gap:10}}>
            <a href="/account.html" className="btn btn-ghost" style={{justifyContent:"center"}} onClick={()=>setOpen(false)}>Sign in</a>
            <button className="btn btn-primary" style={{justifyContent:"center"}}
              onClick={()=>{
                scrollTo('install');
                if(window.posthog) posthog.capture('cta_clicked',{location:'nav_mobile',cta:'start_free'});
              }}>
              Get started free <span className="arr">→</span>
            </button>
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
              Your cloud bill,<br/>
              <span className="strike">in a dashboard.</span>
              <span className="accent">Waste found.<br/>Money saved.</span>
            </h1>
            <p className="lede">
              Connect AWS, Azure, GCP, and 17 providers to Claude or Cursor. Ask about spend, get rightsizing recommendations, patch your Terraform, open the PR. Runs locally. Your credentials never leave your machine. We never see your data.
            </p>
            <div className="hero-cta-row" id="install">
              <CopyInstall />
              <a href="/docs.html" className="btn btn-ghost"
                onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'hero',cta:'docs'}); }}>
                Read the docs
              </a>
            </div>
            <div className="hero-mobile-cta">
              <p style={{fontSize:13, color:"var(--fg-3)", marginBottom:12, letterSpacing:".01em"}}>
                On mobile? Get the setup guide sent to your inbox.
              </p>
              <EmailCapture source="hero_mobile" placeholder="your@email.com" btnLabel="Send guide" />
            </div>
          </div>
          <div className="hero-right">
            <Console interaction={interaction} />
          </div>
        </div>
        <TrustStrip />
      </div>
    </header>
  );
}

function CopyInstall(){
  const [copied, setCopied] = useState(false);
  const cmd = "pip install finops-mcp && finops welcome";
  return (
    <div style={{display:"flex",flexDirection:"column",gap:8}}>
      <div className="install" role="group" aria-label="Install command">
        <span className="prompt">$</span>
        <span className="cmd">{cmd}</span>
        <button onClick={() => {
          navigator.clipboard?.writeText(cmd);
          setCopied(true);
          setTimeout(()=>setCopied(false),1600);
          if(window.posthog) posthog.capture('install_copied');
        }}>
          {copied ? "copied" : "copy"}
        </button>
      </div>
      <p className="mono" style={{fontSize:11,color:"var(--fg-3)",letterSpacing:".04em",paddingLeft:2}}>
        installs the MCP server · guided setup runs automatically
      </p>
    </div>
  );
}

function fmtNum(n){
  if(n >= 1000) return (n/1000).toFixed(1).replace(/\.0$/,"") + "k";
  return String(n);
}

function TrustStrip(){
  const [installs, setInstalls] = useState(null);

  useEffect(() => {
    fetch("/api/pypi-stats")
      .then(r => r.ok ? r.json() : null)
      .then(d => { if(d?.data?.last_month) setInstalls(d.data.last_month); })
      .catch(() => {});
  }, []);

  const items = [
    {lab:"installs / mo", val: installs ? fmtNum(installs) : "4k+", sub:"via PyPI · live"},
    {lab:"providers", val:"17", sub:"AWS · Azure · GCP +"},
    {lab:"sent to nable", val:"0 bytes", sub:"your data stays in your infrastructure"},
  ];
  return (
    <div className="trust" style={{gridTemplateColumns:"repeat(3,1fr)"}}>
      {items.map((t,i) => (
        <div className="ti" key={i}>
          <span className="lab">{t.lab}</span>
          <span className="val mono">{t.val}<span className="sub">{t.sub}</span></span>
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
      p: "Ask 'where am I wasting money?' and get a ranked list of every opportunity across your infrastructure — sorted by dollar impact, not category. No dashboard to configure. No report to schedule. No knowing what to look for. Just results.",
      chips: ["ranked by $","works day one","no setup","20 scanners"],
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
      p: "Bedrock, OpenAI, Anthropic — these don't fit in the usual cost buckets. nable tracks AI spend by model, by use case, by team. It spots where expensive models are doing work cheaper ones handle just as well, and flags environments burning AI budget unnecessarily.",
      chips: ["by model","by team","model routing","AI-native"],
    },
    {
      n: "04",
      h: "It tells you who to call.",
      p: "When spend spikes, you don't need another chart. You need to know which team owns it. nable attributes anomalies to the service, team, or environment that caused them, then alerts whoever owns it in Slack or Teams — before finance notices.",
      chips: ["team attribution","Slack / Teams","near-zero false positives","28-day baseline"],
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

/* Architecture */
function Architecture({ version }){
  return (
    <section id="arch" className="alt">
      <div className="wrap">
        <div className="section-head">
          <div className="label">Architecture</div>
          <h2>Headless by design.<br/><em>Your data never moves.</em></h2>
          <p>nable is not SaaS. It runs on the engineer's machine, holds credentials in the OS keyring, queries provider APIs directly, and surfaces tools to whichever AI editor is open. Your credentials never leave your machine. We never see your data.</p>
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
                <span className="sub">finops-mcp / {version || "0.8.36"}</span>
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
  {nm:"New Relic",  px:"Data ingest · user counts",      tag:"live"},
  {nm:"Linear",     px:"Seat plan · usage rollup",       tag:"live"},
  {nm:"OpenAI",     px:"Usage API · per-model spend",    tag:"live"},
  {nm:"Anthropic",  px:"Org usage · per-model spend",    tag:"live"},
  {nm:"Stripe",     px:"Billing meter · platform fees",  tag:"beta"},
  {nm:"PagerDuty",  px:"License spend · on-call costs",  tag:"beta"},
  {nm:"Coming soon",px:"Vote on the next connector",     tag:"soon"},
];

function Connectors(){
  return (
    <section id="connectors" className="alt">
      <div className="wrap">
        <div className="section-head">
          <div className="label">Connectors</div>
          <h2>17 sources.<br/><em>One conversation.</em></h2>
          <p>Every connector is a real API integration, not a CSV export. New providers ship monthly.</p>
        </div>
        <div className="conn-grid">
          {CONNECTORS.map((c,i) => (
            <div className="conn" key={i}>
              <span className="nm">{c.nm}</span>
              <span className="px">{c.px}</span>
              <span className={"tag " + (c.tag === "beta" ? "beta" : c.tag === "soon" ? "soon" : "")}>{c.tag}</span>
            </div>
          ))}
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
const SOLO_FEATURES = [
  "Cost queries across all providers",
  "Anomaly detection",
  "Rightsizing recommendations",
  "All 17 connectors",
  "Your credentials never leave your machine",
  "Works in Claude, Cursor, Windsurf, Zed",
];

const TEAM_FEATURES = [
  "Everything in Solo",
  "Terraform remediation: patch files, open PR",
  "Slack and Teams alerts — anomalies, budgets, weekly digest",
  "Publish cost reports to Notion for the whole team",
  "Ticket creation (Jira, Linear, GitHub Issues)",
  "Scheduled cost digests via email",
  "Budget enforcement and alerts",
  "Commitment analysis and RI recommendations",
  "No shared database required",
];

function CheckIcon(){
  return (
    <svg width="15" height="15" viewBox="0 0 15 15" fill="none" aria-hidden="true" style={{flexShrink:0,marginTop:1}}>
      <circle cx="7.5" cy="7.5" r="7" stroke="currentColor" strokeWidth="1"/>
      <path d="M4.5 7.5L6.5 9.5L10.5 5.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}

const ANNUAL_STRIPE_LINK = "https://buy.stripe.com/aFa28q1DzffEdZS2sy2Nq03";
const MONTHLY_STRIPE_LINK = "https://buy.stripe.com/3cIcN41Dz9Vk9JCd7c2Nq01";

function Pricing(){
  const [annual, setAnnual] = useState(false);

  const teamPrice    = annual ? "$400" : "$40";
  const teamPer      = annual ? "/ yr" : "/ mo";
  const teamSub      = annual ? "$33/mo · save $80" : null;
  const teamSavings  = annual ? "Save $80 — 2 months free" : "7-day free trial";
  const teamLink     = annual ? ANNUAL_STRIPE_LINK : MONTHLY_STRIPE_LINK;
  const teamPlan     = annual ? "team_annual" : "team_monthly";

  return (
    <section id="pricing">
      <div className="wrap">
        <div className="section-head">
          <div className="label">Pricing</div>
          <h2>Free to ask.<br/><em>Pay to remediate.</em></h2>
          <p>Solo is free forever. Team adds the remediation layer: Terraform PRs, digests, budget enforcement, and org rollups.</p>

          {/* Billing toggle */}
          <div style={{display:"flex",alignItems:"center",gap:12,justifyContent:"center",marginTop:24}}>
            <span style={{fontSize:13,color:annual?"var(--fg-3)":"var(--fg)",fontWeight:annual?400:500,transition:"color .15s"}}>Monthly</span>
            <button
              onClick={()=>setAnnual(a=>!a)}
              style={{
                width:44,height:24,borderRadius:12,border:"1px solid var(--line-2)",
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
        <div className="pricing-grid">

          {/* Solo */}
          <div className="pricing-card">
            <div className="pricing-top">
              <div className="pricing-name">Solo</div>
              <div className="pricing-price">
                <span className="pricing-amount">Free</span>
                <span className="pricing-per">forever</span>
              </div>
              <p className="pricing-desc">Everything you need to query, investigate, and understand your cloud costs.</p>
              <button className="btn btn-ghost pricing-cta"
                onClick={()=>{
                  document.getElementById('install')?.scrollIntoView({behavior:'smooth'});
                  if(window.posthog) posthog.capture('cta_clicked',{location:'pricing',plan:'solo'});
                }}>
                Get started free <span className="arr">→</span>
              </button>
            </div>
            <div className="pricing-features">
              {SOLO_FEATURES.map((f,i) => (
                <div key={i} className="pricing-feature">
                  <CheckIcon />
                  <span>{f}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Team */}
          <div className="pricing-card featured">
            <div className="pricing-badge">{teamSavings}</div>
            <div className="pricing-top">
              <div className="pricing-name">Team</div>
              <div className="pricing-price">
                <span className="pricing-amount">{teamPrice}</span>
                <span className="pricing-per">{teamPer}</span>
              </div>
              {teamSub && <p style={{fontSize:12,color:"var(--fg-3)",marginTop:4,letterSpacing:".01em"}}>{teamSub}</p>}
              <p className="pricing-desc">The remediation layer. Finds the waste, writes the fix, opens the PR, tracks whether it actually shipped.</p>
              <a
                href={teamLink}
                target="_blank"
                rel="noopener noreferrer"
                className="btn btn-primary pricing-cta"
                onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'pricing',plan:teamPlan,billing:annual?'annual':'monthly'}); }}>
                {annual ? "Get annual plan" : "Start free trial"} <span className="arr">→</span>
              </a>
            </div>
            <div className="pricing-features">
              {TEAM_FEATURES.map((f,i) => (
                <div key={i} className="pricing-feature">
                  <CheckIcon />
                  <span>{f}</span>
                </div>
              ))}
            </div>
          </div>

        </div>
        <p className="mono" style={{marginTop:32,fontSize:12,color:"var(--fg-4)",textAlign:"center",letterSpacing:".04em"}}>
          No credit card for Solo. Team trial requires a card, cancel any time.
        </p>
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
              Five minutes from install to your first real insight. Free forever for solo use.
            </p>
          </div>
          <div style={{display:"flex",alignItems:"center",gap:12,flexWrap:"wrap",justifyContent:"center"}}>
            <button className="btn btn-primary"
              onClick={()=>{
                document.getElementById('install')?.scrollIntoView({behavior:'smooth'});
                if(window.posthog) posthog.capture('cta_clicked',{location:'mid_cta',cta:'start_free'});
              }}>
              Get started free <span className="arr">→</span>
            </button>
            <a href="/docs.html" className="btn btn-ghost"
              onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'mid_cta',cta:'docs'}); }}>
              Read the docs
            </a>
          </div>
          <p className="mono" style={{fontSize:11,color:"var(--fg-4)",letterSpacing:".05em"}}>
            pip install finops-mcp &amp;&amp; finops welcome
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
            <button className="btn btn-primary" style={{padding:"14px 22px",fontSize:14}}
               onClick={()=>{
                 document.getElementById('install')?.scrollIntoView({behavior:'smooth'});
                 if(window.posthog) posthog.capture('cta_clicked',{location:'footer_cta',cta:'install'});
               }}>
              Get started free <span className="arr">→</span>
            </button>
            <a href="mailto:chandan@getnable.com?subject=nable - talk to founders"
               target="_blank" rel="noopener noreferrer"
               className="btn btn-ghost" style={{padding:"14px 22px",fontSize:14}}
               onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'footer_cta',cta:'talk_to_founders'}); }}>
              Talk to founders
            </a>
          </div>
          <EmailCapture source="footer" placeholder="drop your email, we'll send the setup guide" btnLabel="Send it" center={true} />
        </div>
        <p className="mono" style={{marginTop:32,fontSize:12,color:"var(--fg-3)",letterSpacing:".04em"}}>
          $ pip install finops-mcp &amp;&amp; finops welcome
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
        <div style={{fontFamily:"'Instrument Sans',system-ui,sans-serif",fontWeight:500,fontSize:11,color:"var(--accent-dim)",letterSpacing:".08em",textTransform:"uppercase",display:"flex",alignItems:"center",gap:10,marginBottom:24}}>
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
            <p style={{color:"var(--fg-3)",fontSize:13,maxWidth:"34ch",lineHeight:1.55,marginTop:10}}>Your cloud bill, in your editor. Made in Austin, TX.</p>
          </div>
          <div>
            <h5>Product</h5>
            <a href="#connectors">Connectors</a>
            <a href="#pricing">Pricing</a>
            <a href="#faq">FAQ</a>
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
            <a href="/about">About</a>
            <a href="/about#investors">Investors</a>
            <a href="mailto:hello@getnable.com" target="_blank" rel="noopener noreferrer">Contact</a>
            <a href="https://github.com/chaandannn/finopsmcp" target="_blank" rel="noopener noreferrer">GitHub</a>
          </div>
        </div>
        <div className="foot-meta">
          <span>2026 nable, inc. · all rights reserved</span>
          <span>finops-mcp / {version || "0.8.36"} · runtime healthy</span>
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
    a: "Your credentials are stored in your OS keyring (macOS Keychain, Windows Credential Manager, or libsecret on Linux) and never leave your machine. Cost data stays in a local SQLite database on your machine. We never see your data. For teams, findings are shared via Slack alerts and Notion — no shared database required."
  },
  {
    q: "What editors does it work with?",
    a: "Claude Desktop, Cursor, Windsurf, Zed, and anything that supports MCP. The setup wizard configures your editor automatically. If you use multiple editors, run the wizard once per editor."
  },
  {
    q: "How long does setup take?",
    a: "About 5 minutes. Run `pip install finops-mcp && finops welcome`, follow the prompts, and you're done. The wizard handles the MCP config and credential storage."
  },
  {
    q: "Is the free tier actually free?",
    a: "Yes. No credit card, no expiry. The free tier includes cost queries, anomaly detection, rightsizing recommendations, and all 17 connectors. Team adds automated ticket creation, scheduled digests, and commitment analysis."
  },
  {
    q: "I only have one AWS account. Is this worth it?",
    a: "Yes. Rightsizing and anomaly detection alone are usually worth it. Most people find savings in the first session. You can add more providers later."
  },
  {
    q: "Do you support multiple AWS accounts or organizations?",
    a: "Yes. Run `finops setup aws --add` to connect additional accounts. You can query across all of them in a single conversation. Multi-account org rollups are on the roadmap for Q3."
  },
];

function FAQ(){
  const [open, setOpen] = useState(null);
  return (
    <section id="faq" className="alt" style={{borderTop:"1px solid var(--line)"}}>
      <div className="wrap" style={{maxWidth:720,paddingTop:80,paddingBottom:80}}>
        <div style={{fontFamily:"'Instrument Sans',system-ui,sans-serif",fontWeight:500,fontSize:11,color:"var(--accent-dim)",letterSpacing:".08em",textTransform:"uppercase",display:"flex",alignItems:"center",gap:10,marginBottom:18}}>
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
                  onClick={()=>setOpen(isOpen ? null : i)}
                  style={{
                    width:"100%",
                    display:"flex",
                    justifyContent:"space-between",
                    alignItems:"center",
                    padding:"20px 0",
                    background:"none",
                    border:"none",
                    color:"var(--fg)",
                    fontFamily:"'Instrument Sans',system-ui,sans-serif",
                    fontSize:16,
                    fontWeight:500,
                    textAlign:"left",
                    cursor:"pointer",
                    gap:16,
                  }}
                  aria-expanded={isOpen}
                >
                  <span>{item.q}</span>
                  <span style={{
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
      <Connectors />
      <Depth />
      <Architecture version={version} />
      <Pricing />
      <FAQ />
      <FootCta />
      <Footer version={version} />
      <Tweaks />
    </>
  );
}

ReactDOM.createRoot(document.getElementById("app")).render(<App />);
