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
    const sections = ['demo', 'connectors', 'architecture', 'pricing', 'foot-cta'];
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
  return (
    <svg width="26" height="26" viewBox="0 0 120 120" className="mark-img" aria-hidden="true">
      <defs><linearGradient id="nmg" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stopColor="#5cc1da"/><stop offset="1" stopColor="#3a9ab6"/>
      </linearGradient></defs>
      <rect width="120" height="120" rx="27" fill="url(#nmg)"/>
      <path d="M44 80 L44 56 A16 16 0 0 1 76 56 L76 80" fill="none" stroke="#000000" strokeWidth="13" strokeLinecap="round" strokeLinejoin="round"/>
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
          <li><a href="/pricing.html" onClick={()=>{ if(window.posthog) posthog.capture('nav_clicked',{item:'pricing'}); }}>Pricing</a></li>
          <li><a href="/docs.html" onClick={()=>{ if(window.posthog) posthog.capture('docs_clicked',{location:'nav'}); }}>Docs</a></li>
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
          <a className="nav-mobile-item" href="/pricing.html" onClick={()=>{ if(window.posthog) posthog.capture('nav_clicked',{item:'pricing'}); }}>Pricing</a>
          <a className="nav-mobile-item" href="/docs.html" onClick={()=>{ setOpen(false); if(window.posthog) posthog.capture('docs_clicked',{location:'nav_mobile'}); }}>Docs</a>
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
// The hero is always the split layout: copy left, live console right (per
// DESIGN.md). The centered "editorial" variant was retired, so layout is
// accepted for compatibility but no longer switches the arrangement.
function Hero(){
  return (
    <header className="hero hero-centered" id="top">
      <div className="wrap">
        <div className="hero-c">
          <h1 className="display">
            Stop guessing why cloud costs went up. <span className="h1-ask">Ask.</span>
          </h1>
          <p className="hero-sub">Then it finds the waste, writes the fix for you to approve, and proves the savings on your next bill.</p>
          <div className="hero-actions">
            <CopyCmd cmd="uvx nable" />
            <a className="btn btn-primary" href="/docs.html" onClick={() => { if(window.posthog) posthog.capture('cta_clicked', { location:'hero', cta:'start_free' }); }}>
              Get started free <span className="arr">→</span>
            </a>
          </div>
          <p className="hero-trustline">Every cloud + AI bill in <b>one place</b> · works in any editor · free for solo use</p>
        </div>
      </div>
    </header>
  );
}

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
        <p style={{marginTop:12}}>You'd unlock another <span style={{color:"var(--accent)"}}>$8,200 / mo</span> by raising Compute SP coverage to 92%. Model it?</p>
      </>
    )
  },
  {
    q: "What can you actually do?",
    response: (
      <>
        <p>A lot, and all of it from your editor. On a connected account I can:</p>
        <ul className="caps">
          <li><b>Answer cost questions</b> across AWS, Azure, GCP, Kubernetes and 13+ SaaS and AI providers</li>
          <li><b>Catch anomalies</b> with Z-score detection and name the tag driving the spike</li>
          <li><b>Find savings</b>: rightsizing, idle cleanup, commitment and discount coverage</li>
          <li><b>Track AI spend</b> by model and forecast where your token bill lands</li>
          <li><b>Act</b>: open a rightsizing PR against your IaC, file a ticket, post to Slack</li>
        </ul>
        <p style={{marginTop:12}}><span style={{color:"var(--accent)"}}>160+ tools</span> in all. Pick a prompt below to run a real one.</p>
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
          <h2>Ask your bill like you'd<br/><em>ask a teammate.</em></h2>
          <p>nable pulls every connected provider, normalizes to USD, and answers in plain English. Watch it run through real questions, or ask your own.</p>
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
        <div className="aicost-panel aicost-panel-center">
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
            <code className="aicost-cmd" onClick={copy}>uvx nable</code>
          </div>
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
          <button role="tab" aria-selected={tab==="run"} className={"arch-tab" + (tab==="run"?" active":"")} onClick={()=>setTab("run")}>Run it or host it</button>
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
                <h4>17 connectors</h4>
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
            <p>Install with one command. Credentials live in your OS keyring, cost data caches in a local SQLite file, and queries hit your provider APIs directly. There is no nable backend in the path and no data lake to breach. For zero AI exposure, use the local dashboard or CLI, which never call a model.</p>
            <div className="gate-cmd"><CopyCmd cmd="uvx nable" /></div>
          </div>
          <div className="host-opt">
            <span className="host-tag">Or let us host it</span>
            <h4>Managed, single-tenant</h4>
            <p>Want it always on without running it yourself? We deploy and manage a single-tenant instance for your org: your own runtime, your own store, isolated from every other customer. Same connectors, same analysis, plus the dashboard with SSO (Okta, Entra ID, Google Workspace), RBAC, and share links. Single-tenant by design, never a shared pool.</p>
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

// Paid checkout links. Pro = $100/mo flat or $1,000/yr (2 months free): local,
// bring your own LLM key. Startups = $1,000/mo or $10,000/yr: org scale, local,
// bring your own LLM key. Hosting (single-tenant + managed AI) is an optional
// credit-based add-on on either plan, billed on top, use-it-or-lose-it.
const PRO_MONTHLY_LINK     = "https://buy.stripe.com/9B600igyt1oO1d69V02Nq06";
const PRO_ANNUAL_LINK      = "https://buy.stripe.com/bJe5kCbe97Nc0924AG2Nq07";
const STARTUP_MONTHLY_LINK = "https://buy.stripe.com/3cI3cucid6J85tm3wC2Nq08";
const STARTUP_ANNUAL_LINK  = "https://buy.stripe.com/14A6oG0zvgjI9JCffk2Nq09";

const BOOK_CALL_LINK = "https://calendar.app.google/2duYBqjLXaTmX5xC8";

// Comparison rows. value true -> check, false -> dash, string -> mono text.
const PRICE_ROWS = [
  { label: "Users",                                         dev: "Just you",     pro: "Your team",    startup: "Your org",              ent: "Your org" },
  { label: "Cost queries, anomalies, rightsizing, 17 connectors", dev: true,     pro: true,           startup: true,                    ent: true },
  { label: "Remediation PRs, alerts, dashboards, Slack bot", dev: false,         pro: true,           startup: true,                    ent: true },
  { label: "Runs",                                          dev: "Your machine", pro: "Your machine", startup: "Your machine",          ent: "Hosted or self-host" },
  { label: "Managed AI",                                    dev: "Your own key", pro: "Your own key", startup: "Your own key",          ent: "Custom" },
  { label: "Hosting + managed AI (add-on)",                 dev: false,          pro: "+$200/mo · 500 credits", startup: "+$4,000/mo · 10,000 credits", ent: "Custom" },
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
  const [annual, setAnnual] = useState(false);

  const proPrice     = annual ? "$1,000" : "$100";
  const startupPrice = annual ? "$10,000" : "$1,000";
  const per    = annual ? "/yr" : "/mo";
  const billed = annual ? "Billed annually" : "Billed monthly";
  const proLink     = annual ? PRO_ANNUAL_LINK : PRO_MONTHLY_LINK;
  const startupLink = annual ? STARTUP_ANNUAL_LINK : STARTUP_MONTHLY_LINK;

  const tiers = [
    { key:"dev", name:"Dev", tag:"Ask your bill anything", amt:"Free", per:"forever", billed:"No credit card",
      feats:["Cost, anomaly + rightsizing","LLM spend by model","All 17 connectors","Your own LLM key"],
      cta:"Start free", href:"/docs.html", plan:"dev", ext:false, primary:false, rec:false },
    { key:"pro", name:"Pro", tag:"Find and fix the waste", amt:proPrice, per, billed,
      feats:["Everything in Dev","Remediation PRs + tickets","Alerts, digests, budgets","Hosting add-on available"],
      cta:annual ? "Get annual" : "Get Pro", href:proLink, plan:annual?"pro_annual":"pro_monthly", ext:true, primary:true, rec:true },
    { key:"startup", name:"Startups", tag:"Scale to the whole org", amt:startupPrice, per, billed,
      feats:["Everything in Pro","Org scale, more accounts","Priority support","10,000-credit hosting tier"],
      cta:"Get Startups", href:startupLink, plan:annual?"startups_annual":"startups_monthly", ext:true, primary:false, rec:false },
    { key:"ent", name:"Enterprise", tag:"Controls, SSO + an SLA", amt:"Custom", per:"", billed:"Talk to us",
      feats:["Everything in Startups","SSO + audit logs","Dedicated SLA","Hosted or self-host"],
      cta:"Contact us", href:BOOK_CALL_LINK, plan:"enterprise", ext:true, primary:false, rec:false },
  ];

  return (
    <section id="pricing">
      <div className="wrap">
        <div className="section-head center">
          <div className="label">Pricing</div>
          <h2>Free to ask.<br/><em>Pay to remediate.</em></h2>

          {/* Billing toggle: segmented control, matched to the dashboard range group. */}
          <div className="bill-toggle" role="group" aria-label="Billing period">
            <div className="seg">
              <button className={"seg-btn" + (annual ? "" : " active")} onClick={()=>setAnnual(false)} aria-pressed={!annual}>Monthly</button>
              <button className={"seg-btn" + (annual ? " active" : "")} onClick={()=>setAnnual(true)} aria-label="Toggle annual billing" aria-pressed={annual}>Annual</button>
            </div>
            <span className="seg-save">SAVE 17%</span>
          </div>
        </div>

        <PricingCards tiers={tiers} annual={annual} />

        <div className="phost">
          <div className="phost-label">Hosting add-on</div>
          <p className="phost-body">Optional on Pro or Startups. We run nable single-tenant with a managed AI agent, billed on top of your plan in monthly credits that reset each month, use them or lose them.</p>
          <div className="phost-rows">
            <div className="phost-row"><span className="phost-tier">Pro</span><span className="phost-price">500 credits · $200/mo</span></div>
            <div className="phost-row"><span className="phost-tier">Startups</span><span className="phost-price">10,000 credits · $4,000/mo</span></div>
          </div>
        </div>

        <details className="pcompare">
          <summary>Compare all features</summary>
          <div className="ptable-wrap">
            <div className="ptable ptable-4">
              {/* header row */}
              <div className="ph ph-corner"></div>
              <div className="ph">
                <div className="pt-name">Dev</div>
                <div className="pt-price"><span className="pt-amt">Free</span><span className="pt-per">forever</span></div>
              </div>
              <div className="ph pcol-team">
                <div className="pt-rec">Recommended</div>
                <div className="pt-name">Pro</div>
                <div className="pt-price"><span className="pt-amt">{proPrice}</span><span className="pt-per">{per}</span></div>
              </div>
              <div className="ph">
                <div className="pt-name">Startups</div>
                <div className="pt-price"><span className="pt-amt">{startupPrice}</span><span className="pt-per">{per}</span></div>
              </div>
              <div className="ph">
                <div className="pt-name">Enterprise</div>
                <div className="pt-price"><span className="pt-amt">Custom</span><span className="pt-per">annual</span></div>
              </div>

              {/* feature rows */}
              {PRICE_ROWS.map((r,i) => (
                <React.Fragment key={i}>
                  <div className="pr pr-label">{r.label}</div>
                  <div className="pr pr-cell"><PCell v={r.dev} /></div>
                  <div className="pr pr-cell pcol-team"><PCell v={r.pro} /></div>
                  <div className="pr pr-cell"><PCell v={r.startup} /></div>
                  <div className="pr pr-cell"><PCell v={r.ent} /></div>
                </React.Fragment>
              ))}
            </div>
          </div>
        </details>
        <p className="pfoot">No credit card for Dev. Pro and Startups trials require a card, cancel any time.</p>
        <p className="pfoot pdemo">Weighing Pro or Startups for your org?{" "}
          <a href="https://calendar.app.google/2duYBqjLXaTmX5xC8" target="_blank" rel="noopener noreferrer"
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
   "Yes. The Dev tier is free with no credit card and no expiry: cost queries, anomaly detection, rightsizing, LLM spend tracking, and every connector. Paid tiers add remediation pull requests, alerts, scheduled digests, with single-tenant hosting available as a credit-based add-on."],
  ["Does nable see or store my cloud credentials?",
   "No. nable runs on your machine. Credentials stay in your OS keyring and cost data caches in a local SQLite database. There is no nable backend that holds your data, and nothing is shipped to a vendor."],
  ["Can nable change my cloud infrastructure on its own?",
   "No. nable is propose-only. It drafts a pull request or opens a ticket with the fix, and a human reviews and applies it. It never edits, deletes, or buys anything in your environment autonomously."],
  ["What clouds and tools does nable support?",
   "AWS, Azure, GCP, and Kubernetes, plus more than ten SaaS and AI providers including Datadog, Snowflake, Databricks, Stripe, OpenAI, Anthropic, and Amazon Bedrock. It exposes 165+ read-only tools your editor can call."],
  ["How is nable different from Vantage, CloudHealth, or the AWS FinOps agent?",
   "nable is local-first, your credentials and bills never leave your machine; AI-native, it lives in Claude or Cursor instead of a separate dashboard; and genuinely cross-cloud, including AI and LLM spend in the same answer. It proposes fixes as pull requests for human approval rather than acting on its own."],
  ["What is a FinOps MCP server?",
   "MCP, the Model Context Protocol, lets AI editors call external tools. A FinOps MCP server exposes cloud-cost tools to your AI editor, so you ask about spend in your own words and the editor calls the right tool. nable is a local-first FinOps MCP server."],
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
          <a href="/docs.html#quickstart" className="foot-quicklink"
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
            <a href="/pricing.html">Pricing</a>
            <a href="/pricing.html#faq">FAQ</a>
            <a href="https://calendar.app.google/2duYBqjLXaTmX5xC8" target="_blank" rel="noopener noreferrer"
               onClick={()=>{ if(window.posthog) posthog.capture('cta_clicked',{location:'footer_nav',cta:'book_demo'}); }}>Book a demo</a>
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
              fontFamily:"'Bricolage Grotesque',system-ui,sans-serif",fontSize:12,cursor:"pointer",
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
      </div>
    </section>
  );
}

/* Non-technical bridge: the 3-step shape (install, connect read-only, ask) so a
   buyer sees how simple setup is before the docs go deep on IAM and CloudFormation. */
function GetStarted(){
  return (
    <section id="start" className="alt" style={{borderTop:"1px solid var(--line)"}}>
      <div className="wrap">
        <div className="section-head center">
          <div className="label">Get started</div>
          <h2>Two minutes, three steps,<br/><em>no SQL, no dashboards.</em></h2>
          <p>Give nable read-only access to your bill, then ask questions in plain English. That is the whole setup.</p>
        </div>
        <div className="start-grid">
          <div className="start-step">
            <div className="start-n">1</div>
            <h3>Install</h3>
            <p>One command, <code>uvx nable</code>. It runs on your machine. No account, no signup.</p>
          </div>
          <div className="start-step">
            <div className="start-n">2</div>
            <h3>Connect, read-only</h3>
            <p>Point it at AWS, Azure, or GCP. nable only ever reads your bill, and your credentials stay in your OS keyring, never on our servers.</p>
          </div>
          <div className="start-step">
            <div className="start-n">3</div>
            <h3>Ask</h3>
            <p>In Claude or Cursor, ask why the bill went up. Get the cause, the cost, and the fix, in plain English.</p>
          </div>
        </div>
      </div>
    </section>
  );
}

/* The payoff: verified, learning savings (the differentiation people miss) */
function ProofBand(){
  return (
    <section className="proof-band">
      <div className="wrap">
        <p className="proof-line">Every other tool claims a number. <em>nable proves it on your bill</em>, and gets smarter every week.</p>
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
      <SeeItWork interaction={t.interaction} />
      <GetStarted />
      <Loop />
      <ProofBand />
      <AiCost />
      <Connectors />
      <Architecture version={version} />
      <FootCta />
      <Footer version={version} />
      <Tweaks />
      </div>
    </>
  );
}

// /pricing(.html) renders just the pricing view; every other path is the homepage.
const _path = (typeof location !== "undefined" ? location.pathname : "/");
const _isPricing = _path === "/pricing" || _path === "/pricing.html" || _path === "/pricing/";
ReactDOM.createRoot(document.getElementById("app")).render(_isPricing ? <PricingPage /> : <App />);
