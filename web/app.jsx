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
    "--bg":"#000000","--bg-1":"#0a0a0c","--bg-2":"#121214","--bg-3":"#1a1a1d",
    "--line":"#232327","--line-2":"#2d2d32",
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
    // ids must match the real <section id>s; 'architecture' and 'foot-cta' never
    // existed, so scroll tracking was silently blind past the connectors band.
    // 'start' (the GetStarted section) was folded away in the density pass.
    const sections = ['demo', 'loop', 'ai', 'arch', 'connectors', 'pricing', 'faq', 'cta'];
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

function LogoMark(){
  // V2 mark (2026-07-10): the N's diagonal is a falling cost line, the node is
  // where the saving lands. Canonical shapes live in web/favicon.svg.
  return (
    <svg width="26" height="26" viewBox="0 0 120 120" className="mark-img" aria-hidden="true">
      <rect x="2" y="2" width="116" height="116" rx="25" fill="#0a0a0c" stroke="#2c7d91" strokeOpacity=".55" strokeWidth="3"/>
      <path d="M36 92 V30" stroke="#4db8d4" strokeWidth="14" strokeLinecap="round"/>
      <path d="M36 32 L84 86" stroke="#4db8d4" strokeWidth="14" strokeLinecap="round"/>
      <path d="M84 90 V28" stroke="#2c7d91" strokeWidth="14" strokeLinecap="round"/>
      <circle cx="84" cy="88" r="9" fill="#4db8d4"/>
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
        <span className="seg">70k+ PyPI downloads</span>
        <span className="sep">·</span>
        <span className="seg">AWS · Azure · GCP · SaaS · AI spend, one bill</span>
        <span className="sep">·</span>
        <span className="seg">
          <a href="/about" style={{color:"var(--accent)",textDecoration:"none",fontWeight:500}}>
            About →
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
    <>
    <nav className="nav">
      <div className="nav-inner">
        <a href="/" className="logo">
          <LogoMark />
          <span><span style={{color:'var(--accent)'}}>n</span>able</span>
        </a>
        <ul>
          <li><a href="/agents" onClick={()=>{ if(window.posthog) posthog.capture('nav_clicked',{item:'agents'}); }}>Agents</a></li>
          <li><a href="/pricing" onClick={()=>{ if(window.posthog) posthog.capture('nav_clicked',{item:'pricing'}); }}>Pricing</a></li>
          <li><a href="/docs" onClick={()=>{ if(window.posthog) posthog.capture('docs_clicked',{location:'nav'}); }}>Docs</a></li>
        </ul>
        <div className="right">
          <a href="https://github.com/chaandannn/finopsmcp" target="_blank" rel="noopener noreferrer" className="nav-star"
             onClick={()=>{ if(window.posthog) posthog.capture('github_star_clicked',{location:'nav'}); }}
             aria-label="Star nable on GitHub">
            <svg viewBox="0 0 16 16" width="15" height="15" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/></svg>
            <span>Star</span>
            <span className="nav-star-glyph">★</span>
          </a>
          <a href="/account" className="nav-signin">Sign in</a>
          <a href="/demo" className="btn btn-primary"
             onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'nav',cta:'try_demo'}); }}>
            Try it free <span className="arr">→</span>
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
    </nav>
      {open && (
        <div className="nav-mobile-menu">
          <a className="nav-mobile-item" href="/agents" onClick={()=>{ setOpen(false); if(window.posthog) posthog.capture('nav_clicked',{item:'agents'}); }}>Agents</a>
          <a className="nav-mobile-item" href="/pricing" onClick={()=>{ setOpen(false); if(window.posthog) posthog.capture('nav_clicked',{item:'pricing'}); }}>Pricing</a>
          <a className="nav-mobile-item" href="/docs" onClick={()=>{ setOpen(false); if(window.posthog) posthog.capture('docs_clicked',{location:'nav_mobile'}); }}>Docs</a>
          <a className="nav-mobile-item" href="https://github.com/chaandannn/finopsmcp" target="_blank" rel="noopener noreferrer" onClick={()=>{ setOpen(false); if(window.posthog) posthog.capture('github_star_clicked',{location:'nav_mobile'}); }}>Star on GitHub <span style={{color:'var(--warn)'}}>★</span></a>
          <div style={{marginTop:24,display:"flex",flexDirection:"column",gap:10}}>
            <a href="/account" className="btn btn-ghost" style={{justifyContent:"center"}} onClick={()=>setOpen(false)}>Sign in</a>
            <a href="/demo" className="btn btn-primary" style={{justifyContent:"center"}}
              onClick={()=>{ setOpen(false); if(window.posthog) posthog.capture('cta_clicked',{location:'nav_mobile',cta:'try_demo'}); }}>
              Try it free <span className="arr">→</span>
            </a>
          </div>
        </div>
      )}
    </>
  );
}

/* Hero */
// The hero is always the split layout: copy left, live console right (per
// DESIGN.md). The centered "editorial" variant was retired, so layout is
// accepted for compatibility but no longer switches the arrangement.
function Hero(){
  return (
    <header className="hero hero-centered" id="top">
      <div className="hero-grid" aria-hidden="true"></div>
      <div className="wrap">
        <div className="hero-c">
          <h1 className="display">
            The cost brain <span className="h1-ask">for the AI era.</span>
          </h1>
          <p className="hero-sub">Cloud and AI cost management, where you code. Find the waste in Claude or Cursor, ship the fix as a PR, all on your machine.</p>
          <div className="hero-actions">
            <CopyCmd cmd="uvx nable" />
          </div>
          <p className="hero-cmdnote">Read-only · no signup · no cloud keys · free for solo use</p>
        </div>
      </div>
    </header>
  );
}

function CopyCmd({ cmd }){
  const [copied, setCopied] = useState(false);
  return (
    <button className={"copycmd" + (copied ? " copied" : "")} onClick={() => {
      navigator.clipboard?.writeText(cmd);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
      if(window.posthog) posthog.capture('install_copied');
    }}>
      <span className="prompt">$</span>
      <span className="cmd">{cmd}</span>
      <span className="copylab">{copied ? "✓ copied" : "copy"}</span>
    </button>
  );
}

function fmtNum(n){
  if(n >= 1000) return (n/1000).toFixed(1).replace(/\.0$/,"") + "k";
  return String(n);
}

/* Console (interactive demo terminal) */
const QUERIES = [
  {
    q: "How much are we spending on databases?",
    response: (
      <>
        <p>Pulled every managed database across your clouds and normalized to USD. This month so far:</p>
        <div className="ttable">
          <div className="r hd"><span>Provider · service</span><span>Spend</span><span>delta MoM</span></div>
          <div className="r"><span>AWS · RDS + Aurora</span><span className="v num">$9,240</span><span className="d up num">+11.4%</span></div>
          <div className="r"><span>GCP · Cloud SQL</span><span className="v num">$3,180</span><span className="d up num">+6.2%</span></div>
          <div className="r"><span>MongoDB · Atlas</span><span className="v num">$2,460</span><span className="d down num">-2.1%</span></div>
          <div className="r"><span>Snowflake · compute</span><span className="v num">$1,910</span><span className="d up num">+18.7%</span></div>
          <div className="r total"><span>Total databases</span><span className="v num">$16,790</span><span className="d up num">+9.8%</span></div>
        </div>
        <p style={{marginTop:12}}>Two Aurora instances sit below 20% utilization. Rightsizing them saves about <span style={{color:"var(--accent)"}}>$640 / mo</span>. Want the breakdown?</p>
      </>
    )
  },
  {
    q: "What's our compute cost across AWS and GCP?",
    response: (
      <>
        <p>Normalized to USD and pulled from each provider's billing API just now. This month:</p>
        <div className="ttable">
          <div className="r hd"><span>Provider · service</span><span>Spend</span><span>delta MoM</span></div>
          <div className="r"><span>AWS · EC2 + Fargate</span><span className="v num">$18,420</span><span className="d up num">+18.6%</span></div>
          <div className="r"><span>GCP · Compute Engine</span><span className="v num">$4,870</span><span className="d down num">-3.4%</span></div>
          <div className="r"><span>GCP · Cloud Run</span><span className="v num">$1,240</span><span className="d up num">+7.1%</span></div>
          <div className="r total"><span>Total compute</span><span className="v num">$24,530</span><span className="d up num">+12.9%</span></div>
        </div>
        <p style={{marginTop:14}}>Most of the AWS jump is three new instances in <span className="mono" style={{color:"var(--fg)"}}>us-east-1</span>, about $1,890. Want me to tag them and open an audit ticket?</p>
      </>
    )
  },
  {
    q: "Where is our AI spend going this month?",
    response: (
      <>
        <p>Token spend across your model providers this month, normalized to USD:</p>
        <div className="ttable">
          <div className="r hd"><span>Provider · model</span><span>Spend</span><span>delta MoM</span></div>
          <div className="r"><span>OpenAI · gpt-4o</span><span className="v num">$4,120</span><span className="d up num">+34%</span></div>
          <div className="r"><span>Anthropic · Claude</span><span className="v num">$2,880</span><span className="d up num">+21%</span></div>
          <div className="r"><span>AWS · Bedrock</span><span className="v num">$1,610</span><span className="d up num">+12%</span></div>
          <div className="r total"><span>Total AI / LLM</span><span className="v num">$8,610</span><span className="d up num">+26%</span></div>
        </div>
        <p style={{marginTop:12}}>Your token bill is up <span style={{color:"var(--alert)"}}>26%</span> even as per-token prices fell. A gpt-4o classifier is the driver; route it to a cheaper model to save about <span style={{color:"var(--accent)"}}>$1,400 / mo</span>.</p>
      </>
    )
  },
  {
    q: "Which provider grew fastest this month?",
    response: (
      <>
        <p>Ranked every connected provider by month-over-month growth, normalized to USD:</p>
        <div className="ttable">
          <div className="r hd"><span>Provider</span><span>Spend</span><span>delta MoM</span></div>
          <div className="r"><span>OpenAI</span><span className="v num">$4,120</span><span className="d up num">+34%</span></div>
          <div className="r"><span>Snowflake</span><span className="v num">$1,910</span><span className="d up num">+18.7%</span></div>
          <div className="r"><span>AWS</span><span className="v num">$28,400</span><span className="d up num">+12.4%</span></div>
          <div className="r"><span>GCP</span><span className="v num">$9,300</span><span className="d up num">+3.1%</span></div>
          <div className="r total"><span>Fastest grower</span><span className="v num">OpenAI</span><span className="d up num">+34%</span></div>
        </div>
        <p style={{marginTop:12}}>OpenAI grew fastest in percent, but AWS added the most dollars: <span style={{color:"var(--alert)"}}>+$3,130</span>. Want either one traced to the team that caused it?</p>
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
        <p style={{marginTop:12}}>Raising Compute SP coverage to 92% is worth another <span style={{color:"var(--accent)"}}>$8,200 / mo</span>. Model it?</p>
      </>
    )
  },
  {
    q: "What can you actually do?",
    response: (
      <>
        <p>A lot, and all of it from your editor. On a connected account I can:</p>
        <ul className="caps">
          <li><b>Answer cost questions</b> across AWS, Azure, GCP, Kubernetes and 15 SaaS and AI providers</li>
          <li><b>Catch anomalies</b> with Z-score detection and name the tag driving the spike</li>
          <li><b>Find savings</b>: rightsizing, idle cleanup, commitment and discount coverage</li>
          <li><b>Track AI spend</b> by model and forecast where your token bill lands</li>
          <li><b>Act</b>: open a rightsizing PR against your IaC, file a ticket, post to Slack</li>
        </ul>
        <p style={{marginTop:12}}><span style={{color:"var(--accent)"}}>180+ tools</span> in all. Pick a prompt below to run a real one.</p>
      </>
    )
  },
];

// Match a typed question to one of the canned answers above. Tight on purpose:
// only answer when the topic is clearly one of these, otherwise return -1 and
// hit the GATE, the "ask that on your real bill" moment. The fuller /demo.html
// experience is for visitors who want to go deeper.
function matchQuery(text){
  const t = (text || "").toLowerCase();
  // Capabilities ("what can you do").
  if(/what can|capabilit|what do you do|what does nable|use case|everything you|all the tools|how does (this|it) work|what should i ask/.test(t)) return 6;
  // AI / LLM spend.
  if(/\bai\b|llm|token|openai|anthropic|claude|bedrock|\bgpt|inference|model (spend|cost|bill)/.test(t)) return 2;
  // Commitment / discount coverage.
  if(/discount|savings ?plan|reserved|reservation|\bri\b|commitment|coverage|effective (rate|discount)|\bcud/.test(t)) return 5;
  // Anomalies / spikes.
  if(/anomal|spike|spiking|surge|unusual|datadog|went up|going up|jump/.test(t)) return 4;
  // Which provider grew fastest / biggest mover.
  if(/grew fastest|grow(ing)? fastest|fastest grow|biggest (mover|grow|increase|jump)|which provider|who grew|ranked? by growth/.test(t)) return 3;
  // Databases across providers.
  if(/database|\brds\b|aurora|cloud ?sql|postgres|mysql|mongo|snowflake|warehouse|\bdb\b/.test(t)) return 0;
  // Cross-provider compute comparison.
  if(/across (all )?(provider|cloud)|all providers|multi-?cloud|aws.*(vs|versus|and).*(azure|gcp)|gcp.*(vs|versus|and).*aws|month.?over.?month|\bmom\b|compute.*(across|provider|month|vs|versus|cost)|ec2|fargate|compute engine/.test(t)) return 1;
  // Generic waste / rightsizing / concrete savings -> the databases breakdown leads with a rightsizing hook.
  if(/wast|idle|rightsiz|right-?siz|over-?provision|low cpu|cut (cost|spend)|save money|saving money|where can i save|trim/.test(t)) return 0;
  return -1;
}

// A question we don't have a canned answer for is the highest-intent moment.
// Instead of faking an answer (or paying a model), gate straight to the real
// product, framed as "ask that on your own bill."
const GATE = (
  <>
    <p>That's exactly the kind of question nable answers against your <b>own</b> account, with your real numbers. This demo only knows the sample account above.</p>
    <p style={{marginTop:12}}>Connect it in about a minute, then ask away on your real bill. It runs on your machine, nothing leaves it:</p>
    <div className="gate-cmd"><CopyCmd cmd="uvx nable" /></div>
    <p className="gate-sub">Free for solo use, no signup. Runs on your machine.</p>
  </>
);

// Off-topic / nonsense ("bob cat?") is not a finops question, so don't earnestly
// claim "that's what nable answers." Redirect to the demo's actual scope.
const OFFTOPIC = (
  <>
    <p>That one's outside this demo. nable here only covers cloud and AI cost, so ask about your AWS, Azure, GCP, Kubernetes or AI spend, or try a prompt below.</p>
  </>
);

// Does the question even look like a cloud/AI cost question? Separates a real
// but uncanned finops question (which gets the install gate) from off-topic noise.
const FINANCE_RE = /cost|spend|bill|budget|forecast|sav(e|ing)|money|cheap|expensive|pric(e|ing)|discount|invoice|usage|waste|idle|optimi[sz]e|rightsiz|reserved|reservation|commitment|anomal|cloud|aws|azure|gcp|ec2|\bs3\b|rds|lambda|fargate|eks|kubernetes|k8s|container|cluster|instance|\bvm\b|server|database|storage|snowflake|databricks|datadog|gpu|\bai\b|llm|token|openai|anthropic|claude|bedrock|gpt|\bmodel\b|provider|region|account|\btag|dollar|\$/;

const CHIPS = [
  { label: "What can you do?", idx: 6 },
  { label: "Spend on databases?", idx: 0 },
  { label: "Compute across AWS and GCP?", idx: 1 },
  { label: "Where's our AI spend going?", idx: 2 },
  { label: "Which provider grew fastest?", idx: 3 },
];

function Console({ interaction }){
  // Render the first answer immediately on mount, so the proof is on screen
  // before any animation runs. The console auto-cycles the canned library as a
  // live walkthrough until the visitor types or focuses the input; from then on
  // it answers what they ask.
  const [phase, setPhase] = useState("answered");      // typing | thinking | answered
  const [typed, setTyped] = useState(QUERIES[0].q);
  const [answer, setAnswer] = useState(QUERIES[0].response);
  const [asked, setAsked] = useState(false);
  const [focused, setFocused] = useState(false);
  const [input, setInput] = useState("");
  const [cycleIdx, setCycleIdx] = useState(0);
  const [isGate, setIsGate] = useState(false);
  const [offTopic, setOffTopic] = useState(false);
  const timers = useRef([]);

  function clearTimers(){ timers.current.forEach(clearTimeout); timers.current = []; }

  function runExchange(qText, ansJSX){
    clearTimers();
    setTyped(""); setAnswer(ansJSX); setPhase("typing");
    let i = 0;
    (function step(){
      if(i <= qText.length){
        setTyped(qText.slice(0,i)); i++;
        timers.current.push(setTimeout(step, 16 + Math.random()*20));
      } else {
        timers.current.push(setTimeout(() => setPhase("thinking"), 280));
        timers.current.push(setTimeout(() => setPhase("answered"), 1000));
      }
    })();
  }

  // Auto-cycle the canned proof until the visitor engages. The console mounts on
  // the first answer (phase "answered"); this schedules the next question, and
  // each answered phase re-arms it, so the hero keeps rotating through the
  // library. It pauses the moment the visitor types or focuses the input.
  useEffect(() => {
    if(interaction !== "cycling" || asked || focused) return;
    if(phase !== "answered") return;
    const t = setTimeout(() => {
      const next = (cycleIdx + 1) % QUERIES.length;
      setCycleIdx(next);
      runExchange(QUERIES[next].q, QUERIES[next].response);
    }, 6500);
    return () => clearTimeout(t);
  }, [phase, interaction, asked, focused, cycleIdx]);

  useEffect(() => () => clearTimers(), []);

  function ask(text){
    const q = (text || "").trim();
    if(!q) return;
    setAsked(true); setInput("");
    const m = matchQuery(q);
    let kind;
    if(m >= 0){ setIsGate(false); setOffTopic(false); runExchange(q, QUERIES[m].response); kind = "answer"; }
    else if(FINANCE_RE.test(q.toLowerCase())){ setIsGate(true); setOffTopic(false); runExchange(q, GATE); kind = "gate"; }
    else { setIsGate(false); setOffTopic(true); runExchange(q, OFFTOPIC); kind = "offtopic"; }
    if(window.posthog) posthog.capture('hero_demo_ask', { kind });
  }
  function pickChip(c){
    setAsked(true); setInput(""); setIsGate(false); setOffTopic(false);
    runExchange(QUERIES[c.idx].q, QUERIES[c.idx].response);
    if(window.posthog) posthog.capture('hero_demo_chip', { idx: c.idx });
  }

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
            <div className="bubble">{answer}</div>
          </div>
        )}
      </div>
    </div>
  );
}

/* See it work (console demo, relocated out of the hero) */
function SeeItWork({ interaction }){
  return (
    <section id="demo" className="demo-sec">
      <div className="wrap">
        <div className="section-head center">
          <div className="label">See it work</div>
          <h2>Ask nable like you'd<br/><em>ask a teammate.</em></h2>
          <p>nable pulls every connected provider, normalizes to USD, and answers right in your editor. Watch it run through real questions, or ask your own.</p>
        </div>
        <div className="console-stage">
          <Console interaction={interaction} />
        </div>
      </div>
    </section>
  );
}

/* AI cost */
function AiCost(){
  const copy = () => {
    if(navigator.clipboard) navigator.clipboard.writeText("uvx nable");
    if(window.posthog) posthog.capture('cta_clicked',{location:'ai_cost',cta:'copy_install'});
  };
  return (
    <section id="ai" className="alt" style={{borderTop:"1px solid var(--line)"}}>
      <div className="wrap">
        <div className="section-head center">
          <div className="label">Your AI bill</div>
          <h2>Tools chart your AI spend.<br/><em>nable finds the waste.</em></h2>
          <p>Most of an AI bill is input tokens billed at full price, plus calls sent to a frontier model a cheaper one would have answered the same way. nable reads the split from your real usage and shows you the cheapest way to get the same output. No caching guesswork.</p>
        </div>
        <ul className="ee-points ee-points-center">
          <li><span className="ee-plus">+</span><span>Input, output and cache, <b>split from your actual bill</b></span></li>
          <li><span className="ee-plus">+</span><span>Flags <b>frontier-model calls</b> a cheaper model handles the same</span></li>
          <li><span className="ee-plus">+</span><span>Separates <b>what you can bank today</b> from what needs a closer look</span></li>
        </ul>
        <div className="aicost-cta aicost-panel-center" style={{justifyContent:"center"}}>
          <span className="aicost-cta-l">See your own AI bill, split by model, free:</span>
          <code className="aicost-cmd" onClick={copy}>uvx nable</code>
        </div>
      </div>
    </section>
  );
}

/* Architecture */
function Architecture({ version }){
  const [tab, setTab] = useState("flow");
  return (
    <section id="arch" className="alt">
      <div className="wrap">
        <div className="section-head center">
          <div className="label">Architecture</div>
          <h2>How it works,<br/><em>and where your data lives.</em></h2>
          <p>One runtime, local or hosted. Your editor talks to it over MCP; it reads your providers read-only, and never changes anything on its own.</p>
        </div>

        <div className="arch-tabs" role="tablist">
          <button role="tab" aria-selected={tab==="flow"} className={"arch-tab" + (tab==="flow"?" active":"")} onClick={()=>setTab("flow")}>The flow</button>
          <button role="tab" aria-selected={tab==="data"} className={"arch-tab" + (tab==="data"?" active":"")} onClick={()=>setTab("data")}>Your data</button>
          <button role="tab" aria-selected={tab==="run"} className={"arch-tab" + (tab==="run"?" active":"")} onClick={()=>setTab("run")}>Run or host</button>
        </div>

        <div className="arch-panel">
          {tab==="data" && (
        <div className="guarantees">
          <div className="guarantees-head">
            <h3>Your data, in plain terms</h3>
            <a className="guarantees-link" href="/security">Full security model <span className="arr">→</span></a>
          </div>
          <div className="guarantees-cols">
            <div className="guarantee-col">
              <span className="guarantee-tag">Local · Dev and Pro</span>
              <ul>
                <li><CheckIcon /><span>Your credentials and cost data never reach our servers</span></li>
                <li><CheckIcon /><span>We never sell, rent, or share your data</span></li>
                <li><CheckIcon /><span>Nothing you connect is used to train models</span></li>
                <li><CheckIcon /><span>You own your inputs and outputs</span></li>
                <li><CheckIcon /><span>Propose-only: nable never changes your cloud on its own</span></li>
              </ul>
            </div>
            <div className="guarantee-col">
              <span className="guarantee-tag">Hosted · Startups and Enterprise</span>
              <ul>
                <li><CheckIcon /><span>Single-tenant: your data is never pooled with another customer's</span></li>
                <li><CheckIcon /><span>Your own isolated runtime and store</span></li>
                <li><CheckIcon /><span>Sub-processors: Anthropic for managed AI, the host, and Stripe</span></li>
                <li><CheckIcon /><span>SSO, RBAC, and audit logs</span></li>
              </ul>
            </div>
          </div>
        </div>
          )}
          {tab==="flow" && (
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
              <span className="lab">runtime · local or hosted</span>
              <div className="arch-node center">
                <h4>nable runtime</h4>
                <span className="sub">your machine or a single-tenant host</span>
                <div className="chips"><span>keyring</span><span>fernet</span><span>read-only</span><span>audit-log</span></div>
              </div>
            </div>
            <div className="arch-arrow"><span>https</span><span className="line"></span><span>signed</span></div>
            <div className="arch-col">
              <span className="lab">provider apis</span>
              <div className="arch-node">
                <h4>Every provider</h4>
                <span className="sub">cost · usage · billing</span>
                <div className="chips"><span>AWS CE/CUR</span><span>Azure CM</span><span>GCP BQ</span><span>+14</span></div>
              </div>
            </div>
          </div>
        </div>
          )}
          {tab==="run" && (
        <div className="host-opts">
          <div className="host-opt">
            <span className="host-tag">Run it yourself</span>
            <h4>Local-first, on your machine</h4>
            <p>Install with one command. Credentials, cache, and queries all stay on your machine, no nable backend in the path.</p>
            <div className="gate-cmd"><CopyCmd cmd="uvx nable" /></div>
            <a className="host-alt" href="https://github.com/chaandannn/finopsmcp#run-it-on-a-server-docker" target="_blank" rel="noopener noreferrer"
               onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'architecture',cta:'selfhost_docker'}); }}>
              Prefer a server? Self-host the dashboard with Docker <span className="arr">→</span>
            </a>
          </div>
          <div className="host-opt">
            <span className="host-tag">Or let us host it</span>
            <h4>Managed, single-tenant</h4>
            <p>Want it always on? We run a single-tenant instance for your org, isolated from every other customer, plus SSO, RBAC, and share links.</p>
            <a className="btn btn-ghost host-cta" href={BOOK_CALL_LINK} target="_blank" rel="noopener noreferrer"
               onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'architecture',cta:'hosted_demo'}); }}>
              Talk to us about hosting <span className="arr">→</span>
            </a>
          </div>
        </div>
          )}
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
        <div className="section-head center">
          <div className="label">One dataset</div>
          <h2>Every provider,<br/><em>one normalized bill.</em></h2>
          <p>Every provider lands in the same FOCUS 1.2 records, the open FinOps standard, which nable extends past the clouds to usage-based SaaS and per-model AI spend. That is why one question can span AWS, Snowflake, Datadog and your OpenAI tokens: they all answer in the same shape.</p>
          <div className="focus-chips" aria-hidden="true">
            <span>FOCUS 1.2</span><span>AWS · Azure · GCP</span><span>11 SaaS providers</span><span>AI spend, per model</span><span>tokens preserved</span>
          </div>
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
        <p className="logo-band-note">Clouds, SaaS and AI spend in one normalized bill · new providers ship monthly · <a className="logo-band-link" href="/docs#env-vars" onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'connectors',cta:'provider_setup'}); }}>setup guide for every provider &rarr;</a></p>
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

// Stripe checkout links for the previous Pro/Startups tiers. NOT surfaced on the
// site during the interim Community/Enterprise pricing (2026-07-10); kept because
// the in-product upgrade path still uses them and reverting is one edit.
const PRO_MONTHLY_LINK     = "https://buy.stripe.com/5kQeVc4PL9Vk4piaZ42Nq0a";
const PRO_ANNUAL_LINK      = "https://buy.stripe.com/eVqaEW961aZocVO8QW2Nq0b";
const STARTUP_MONTHLY_LINK = "https://buy.stripe.com/3cI3cucid6J85tm3wC2Nq08";
const STARTUP_ANNUAL_LINK  = "https://buy.stripe.com/14A6oG0zvgjI9JCffk2Nq09";

const BOOK_CALL_LINK = "https://calendar.app.google/2duYBqjLXaTmX5xC8";

// Comparison rows. value true -> check, false -> dash, string -> mono text.
const PRICE_ROWS = [
  { label: "Users",                                         dev: "Just you",     pro: "Your team",    startup: "Your org",              ent: "Your org" },
  { label: "Cost queries, anomalies, rightsizing, every provider", dev: true,   pro: true,           startup: true,                    ent: true },
  { label: "Remediation PRs, alerts, dashboards, Slack bot", dev: false,         pro: true,           startup: true,                    ent: true },
  { label: "Runs",                                          dev: "Your machine", pro: "Your machine", startup: "Your machine",          ent: "Hosted or self-host" },
  { label: "Managed AI",                                    dev: "Your own key", pro: "Your own key", startup: "Your own key",          ent: "Custom" },
  { label: "Hosting + managed AI (add-on)",                 dev: false,          pro: "Contact us", startup: "Contact us", ent: "Custom" },
  { label: "SSO + audit logs",                              dev: false,          pro: false,          startup: false,                   ent: true },
  { label: "Support",                                       dev: "Slack",        pro: "Slack",        startup: "Priority Slack",        ent: "Slack + SLA" },
];

function PCell({ v }){
  if (v === true)  return <span className="pcheck"><CheckIcon /></span>;
  if (v === false) return <span className="pdash">–</span>;
  return <span className="pval">{v}</span>;
}

// Pricing cards, shown on every viewport. Each tier carries a readable name, a
// benefit tagline, a big mono price, a divider, four short feature lines, and one
// CTA pinned to the bottom so the buttons align across cards.
function PricingCards({ tiers, annual }){
  return (
    <div className="pcards">
      {tiers.map(t => (
        <div className={"pcard" + (t.rec ? " pcard-rec" : "")} key={t.key}>
          {t.rec && <div className="pcard-badge">Recommended</div>}
          <div className="pcard-name">{t.name}</div>
          <div className="pcard-tag">{t.tag}</div>
          <div className="pcard-price">
            <span className="pcard-amt">{t.amt}</span>
            {t.per && <span className="pcard-per">{t.per}</span>}
          </div>
          <div className="pcard-billed">{t.billed}</div>
          <ul className="pcard-feats">
            {t.feats.map((f,i) => (<li key={i}><CheckIcon /><span>{f}</span></li>))}
          </ul>
          <a className={"btn " + (t.primary ? "btn-primary" : "btn-ghost") + " pcard-cta"}
             href={t.href} {...(t.ext ? {target:"_blank", rel:"noopener noreferrer"} : {})}
             onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'pricing',plan:t.plan,billing:annual?'annual':'monthly'}); }}>
            {t.cta}</a>
        </div>
      ))}
    </div>
  );
}

function Pricing(){
  // Interim two-tier pricing (2026-07-10) while the outcome-based model is
  // finalized: Community = the whole local product, free. Enterprise = managed
  // hosting, always-on, SSO, contact us. The old Pro/Startups Stripe links stay
  // live in-product; they are just not sold on the site right now.
  const tiers = [
    { key:"community", name:"Community", tag:"For engineers and their bill", amt:"Free", per:"", billed:"No credit card, no expiry",
      feats:[
        "The full local product: cost queries, anomalies, rightsizing, every provider",
        "The agent team: Budget Guard, the fix as a PR you approve, verified savings",
        "AI/LLM spend tracking, forecasts + commitment recommendations",
        "Self-host the dashboard with Docker",
        "Runs on your machine, on your own Claude membership",
      ],
      cta:"Start free", href:"/demo", plan:"community", ext:false, primary:true, rec:false },
    { key:"ent", name:"Enterprise", tag:"For teams that need it always on", amt:"Custom", per:"", billed:"Tailored to your team",
      feats:[
        "Everything in Community",
        "Managed single-tenant hosting: always-on monitoring + push alerts",
        "Dashboards + Slack for the whole team, no terminals",
        "SSO, RBAC + audit logs",
        "Your data never pooled with another customer's",
        "Priority support + custom SLA",
      ],
      cta:"Contact us", href:BOOK_CALL_LINK, plan:"enterprise", ext:true, primary:false, rec:false },
  ];

  return (
    <section id="pricing">
      <div className="wrap">
        <div className="section-head center">
          <div className="label">Pricing</div>
          <h2>Free to run yourself.<br/><em>Enterprise when it runs for you.</em></h2>
        </div>

        <div className="pcards pcards-2">
          {tiers.map(t => (
            <div className={"pcard" + (t.rec ? " pcard-rec" : "")} key={t.key}>
              <div className="pcard-name">{t.name}</div>
              <div className="pcard-tag">{t.tag}</div>
              <div className="pcard-price">
                <span className="pcard-amt">{t.amt}</span>
                {t.per && <span className="pcard-per">{t.per}</span>}
              </div>
              <div className="pcard-billed">{t.billed}</div>
              <ul className="pcard-feats">
                {t.feats.map((f,i) => (<li key={i}><CheckIcon /><span>{f}</span></li>))}
              </ul>
              <a className={"btn " + (t.primary ? "btn-primary" : "btn-ghost") + " pcard-cta"}
                 href={t.href} {...(t.ext ? {target:"_blank", rel:"noopener noreferrer"} : {})}
                 onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'pricing',plan:t.plan}); }}>
                {t.cta}</a>
            </div>
          ))}
        </div>

        <p className="pfoot">Community is free for real work, not a trial. Team pricing is being finalized; early users will get the best terms we ever offer.</p>
        <p className="pfoot pdemo">Want it hosted and always on?{" "}
          <a href={BOOK_CALL_LINK} target="_blank" rel="noopener noreferrer"
             onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'pricing',cta:'book_demo'}); }}>
            Book a 20-min demo</a> and we'll run it on your own bill.</p>
      </div>
    </section>
  );
}

/* Foot CTA */
const FAQ_QA = [
  ["What is nable?",
   "nable is a local-first, AI-native FinOps tool. It is an MCP server you install on your own machine to ask about your AWS, Azure, GCP, and AI or LLM spend right inside Claude, Cursor, or any MCP editor. Your credentials never leave your machine."],
  ["Is nable free?",
   "Yes. The Community tier is free with no credit card and no expiry: cost queries, anomaly detection, rightsizing, the agent team, LLM spend tracking, and every connector, all running on your own machine. Enterprise adds managed single-tenant hosting with always-on monitoring, team dashboards, SSO and an SLA; contact us for a demo."],
  ["Does nable see or store my cloud credentials?",
   "No. nable runs on your machine and nothing is shipped to a vendor. If you connect with an AWS or GCP SSO login or a CLI profile, nable only references it and stores no secret. Keys you paste directly are encrypted in your OS keyring. Cost data caches in a local SQLite database, and there is no nable backend that holds any of it."],
  ["Can nable change my cloud infrastructure on its own?",
   "No. nable is propose-only. It drafts a pull request or opens a ticket with the fix, and a human reviews and applies it. It never edits, deletes, or buys anything in your environment autonomously."],
  ["What clouds and tools does nable support?",
   "AWS, Azure, GCP, and Kubernetes, plus more than ten SaaS and AI providers including Datadog, Snowflake, Databricks, Stripe, OpenAI, Anthropic, and Amazon Bedrock. It exposes 180+ tools your editor can call. None of them can change your infrastructure: fixes ship as pull requests or tickets a human approves."],
  ["How is nable different from Vantage, CloudHealth, or the AWS FinOps agent?",
   "nable is local-first, your credentials and bills never leave your machine; AI-native, it lives in Claude or Cursor instead of a separate dashboard; and genuinely cross-cloud, including AI and LLM spend in the same answer. It proposes fixes as pull requests for human approval rather than acting on its own."],
  ["What is a FinOps MCP server?",
   "MCP, the Model Context Protocol, lets AI editors call external tools. A FinOps MCP server exposes cloud-cost tools to your AI editor, so you ask about spend in your own words and the editor calls the right tool. nable is a local-first FinOps MCP server."],
  ["Does nable normalize cost data across providers?",
   "Yes. Every provider is normalized into FOCUS 1.2, the FinOps Foundation's open billing standard. nable extends the standard past AWS, Azure, and GCP to usage-based SaaS like Snowflake, Datadog, MongoDB Atlas, and Databricks, and to per-model AI spend from OpenAI, Anthropic, OpenRouter, and LiteLLM with token counts preserved. One query spans your whole bill because every provider answers in the same schema."],
  ["Can nable show what my AI coding costs?",
   "Yes. nable attributes merged pull requests and commits to the AI model that wrote them and joins your LLM spend by model, so you can see what each model shipped and what it cost per pull request or per commit."],
];
function Faq(){
  return (
    <section className="faq" id="faq">
      <div className="wrap faq-wrap">
        <div className="foot-label"><span className="foot-dash"></span>FAQ</div>
        <h2 className="faq-h">Common questions</h2>
        <div className="faq-list">
          {FAQ_QA.map(([q,a],i)=>(
            <details className="faq-item" key={i}>
              <summary className="faq-q">{q}</summary>
              <p className="faq-a">{a}</p>
            </details>
          ))}
        </div>
      </div>
    </section>
  );
}
function FootCta(){
  return (
    <section className="foot-cta" id="cta">
      <div className="wrap">
        <div className="foot-label"><span className="foot-dash"></span>Get started</div>
        <h2 className="display">
          One command.<br/>
          <em>Then just ask.</em>
        </h2>
        <div className="foot-cta-actions">
          <div className="foot-install"><CopyCmd cmd="uvx nable" /></div>
          <a href="/docs#quickstart" className="foot-quicklink"
             onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'footer_cta',cta:'quickstart'}); }}>
            or read the quickstart <span className="arr">→</span>
          </a>
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
            <a href="/#demo">Demo</a>
            <a href="/#connectors">Connectors</a>
            <a href="/pricing">Pricing</a>
            <a href="/pricing#faq">FAQ</a>
            <a href="https://calendar.app.google/2duYBqjLXaTmX5xC8" target="_blank" rel="noopener noreferrer"
               onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'footer_nav',cta:'book_demo'}); }}>Book a demo</a>
          </div>
          <div>
            <h5>Resources</h5>
            <a href="/docs">Docs</a>
            <a href="/guides">Guides &amp; comparisons</a>
            <a href="/docs#quickstart">Quickstart</a>
            <a href="/docs#iam">IAM templates</a>
            <a href="/security">Security</a>
          </div>
          <div>
            <h5>Company</h5>
            <a href="/about">About</a>
            <a href="mailto:chaandannn@gmail.com" target="_blank" rel="noopener noreferrer">Contact</a>
            <a href="https://github.com/chaandannn/finopsmcp" target="_blank" rel="noopener noreferrer" onClick={()=>{ if(window.posthog) posthog.capture('github_star_clicked',{location:'footer'}); }}>Star on GitHub <span style={{color:'var(--warn)'}}>★</span></a>
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

/* Tweaks panel */
const PALETTE_OPTIONS = [
  {value:"onyx",     label:"Onyx",     swatch:["#0a0a0c","#5fe8a0","#15151a"]},
  {value:"graphite", label:"Graphite", swatch:["#000000","#4db8d4","#121214"]},
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
              fontFamily:"'Geist',system-ui,sans-serif",fontSize:12,cursor:"pointer",
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
      <TweakSection label="Interaction">
        <TweakRadio label="Console queries" value={t.interaction}
          options={[{value:"cycling",label:"Auto"},{value:"static",label:"Manual"}]}
          onChange={(v)=>setTweak("interaction",v)} />
      </TweakSection>
    </TweaksPanel>
  );
}

/* App */
/* The loop: what makes nable an agent, not a dashboard */
function Loop(){
  return (
    <section id="loop" className="alt" style={{borderTop:"1px solid var(--line)"}}>
      <div className="wrap">
        <div className="section-head center">
          <div className="label">How it works</div>
          <h2>Most tools show you the problem.<br/><em>nable fixes it.</em></h2>
          <p>Other cost tools just tell you the bill went up. nable finds what caused it, writes the fix, and proves the savings, getting smarter about your setup every time.</p>
        </div>
        <div className="loop-grid">
          <div className="loop-step">
            <div className="loop-n">01</div>
            <h3>Find the cause</h3>
            <p>It points to the exact change that drove your bill up, down to the day it happened, so you stop digging through dashboards.</p>
          </div>
          <div className="loop-step">
            <div className="loop-n">02</div>
            <h3>Fix it</h3>
            <p>It writes the fix and waits for your go-ahead. nable never changes anything on its own. You're always in control.</p>
          </div>
          <div className="loop-step">
            <div className="loop-n">03</div>
            <h3>Prove it</h3>
            <p>After you approve, it checks your next bill to confirm the money was really saved, then learns what works for you and gets smarter every time.</p>
          </div>
        </div>
        <div className="agents-band">
          <div className="agents-band-copy">
            <div className="band-tag">Agent cost controls</div>
            <h3>Your agents ask nable <em>before they spend.</em></h3>
            <p>An agent calls nable to price a change and check it against your budgets, before anything is applied. Live now in every install.</p>
          </div>
          <a className="btn btn-primary" href="/agents"
             onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'agents_band',cta:'see_agents'}); }}>
            See how it works <span className="arr">→</span>
          </a>
        </div>
      </div>
    </section>
  );
}

// Scroll-reveal: fade + rise each wrapped block in as it enters the viewport.
function Reveal({ children }){
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (!("IntersectionObserver" in window)) { el.classList.add("in"); return; }
    const io = new IntersectionObserver(([e]) => {
      if (e.isIntersecting) { el.classList.add("in"); io.disconnect(); }
    }, { threshold: 0.12, rootMargin: "0px 0px -8% 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return <div ref={ref} className="reveal">{children}</div>;
}

// The product, front and center. A scripted, native animation instead of a screen
// recording: always crisp, no dead space, no real account ids, every beat staged.
// Beats: question types -> tool chips -> filled cost table -> driver + offer ->
// "Yes, open it" -> PR drafted card -> hold -> loop.
const DT_ROWS = [
  { svc: "EC2",   usd: "$5,184", d: "+21%", dir: "up" },
  { svc: "EKS",   usd: "$3,821", d: "+34%", dir: "up" },
  { svc: "RDS",   usd: "$2,244", d: "+3%",  dir: "flat" },
  { svc: "S3",    usd: "$684",   d: "-2%",  dir: "down" },
  { svc: "Other", usd: "$1,727", d: "+1%",  dir: "flat" },
];
const DT_Q = "Just downloaded nable, why is our AWS bill up this month?";

function DemoTheater(){
  const [step, setStep] = useState(0);       // timeline stage
  const [typed, setTyped] = useState(0);     // chars of the question typed
  const reduced = typeof window !== "undefined" &&
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  useEffect(() => {
    if (reduced) { setStep(9); setTyped(DT_Q.length); return; }
    let alive = true;
    const timers = [];
    const at = (ms, fn) => timers.push(setTimeout(() => { if (alive) fn(); }, ms));
    const run = () => {
      setStep(1); setTyped(0);
      // type the question
      for (let i = 1; i <= DT_Q.length; i++) at(300 + i * 24, () => setTyped(i));
      at(2100, () => setStep(2));   // chip: get_cost_summary
      at(2800, () => setStep(3));   // chip: explain_cost_change
      at(3600, () => setStep(4));   // headline
      at(4100, () => setStep(5));   // table rows (stagger via css)
      at(6300, () => setStep(6));   // driver + offer
      at(8600, () => setStep(7));   // user: yes, open it
      at(9600, () => setStep(8));   // PR card
      at(15000, () => setStep(0));  // fade out
      at(15800, run);               // loop
    };
    run();
    return () => { alive = false; timers.forEach(clearTimeout); };
  }, [reduced]);

  const s = (n) => step >= n ? " on" : "";
  return (
    <div className={"dt" + (step === 0 ? " dt-dim" : "")} aria-label="nable demo: ask why the AWS bill is up, get the driver and a drafted fix">
      <div className="dt-bar">
        <span className="dt-dot"/><span className="dt-dot"/><span className="dt-dot"/>
        <span className="dt-title">your editor · nable connected</span>
        <span className="dt-badge">read-only</span>
      </div>
      <div className="dt-body">
        <div className={"dt-msg dt-user" + s(1)}>
          {DT_Q.slice(0, typed)}<span className={"dt-caret" + (step >= 2 ? " off" : "")}/>
        </div>
        <div className={"dt-chip" + s(2)}><span className="dt-chip-dot"/>nable · get_cost_summary</div>
        <div className={"dt-chip" + s(3)}><span className="dt-chip-dot"/>nable · explain_cost_change</div>
        <div className={"dt-ans" + s(4)}>
          <div className="dt-headline"><b>$13,660</b> on AWS, last 30 days · <span className="dt-up">up 18%</span> vs the prior 30 · account acme-prod</div>
          <div className={"dt-table" + s(5)}>
            {DT_ROWS.map((r, i) => (
              <div className="dt-row" style={{transitionDelay: (i * 120) + "ms"}} key={r.svc}>
                <span className="dt-svc">{r.svc}</span>
                <span className="dt-usd">{r.usd}</span>
                <span className={"dt-delta dt-" + r.dir}>{r.d}</span>
              </div>
            ))}
          </div>
          <div className={"dt-driver" + s(6)}>
            Driver: the <b>EKS node pool doubled on Jun 12</b> and sits at 38% utilization,
            about <b>$1,240/mo idle</b>. Want the rightsizing PR?
          </div>
        </div>
        <div className={"dt-msg dt-user dt-short" + s(7)}>Yes, open it.</div>
        <div className={"dt-pr" + s(8)}>
          <span className="dt-pr-branch">⎇ rightsize-eks-nodepool</span>
          <span className="dt-pr-stat"><i className="dt-add">+214</i> <i className="dt-del">-31</i></span>
          <span className="dt-pr-label">PR drafted · you review, nothing auto-applies</span>
        </div>
      </div>
    </div>
  );
}

function DemoVideo(){
  return (
    <section id="demo" className="demo-sec">
      <div className="wrap">
        <div className="section-head center">
          <div className="label">See it work</div>
          <h2>Watch nable<br/><em>find the money.</em></h2>
          <p>Just ask. nable reads your real bill, finds what changed, and drafts the fix, live.</p>
        </div>
        <div className="demo-video-frame">
          <DemoTheater/>
        </div>
        <div className="postdemo">
          <span className="postdemo-l">Same answers, on your real bill, in about a minute:</span>
          <code className="aicost-cmd postdemo-cmd" onClick={()=>{
            if(navigator.clipboard) navigator.clipboard.writeText("uvx nable");
            if(window.posthog) posthog.capture('install_copied', {location:'post_demo'});
          }}>uvx nable</code>
          <a className="postdemo-docs" href="/docs" onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'post_demo',cta:'setup_guide'}); }}>2-minute setup guide <span className="arr">&rarr;</span></a>
        </div>
      </div>
    </section>
  );
}

// The pricing page (/pricing): the homepage's Pricing + FAQ on their own route, so
// the front page stays focused on what nable is.
function PricingPage(){
  return (
    <div className="page-content">
      <Nav />
      <Pricing />
      <Faq />
      <Footer version={null} />
    </div>
  );
}

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
      <div className="page-atmos" aria-hidden="true">
        <svg className="atmos-svg" width="100%" height="100%" preserveAspectRatio="xMidYMid slice">
          <defs>
            <pattern id="natmos" width="260" height="260" patternUnits="userSpaceOnUse">
              <g stroke="#ffffff" strokeOpacity="0.05" strokeWidth="1" fill="none">
                <path d="M0 70 H160 V260"/>
                <path d="M260 188 H104 V0"/>
                <path d="M0 214 H48 V128 H132"/>
              </g>
              <g fill="#4db8d4" fillOpacity="0.45">
                <circle cx="160" cy="70" r="2.1"/>
                <circle cx="104" cy="188" r="2.1"/>
                <circle cx="132" cy="128" r="1.8"/>
              </g>
            </pattern>
          </defs>
          <rect width="100%" height="100%" fill="url(#natmos)"/>
        </svg>
      </div>
      <div className="page-content">
      <Nav />
      <Hero />
      <Reveal><DemoVideo /></Reveal>
      <Reveal><Loop /></Reveal>
      <Reveal><AiCost /></Reveal>
      <Reveal><Connectors /></Reveal>
      <Reveal><Architecture version={version} /></Reveal>
      <FootCta />
      <Footer version={version} />
      <Tweaks />
      </div>
    </>
  );
}

// Enable scroll-reveal only when JS runs, so no-JS visitors see all content immediately.
if (typeof document !== "undefined") document.documentElement.classList.add("js");

// /pricing(.html) renders just the pricing view; every other path is the homepage.
const _path = (typeof location !== "undefined" ? location.pathname : "/");
const _isPricing = _path === "/pricing" || _path === "/pricing.html" || _path === "/pricing/";
ReactDOM.createRoot(document.getElementById("app")).render(_isPricing ? <PricingPage /> : <App />);
