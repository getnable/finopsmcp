const { useState, useEffect, useRef } = React;
const TWEAK_DEFAULTS = (
  /*EDITMODE-BEGIN*/
  {
    "palette": "graphite",
    "layout": "split",
    "interaction": "cycling"
  }
);
const PALETTES = {
  onyx: {
    "--bg": "#0a0a0c",
    "--bg-1": "#0f0f12",
    "--bg-2": "#15151a",
    "--bg-3": "#1d1d24",
    "--line": "#22222b",
    "--line-2": "#2e2e38",
    "--fg": "#f4f4f0",
    "--fg-2": "#a8a8a2",
    "--fg-3": "#6e6e68",
    "--fg-4": "#46463f",
    "--accent": "#5fe8a0",
    "--accent-dim": "#3aa676",
    "--warn": "#ffb46b",
    "--alert": "#ff7a6b",
    "--grid": "rgba(255,255,255,.03)"
  },
  graphite: {
    "--bg": "#0d0f10",
    "--bg-1": "#111416",
    "--bg-2": "#181c1f",
    "--bg-3": "#1e2327",
    "--line": "#242a2e",
    "--line-2": "#2e3539",
    "--fg": "#f0f2f3",
    "--fg-2": "#94a3ab",
    "--fg-3": "#56656d",
    "--fg-4": "#2d3a40",
    "--accent": "#4db8d4",
    "--accent-dim": "#2c7d91",
    "--warn": "#e6a840",
    "--alert": "#e05c4b",
    "--success": "#3cba7a",
    "--grid": "rgba(255,255,255,.02)"
  },
  paper: {
    "--bg": "#fbfaf7",
    "--bg-1": "#f6f4ee",
    "--bg-2": "#eeebe2",
    "--bg-3": "#e5e1d3",
    "--line": "#e3dfcf",
    "--line-2": "#d2cdb9",
    "--fg": "#1a1915",
    "--fg-2": "#4d4b42",
    "--fg-3": "#85806f",
    "--fg-4": "#b4ae9b",
    "--accent": "#1f8a5b",
    "--accent-dim": "#3b6e3a",
    "--warn": "#b8533a",
    "--alert": "#b8533a",
    "--grid": "rgba(0,0,0,.04)"
  },
  mono: {
    "--bg": "#ffffff",
    "--bg-1": "#fafafa",
    "--bg-2": "#f2f2f0",
    "--bg-3": "#e8e8e5",
    "--line": "#e6e6e3",
    "--line-2": "#d0d0cc",
    "--fg": "#0a0a0a",
    "--fg-2": "#525252",
    "--fg-3": "#8a8a85",
    "--fg-4": "#b8b8b3",
    "--accent": "#0a0a0a",
    "--accent-dim": "#3a3a3a",
    "--warn": "#666",
    "--alert": "#0a0a0a",
    "--grid": "rgba(0,0,0,.035)"
  }
};
function applyPalette(name) {
  const p = PALETTES[name] || PALETTES.graphite;
  const root = document.documentElement;
  Object.entries(p).forEach(([k, v]) => root.style.setProperty(k, v));
}
function useScrollTracking() {
  useEffect(() => {
    if (!window.posthog) return;
    const sections = ["connectors", "depth", "architecture", "pricing", "faq", "foot-cta"];
    const seen = /* @__PURE__ */ new Set();
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting && !seen.has(entry.target.id)) {
          seen.add(entry.target.id);
          posthog.capture("section_viewed", { section: entry.target.id });
        }
      });
    }, { threshold: 0.2 });
    sections.forEach((id) => {
      const el = document.getElementById(id);
      if (el) observer.observe(el);
    });
    return () => observer.disconnect();
  }, []);
}
function EmailCapture({ source = "hero", placeholder = "email", btnLabel = "Get started", center = false }) {
  const [email, setEmail] = useState("");
  const [state, setState] = useState("idle");
  async function submit(e) {
    e.preventDefault();
    if (!email || state === "loading" || state === "done") return;
    setState("loading");
    try {
      const res = await fetch("/api/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, source })
      });
      if (!res.ok) throw new Error("subscribe failed");
      if (window.posthog) posthog.capture("email_subscribed", { source });
      setState("done");
    } catch {
      setState("error");
      setTimeout(() => setState("idle"), 3e3);
    }
  }
  if (state === "done") {
    return /* @__PURE__ */ React.createElement("p", { style: {
      fontFamily: "'Instrument Sans',system-ui,sans-serif",
      fontSize: 12,
      color: "var(--accent)",
      letterSpacing: ".02em",
      textAlign: center ? "center" : "left",
      marginTop: 8
    } }, "Check your inbox. Setup guide on its way.");
  }
  return /* @__PURE__ */ React.createElement(
    "form",
    {
      className: "email-capture" + (center ? " center" : ""),
      onSubmit: submit,
      style: { margin: center ? "0 auto" : "0" }
    },
    /* @__PURE__ */ React.createElement(
      "input",
      {
        type: "email",
        value: email,
        onChange: (e) => setEmail(e.target.value),
        placeholder,
        required: true,
        autoComplete: "email",
        "aria-label": "Email"
      }
    ),
    /* @__PURE__ */ React.createElement("button", { type: "submit", disabled: state === "loading" }, state === "loading" ? "..." : btnLabel, " ", /* @__PURE__ */ React.createElement("span", { className: "arr" }, "\u2192")),
    state === "error" && /* @__PURE__ */ React.createElement("span", { style: {
      position: "absolute",
      bottom: -20,
      left: 0,
      fontSize: 11,
      color: "var(--alert)",
      fontFamily: "'Instrument Sans',system-ui,sans-serif"
    } }, "Something went wrong. Try again.")
  );
}
function LogoMark() {
  return /* @__PURE__ */ React.createElement("svg", { width: "26", height: "26", viewBox: "0 0 32 32", className: "mark-img", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("rect", { width: "32", height: "32", rx: "7", fill: "var(--accent)" }), /* @__PURE__ */ React.createElement("path", { d: "M9.5 23V11.5h2.6v1.5c.7-1.1 1.9-1.7 3.4-1.7 2.6 0 4.2 1.7 4.2 4.5V23h-2.7v-6.6c0-1.7-.9-2.6-2.4-2.6s-2.5 1-2.5 2.7V23H9.5Z", fill: "var(--bg)" }));
}
function Ticker({ installs, version }) {
  return /* @__PURE__ */ React.createElement("div", { className: "ticker" }, /* @__PURE__ */ React.createElement("div", { className: "ticker-inner" }, /* @__PURE__ */ React.createElement("span", { className: "seg" }, /* @__PURE__ */ React.createElement("span", { className: "dot" }), /* @__PURE__ */ React.createElement("b", null, "finops-mcp"), /* @__PURE__ */ React.createElement("span", null, version ? `v${version}` : "v0.8.36", " \xB7 runtime healthy")), /* @__PURE__ */ React.createElement("span", { className: "sep" }, "\xB7"), /* @__PURE__ */ React.createElement("span", { className: "seg" }, installs ? fmtNum(installs) : "4k+", " installs / mo via PyPI"), /* @__PURE__ */ React.createElement("span", { className: "sep" }, "\xB7"), /* @__PURE__ */ React.createElement("span", { className: "seg" }, "17 connectors \xB7 AWS \xB7 Azure \xB7 GCP +14"), /* @__PURE__ */ React.createElement("span", { className: "sep" }, "\xB7"), /* @__PURE__ */ React.createElement("span", { className: "seg" }, /* @__PURE__ */ React.createElement("a", { href: "/about", style: { color: "var(--accent)", textDecoration: "none", fontWeight: 500 } }, "About & investors \u2192"))));
}
function Nav() {
  const [open, setOpen] = useState(false);
  function scrollTo(id) {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth" });
    setOpen(false);
  }
  return /* @__PURE__ */ React.createElement("nav", { className: "nav" }, /* @__PURE__ */ React.createElement("div", { className: "nav-inner" }, /* @__PURE__ */ React.createElement("a", { href: "/", className: "logo" }, /* @__PURE__ */ React.createElement(LogoMark, null), /* @__PURE__ */ React.createElement("span", null, "nable")), /* @__PURE__ */ React.createElement("ul", null, /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("button", { className: "nav-link", onClick: () => scrollTo("connectors") }, "Connectors")), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("button", { className: "nav-link", onClick: () => scrollTo("pricing") }, "Pricing")), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("button", { className: "nav-link", onClick: () => {
    scrollTo("faq");
    if (window.posthog) posthog.capture("nav_clicked", { item: "faq" });
  } }, "FAQ")), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("a", { href: "/docs.html", onClick: () => {
    if (window.posthog) posthog.capture("docs_clicked", { location: "nav" });
  } }, "Docs")), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("a", { href: "/about" }, "About")), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("a", { href: "https://github.com/chaandannn/finopsmcp", target: "_blank", rel: "noopener noreferrer", onClick: () => {
    if (window.posthog) posthog.capture("nav_clicked", { item: "github" });
  } }, "GitHub"))), /* @__PURE__ */ React.createElement("div", { className: "right" }, /* @__PURE__ */ React.createElement("a", { href: "/account.html", className: "btn btn-ghost" }, "Sign in"), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn btn-primary",
      onClick: () => {
        scrollTo("install");
        if (window.posthog) posthog.capture("cta_clicked", { location: "nav", cta: "start_free" });
      }
    },
    "Get started free ",
    /* @__PURE__ */ React.createElement("span", { className: "arr" }, "\u2192")
  )), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "nav-hamburger",
      "aria-label": open ? "Close menu" : "Open menu",
      "aria-expanded": open,
      onClick: () => setOpen((o) => !o)
    },
    open ? /* @__PURE__ */ React.createElement("svg", { width: "20", height: "20", viewBox: "0 0 20 20", fill: "none", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("path", { d: "M4 4L16 16M16 4L4 16", stroke: "currentColor", strokeWidth: "1.5", strokeLinecap: "round" })) : /* @__PURE__ */ React.createElement("svg", { width: "20", height: "20", viewBox: "0 0 20 20", fill: "none", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("path", { d: "M3 5h14M3 10h14M3 15h14", stroke: "currentColor", strokeWidth: "1.5", strokeLinecap: "round" }))
  )), open && /* @__PURE__ */ React.createElement("div", { className: "nav-mobile-menu" }, /* @__PURE__ */ React.createElement("button", { className: "nav-mobile-item", onClick: () => scrollTo("connectors") }, "Connectors"), /* @__PURE__ */ React.createElement("button", { className: "nav-mobile-item", onClick: () => {
    scrollTo("pricing");
    if (window.posthog) posthog.capture("nav_clicked", { item: "pricing" });
  } }, "Pricing"), /* @__PURE__ */ React.createElement("button", { className: "nav-mobile-item", onClick: () => {
    scrollTo("faq");
    if (window.posthog) posthog.capture("nav_clicked", { item: "faq" });
  } }, "FAQ"), /* @__PURE__ */ React.createElement("a", { className: "nav-mobile-item", href: "/docs.html", onClick: () => {
    setOpen(false);
    if (window.posthog) posthog.capture("docs_clicked", { location: "nav_mobile" });
  } }, "Docs"), /* @__PURE__ */ React.createElement("a", { className: "nav-mobile-item", href: "/about", onClick: () => setOpen(false) }, "About"), /* @__PURE__ */ React.createElement("a", { className: "nav-mobile-item", href: "https://github.com/chaandannn/finopsmcp", target: "_blank", rel: "noopener noreferrer", onClick: () => {
    setOpen(false);
    if (window.posthog) posthog.capture("nav_clicked", { item: "github" });
  } }, "GitHub"), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 24, display: "flex", flexDirection: "column", gap: 10 } }, /* @__PURE__ */ React.createElement("a", { href: "/account.html", className: "btn btn-ghost", style: { justifyContent: "center" }, onClick: () => setOpen(false) }, "Sign in"), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn btn-primary",
      style: { justifyContent: "center" },
      onClick: () => {
        scrollTo("install");
        if (window.posthog) posthog.capture("cta_clicked", { location: "nav_mobile", cta: "start_free" });
      }
    },
    "Get started free ",
    /* @__PURE__ */ React.createElement("span", { className: "arr" }, "\u2192")
  ))));
}
function Hero({ layout, interaction }) {
  return /* @__PURE__ */ React.createElement("header", { className: "hero " + (layout === "editorial" ? "editorial" : ""), id: "top" }, /* @__PURE__ */ React.createElement("div", { className: "hero-grid-bg" }), /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "hero-inner" }, /* @__PURE__ */ React.createElement("div", { className: "hero-left" }, /* @__PURE__ */ React.createElement("h1", { className: "display" }, "Your cloud bill,", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("span", { className: "strike" }, "in a dashboard."), /* @__PURE__ */ React.createElement("span", { className: "accent" }, "Waste found.", /* @__PURE__ */ React.createElement("br", null), "Money saved.")), /* @__PURE__ */ React.createElement("p", { className: "lede" }, "Connect AWS, Azure, GCP, and 17 providers to Claude or Cursor. Ask about spend, get rightsizing recommendations, patch your Terraform, open the PR. Runs locally. Your data never leaves your machine."), /* @__PURE__ */ React.createElement("div", { className: "hero-cta-row", id: "install" }, /* @__PURE__ */ React.createElement(CopyInstall, null), /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "/docs.html",
      className: "btn btn-ghost",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "hero", cta: "docs" });
      }
    },
    "Read the docs"
  )), /* @__PURE__ */ React.createElement("div", { className: "hero-mobile-cta" }, /* @__PURE__ */ React.createElement("p", { style: { fontSize: 13, color: "var(--fg-3)", marginBottom: 12, letterSpacing: ".01em" } }, "On mobile? Get the setup guide sent to your inbox."), /* @__PURE__ */ React.createElement(EmailCapture, { source: "hero_mobile", placeholder: "your@email.com", btnLabel: "Send guide" }))), /* @__PURE__ */ React.createElement("div", { className: "hero-right" }, /* @__PURE__ */ React.createElement(Console, { interaction }))), /* @__PURE__ */ React.createElement(TrustStrip, null)));
}
function CopyInstall() {
  const [copied, setCopied] = useState(false);
  const cmd = "pip install finops-mcp && finops welcome";
  return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 8 } }, /* @__PURE__ */ React.createElement("div", { className: "install", role: "group", "aria-label": "Install command" }, /* @__PURE__ */ React.createElement("span", { className: "prompt" }, "$"), /* @__PURE__ */ React.createElement("span", { className: "cmd" }, cmd), /* @__PURE__ */ React.createElement("button", { onClick: () => {
    navigator.clipboard?.writeText(cmd);
    setCopied(true);
    setTimeout(() => setCopied(false), 1600);
    if (window.posthog) posthog.capture("install_copied");
  } }, copied ? "copied" : "copy")), /* @__PURE__ */ React.createElement("p", { className: "mono", style: { fontSize: 11, color: "var(--fg-3)", letterSpacing: ".04em", paddingLeft: 2 } }, "installs the MCP server \xB7 guided setup runs automatically"));
}
function fmtNum(n) {
  if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
  return String(n);
}
function TrustStrip() {
  const [installs, setInstalls] = useState(null);
  useEffect(() => {
    fetch("/api/pypi-stats").then((r) => r.ok ? r.json() : null).then((d) => {
      if (d?.data?.last_month) setInstalls(d.data.last_month);
    }).catch(() => {
    });
  }, []);
  const items = [
    { lab: "installs / mo", val: installs ? fmtNum(installs) : "4k+", sub: "via PyPI \xB7 live" },
    { lab: "providers", val: "17", sub: "AWS \xB7 Azure \xB7 GCP +" },
    { lab: "local only", val: "0 bytes", sub: "sent to our servers" }
  ];
  return /* @__PURE__ */ React.createElement("div", { className: "trust", style: { gridTemplateColumns: "repeat(3,1fr)" } }, items.map((t, i) => /* @__PURE__ */ React.createElement("div", { className: "ti", key: i }, /* @__PURE__ */ React.createElement("span", { className: "lab" }, t.lab), /* @__PURE__ */ React.createElement("span", { className: "val mono" }, t.val, /* @__PURE__ */ React.createElement("span", { className: "sub" }, t.sub)))));
}
const QUERIES = [
  {
    q: "Compute spend across all providers, April vs March.",
    response: /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, "Normalized to USD across the three clouds. Pulled from each provider's billing API just now."), /* @__PURE__ */ React.createElement("div", { className: "ttable" }, /* @__PURE__ */ React.createElement("div", { className: "r hd" }, /* @__PURE__ */ React.createElement("span", null, "Provider \xB7 service"), /* @__PURE__ */ React.createElement("span", null, "April"), /* @__PURE__ */ React.createElement("span", null, "delta MoM")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "AWS \xB7 EC2 + Fargate"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$18,420"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+18.6%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "Azure \xB7 Virtual Machines"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$6,310"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+4.2%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "GCP \xB7 Compute Engine"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$4,870"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "-3.4%")), /* @__PURE__ */ React.createElement("div", { className: "r total" }, /* @__PURE__ */ React.createElement("span", null, "Total compute"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$29,600"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+12.0%"))), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 14 } }, "Three new ", /* @__PURE__ */ React.createElement("span", { className: "mono", style: { color: "var(--fg)" } }, "c6i.4xlarge"), " in ", /* @__PURE__ */ React.createElement("span", { className: "mono", style: { color: "var(--fg)" } }, "us-east-1"), " account for $1,890 of the AWS delta. Want me to tag them and open an audit ticket?"))
  },
  {
    q: "Any anomalies this week?",
    response: /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, /* @__PURE__ */ React.createElement("span", { className: "anomaly" }, "Datadog spike detected."), " Usage is up ", /* @__PURE__ */ React.createElement("span", { style: { color: "var(--alert)" } }, "+127%"), " vs your same-weekday baseline. Z-score 4.8 against the 28-day window."), /* @__PURE__ */ React.createElement("div", { className: "ttable" }, /* @__PURE__ */ React.createElement("div", { className: "r hd" }, /* @__PURE__ */ React.createElement("span", null, "Tag driver"), /* @__PURE__ */ React.createElement("span", null, "Delta"), /* @__PURE__ */ React.createElement("span", null, "% of spike")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "team=platform"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+$2,290"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "78%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "team=infra"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+$480"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "16%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "(untagged)"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+$180"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "6%"))), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 12 } }, "Opened ", /* @__PURE__ */ React.createElement("span", { className: "mono", style: { color: "var(--fg)" } }, "JIRA-2841"), ", paged @sre, posted to ", /* @__PURE__ */ React.createElement("span", { className: "mono", style: { color: "var(--fg)" } }, "#cost-alerts"), ". ", /* @__PURE__ */ React.createElement("span", { style: { color: "var(--accent)" } }, "Drift contained.")))
  },
  {
    q: "Which EC2 instances should we downsize?",
    response: /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, "Cross-referenced CloudWatch metrics with Compute Optimizer. 11 instances are sustained below 15% CPU over 14 days. Top six by savings:"), /* @__PURE__ */ React.createElement("div", { className: "ttable" }, /* @__PURE__ */ React.createElement("div", { className: "r hd" }, /* @__PURE__ */ React.createElement("span", null, "Instance / current"), /* @__PURE__ */ React.createElement("span", null, "Recommended"), /* @__PURE__ */ React.createElement("span", null, "Save / mo")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "i-0a3f \xB7 m5.4xlarge"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "m6i.xlarge"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "$412")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "i-0c91 \xB7 r5.2xlarge"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "r6i.large"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "$298")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "i-0e7d \xB7 m5.2xlarge"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "m6i.large"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "$201")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "i-0b44 \xB7 c5.4xlarge"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "c6i.xlarge"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "$184")), /* @__PURE__ */ React.createElement("div", { className: "r total" }, /* @__PURE__ */ React.createElement("span", null, "11 instances"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "-"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "$1,840 / mo"))), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 12 } }, "Net annualized savings: ", /* @__PURE__ */ React.createElement("span", { style: { color: "var(--accent)" } }, "$22,080"), ". Generate PRs against your IaC repo?"))
  },
  {
    q: "What's our effective discount rate this quarter?",
    response: /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, "Blended across Savings Plans, RIs, and committed-use discounts on GCP. Coverage measured against on-demand list:"), /* @__PURE__ */ React.createElement("div", { className: "ttable" }, /* @__PURE__ */ React.createElement("div", { className: "r hd" }, /* @__PURE__ */ React.createElement("span", null, "Commitment"), /* @__PURE__ */ React.createElement("span", null, "Coverage"), /* @__PURE__ */ React.createElement("span", null, "Effective rate")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "AWS \xB7 Savings Plans (1y)"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "82%"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "-24.1%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "AWS \xB7 RIs (RDS, ElastiCache)"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "71%"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "-31.8%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "GCP \xB7 CUDs (compute)"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "64%"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "-20.4%")), /* @__PURE__ */ React.createElement("div", { className: "r total" }, /* @__PURE__ */ React.createElement("span", null, "Blended effective discount"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "-"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "-26.7%"))), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 12 } }, "You'd unlock another ", /* @__PURE__ */ React.createElement("span", { style: { color: "var(--accent)" } }, "$8,200 / mo"), " by raising Compute SP coverage to 92%. Model it?"))
  }
];
function Console({ interaction }) {
  const [idx, setIdx] = useState(0);
  const [phase, setPhase] = useState("typing");
  const [typed, setTyped] = useState("");
  const timers = useRef([]);
  useEffect(() => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
    setTyped("");
    setPhase("typing");
    const q = QUERIES[idx].q;
    let i = 0;
    function step() {
      if (i <= q.length) {
        setTyped(q.slice(0, i));
        i++;
        timers.current.push(setTimeout(step, 18 + Math.random() * 22));
      } else {
        timers.current.push(setTimeout(() => setPhase("thinking"), 350));
        timers.current.push(setTimeout(() => setPhase("answered"), 1500));
      }
    }
    step();
    return () => timers.current.forEach(clearTimeout);
  }, [idx]);
  useEffect(() => {
    if (interaction !== "cycling") return;
    if (phase !== "answered") return;
    const t = setTimeout(() => setIdx((i) => (i + 1) % QUERIES.length), 6500);
    return () => clearTimeout(t);
  }, [phase, interaction, idx]);
  return /* @__PURE__ */ React.createElement("div", { className: "console", id: "runtime" }, /* @__PURE__ */ React.createElement("div", { className: "console-bar" }, /* @__PURE__ */ React.createElement("div", { className: "dots" }, /* @__PURE__ */ React.createElement("i", null), /* @__PURE__ */ React.createElement("i", null), /* @__PURE__ */ React.createElement("i", null)), /* @__PURE__ */ React.createElement("span", { className: "title" }, "claude \xB7 mcp[nable] \xB7 ~/projects/platform-infra"), /* @__PURE__ */ React.createElement("span", { className: "status" }, "runtime active")), /* @__PURE__ */ React.createElement("div", { className: "console-body" }, /* @__PURE__ */ React.createElement("div", { className: "msg" }, /* @__PURE__ */ React.createElement("div", { className: "av you" }, "you"), /* @__PURE__ */ React.createElement("div", { className: "bubble user" }, /* @__PURE__ */ React.createElement("p", null, typed, /* @__PURE__ */ React.createElement("span", { className: "cursor" })))), phase === "thinking" && /* @__PURE__ */ React.createElement("div", { className: "msg" }, /* @__PURE__ */ React.createElement("div", { className: "av ai" }, "nable"), /* @__PURE__ */ React.createElement("div", { className: "bubble" }, /* @__PURE__ */ React.createElement("div", { className: "thinking" }, /* @__PURE__ */ React.createElement("i", null), /* @__PURE__ */ React.createElement("i", null), /* @__PURE__ */ React.createElement("i", null)))), phase === "answered" && /* @__PURE__ */ React.createElement("div", { className: "msg" }, /* @__PURE__ */ React.createElement("div", { className: "av ai" }, "nable"), /* @__PURE__ */ React.createElement("div", { className: "bubble" }, QUERIES[idx].response))), /* @__PURE__ */ React.createElement("div", { className: "q-pager" }, /* @__PURE__ */ React.createElement("span", null, "query ", String(idx + 1).padStart(2, "0"), " / ", String(QUERIES.length).padStart(2, "0")), /* @__PURE__ */ React.createElement("span", { style: { marginLeft: 14, color: "var(--fg-4)" } }, "\xB7"), /* @__PURE__ */ React.createElement("span", { style: { marginLeft: 14 } }, interaction === "cycling" ? "auto-advancing" : "manual"), /* @__PURE__ */ React.createElement("div", { className: "dots", role: "tablist" }, QUERIES.map((_, i) => /* @__PURE__ */ React.createElement("i", { key: i, className: i === idx ? "on" : "", onClick: () => setIdx(i), role: "tab", "aria-selected": i === idx, tabIndex: 0 })))));
}
function Thesis() {
  const cards = [
    { n: "01 \xB7 TAM", h: "Cloud spend is the #2 line item in modern software.", p: "$700B+ annual cloud + SaaS spend, growing 18% YoY. Every dollar is unaccountable until someone reconciles 8 dashboards and a CSV. That reconciliation work is the wedge." },
    { n: "02 \xB7 Shift", h: "FinOps moved from a quarterly review to a real-time question.", p: 'AI editors made plain-English access to live data the default interface. Asking "what spiked" is now cheaper than building a dashboard. The dashboard era is the legacy era.' },
    { n: "03 \xB7 Moat", h: "Local-first compounds with every connector.", p: "Credentials in the OS keyring. No data lake. No SOC-2 surface area. Each new connector is a feature shipment, not a security review. Enterprise sells itself." }
  ];
  return /* @__PURE__ */ React.createElement("section", { id: "thesis" }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "section-head" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "Thesis"), /* @__PURE__ */ React.createElement("h2", null, "The dashboard ", /* @__PURE__ */ React.createElement("em", null, "was"), " the product.", /* @__PURE__ */ React.createElement("br", null), "The interface is the product now."), /* @__PURE__ */ React.createElement("p", null, "Three forces converge in 2026. nable is the runtime where they meet.")), /* @__PURE__ */ React.createElement("div", { className: "thesis" }, cards.map((c, i) => /* @__PURE__ */ React.createElement("div", { className: "thesis-card", key: i }, /* @__PURE__ */ React.createElement("span", { className: "n" }, c.n), /* @__PURE__ */ React.createElement("h3", null, c.h), /* @__PURE__ */ React.createElement("p", null, c.p))))));
}
function Depth() {
  const cards = [
    {
      n: "01",
      h: "Rightsizing that closes the loop",
      p: "Cross-references CloudWatch, Compute Optimizer, and 14 days of CPU/memory data. Then reads your Terraform state, finds the resource, patches the instance type in the .tf file, and opens the PR. After you merge and apply, nable checks AWS to confirm the change and records the realized saving.",
      chips: ["CloudWatch", "Compute Optimizer", "Terraform state", "PR + verified savings"]
    },
    {
      n: "02",
      h: "Anomaly detection with attribution",
      p: "Z-score detection against a 28-day rolling baseline. When something spikes, nable doesn't just tell you the number. It breaks the spike down by tag, team, and service, then pages whoever owns it. False positive rate is near zero because it uses your own baseline.",
      chips: ["Z-score", "28-day baseline", "tag attribution", "Slack/PagerDuty alert"]
    },
    {
      n: "03",
      h: "Commitment analysis",
      p: "Models your Savings Plan and Reserved Instance coverage gap across AWS, Azure, and GCP. Tells you exactly what buying more coverage would save by service and term, based on your actual usage patterns, not list-price estimates.",
      chips: ["Savings Plans", "Reserved Instances", "GCP CUDs", "coverage modeling"]
    },
    {
      n: "04",
      h: "Multi-provider, one conversation",
      p: "17 billing APIs normalized into a single query layer. Ask about total compute spend across AWS and Azure in the same question. Compare Datadog costs against observability budget. No switching tabs, no exporting CSVs between systems.",
      chips: ["17 providers", "cross-cloud", "SaaS included", "normalized spend"]
    }
  ];
  return /* @__PURE__ */ React.createElement("section", { id: "depth", style: { borderTop: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "section-head" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "What's under the hood"), /* @__PURE__ */ React.createElement("h2", null, "Not a pipe.", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "An analyst.")), /* @__PURE__ */ React.createElement("p", null, "The value isn't connecting Claude to your bill. It's the analysis that runs before Claude ever responds.")), /* @__PURE__ */ React.createElement("div", { className: "depth-grid" }, cards.map((c, i) => /* @__PURE__ */ React.createElement("div", { className: "depth-card", key: i }, /* @__PURE__ */ React.createElement("span", { className: "depth-n" }, c.n), /* @__PURE__ */ React.createElement("h3", { className: "depth-h" }, c.h), /* @__PURE__ */ React.createElement("p", { className: "depth-p" }, c.p), /* @__PURE__ */ React.createElement("div", { className: "depth-chips" }, c.chips.map((ch, j) => /* @__PURE__ */ React.createElement("span", { key: j }, ch))))))));
}
function Architecture({ version }) {
  return /* @__PURE__ */ React.createElement("section", { id: "arch", className: "alt" }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "section-head" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "Architecture"), /* @__PURE__ */ React.createElement("h2", null, "Headless by design.", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "Your data never moves.")), /* @__PURE__ */ React.createElement("p", null, "nable is not SaaS. It runs on the engineer's machine, holds credentials in the OS keyring, queries provider APIs directly, and surfaces tools to whichever AI editor is open. We never see your bill.")), /* @__PURE__ */ React.createElement("div", { className: "arch" }, /* @__PURE__ */ React.createElement("div", { className: "arch-grid" }), /* @__PURE__ */ React.createElement("div", { className: "arch-row" }, /* @__PURE__ */ React.createElement("div", { className: "arch-col" }, /* @__PURE__ */ React.createElement("span", { className: "lab" }, "your editor"), /* @__PURE__ */ React.createElement("div", { className: "arch-node" }, /* @__PURE__ */ React.createElement("h4", null, "Claude \xB7 Cursor \xB7 Zed"), /* @__PURE__ */ React.createElement("span", { className: "sub" }, "MCP client"), /* @__PURE__ */ React.createElement("div", { className: "chips" }, /* @__PURE__ */ React.createElement("span", null, "tools/list"), /* @__PURE__ */ React.createElement("span", null, "tools/call")))), /* @__PURE__ */ React.createElement("div", { className: "arch-arrow" }, /* @__PURE__ */ React.createElement("span", null, "stdio"), /* @__PURE__ */ React.createElement("span", { className: "line" }), /* @__PURE__ */ React.createElement("span", null, "jsonrpc")), /* @__PURE__ */ React.createElement("div", { className: "arch-col" }, /* @__PURE__ */ React.createElement("span", { className: "lab" }, "runtime \xB7 local"), /* @__PURE__ */ React.createElement("div", { className: "arch-node center" }, /* @__PURE__ */ React.createElement("h4", null, "nable runtime"), /* @__PURE__ */ React.createElement("span", { className: "sub" }, "finops-mcp / ", version || "0.8.36"), /* @__PURE__ */ React.createElement("div", { className: "chips" }, /* @__PURE__ */ React.createElement("span", null, "keyring"), /* @__PURE__ */ React.createElement("span", null, "fernet"), /* @__PURE__ */ React.createElement("span", null, "read-only"), /* @__PURE__ */ React.createElement("span", null, "audit-log")))), /* @__PURE__ */ React.createElement("div", { className: "arch-arrow" }, /* @__PURE__ */ React.createElement("span", null, "https"), /* @__PURE__ */ React.createElement("span", { className: "line" }), /* @__PURE__ */ React.createElement("span", null, "signed")), /* @__PURE__ */ React.createElement("div", { className: "arch-col" }, /* @__PURE__ */ React.createElement("span", { className: "lab" }, "provider apis"), /* @__PURE__ */ React.createElement("div", { className: "arch-node" }, /* @__PURE__ */ React.createElement("h4", null, "17 connectors"), /* @__PURE__ */ React.createElement("span", { className: "sub" }, "cost \xB7 usage \xB7 billing"), /* @__PURE__ */ React.createElement("div", { className: "chips" }, /* @__PURE__ */ React.createElement("span", null, "AWS CE/CUR"), /* @__PURE__ */ React.createElement("span", null, "Azure CM"), /* @__PURE__ */ React.createElement("span", null, "GCP BQ"), /* @__PURE__ */ React.createElement("span", null, "+14"))))))));
}
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
  "Find idle NAT Gateways and tag the owners."
];
function QMarquee() {
  return /* @__PURE__ */ React.createElement("section", { className: "tight", style: { padding: "0", borderTop: "none" } }, /* @__PURE__ */ React.createElement("div", { className: "qmarq" }, /* @__PURE__ */ React.createElement("div", { className: "track" }, [...QUESTIONS, ...QUESTIONS].map((q, i) => /* @__PURE__ */ React.createElement("span", { className: "q", key: i }, q)))));
}
const CONNECTORS = [
  { nm: "AWS", px: "Cost Explorer \xB7 CUR via S3", tag: "live" },
  { nm: "Azure", px: "Cost Management API", tag: "live" },
  { nm: "GCP", px: "Cloud Billing \xB7 BigQuery", tag: "live" },
  { nm: "Datadog", px: "Usage Metering v2", tag: "live" },
  { nm: "Snowflake", px: "ACCOUNT_USAGE.METERING", tag: "live" },
  { nm: "Langfuse", px: "Daily metrics \xB7 cost / token", tag: "live" },
  { nm: "MongoDB", px: "Atlas Invoice API", tag: "live" },
  { nm: "Twilio", px: "Usage Records API", tag: "live" },
  { nm: "Cloudflare", px: "Billing API", tag: "live" },
  { nm: "GitHub", px: "Actions mins \xB7 Copilot seats", tag: "live" },
  { nm: "Vercel", px: "Invoice API \xB7 enterprise", tag: "live" },
  { nm: "New Relic", px: "Data ingest \xB7 user counts", tag: "live" },
  { nm: "Linear", px: "Seat plan \xB7 usage rollup", tag: "live" },
  { nm: "OpenAI", px: "Usage API \xB7 per-model spend", tag: "live" },
  { nm: "Anthropic", px: "Org usage \xB7 per-model spend", tag: "live" },
  { nm: "Stripe", px: "Billing meter \xB7 platform fees", tag: "beta" },
  { nm: "PagerDuty", px: "License spend \xB7 on-call costs", tag: "beta" },
  { nm: "Coming soon", px: "Vote on the next connector", tag: "soon" }
];
function Connectors() {
  return /* @__PURE__ */ React.createElement("section", { id: "connectors", className: "alt" }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "section-head" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "Connectors"), /* @__PURE__ */ React.createElement("h2", null, "17 sources.", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "One conversation.")), /* @__PURE__ */ React.createElement("p", null, "Every connector is a real API integration, not a CSV export. New providers ship monthly.")), /* @__PURE__ */ React.createElement("div", { className: "conn-grid" }, CONNECTORS.map((c, i) => /* @__PURE__ */ React.createElement("div", { className: "conn", key: i }, /* @__PURE__ */ React.createElement("span", { className: "nm" }, c.nm), /* @__PURE__ */ React.createElement("span", { className: "px" }, c.px), /* @__PURE__ */ React.createElement("span", { className: "tag " + (c.tag === "beta" ? "beta" : c.tag === "soon" ? "soon" : "") }, c.tag))))));
}
function Telemetry() {
  const pts = [12, 18, 14, 22, 28, 24, 32, 30, 38, 42, 36, 48, 52, 58];
  const path = pts.map((p, i) => `${i === 0 ? "M" : "L"} ${i * (100 / (pts.length - 1))} ${60 - p}`).join(" ");
  return /* @__PURE__ */ React.createElement("section", { id: "telemetry", className: "tight" }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "section-head" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "By the numbers"), /* @__PURE__ */ React.createElement("h2", null, "Adoption signal.", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "Live.")), /* @__PURE__ */ React.createElement("p", null, "Pulled from PyPI, Stripe, and our telemetry endpoint, refreshed nightly. No marketing math.")), /* @__PURE__ */ React.createElement("div", { className: "bento" }, /* @__PURE__ */ React.createElement("div", { className: "bento-cell tall" }, /* @__PURE__ */ React.createElement("span", { className: "lab" }, "monthly installs \xB7 pypi"), /* @__PURE__ */ React.createElement("span", { className: "big mono" }, "4,127", /* @__PURE__ */ React.createElement("span", { className: "delta" }, "+38% WoW")), /* @__PURE__ */ React.createElement("p", null, "Trajectory consistent with bottom-up dev-tool growth. 67% of paid trials originate from a prior unpaid install."), /* @__PURE__ */ React.createElement("div", { className: "sparkline" }, /* @__PURE__ */ React.createElement("svg", { viewBox: "0 0 100 60", preserveAspectRatio: "none" }, /* @__PURE__ */ React.createElement("defs", null, /* @__PURE__ */ React.createElement("linearGradient", { id: "g1", x1: "0", y1: "0", x2: "0", y2: "1" }, /* @__PURE__ */ React.createElement("stop", { offset: "0%", stopColor: "var(--accent)", stopOpacity: ".25" }), /* @__PURE__ */ React.createElement("stop", { offset: "100%", stopColor: "var(--accent)", stopOpacity: "0" }))), /* @__PURE__ */ React.createElement("path", { d: path + ` L 100 60 L 0 60 Z`, fill: "url(#g1)" }), /* @__PURE__ */ React.createElement("path", { d: path, fill: "none", stroke: "var(--accent)", strokeWidth: "1.5", vectorEffect: "non-scaling-stroke" }), pts.map((p, i) => /* @__PURE__ */ React.createElement("circle", { key: i, cx: i * (100 / (pts.length - 1)), cy: 60 - p, r: "1.2", fill: "var(--accent)" }))))), /* @__PURE__ */ React.createElement("div", { className: "bento-cell" }, /* @__PURE__ */ React.createElement("span", { className: "lab" }, "paid conversion"), /* @__PURE__ */ React.createElement("span", { className: "big mono" }, "14.2%"), /* @__PURE__ */ React.createElement("p", null, "Installs to Team plan within 30 days.")), /* @__PURE__ */ React.createElement("div", { className: "bento-cell span-end" }, /* @__PURE__ */ React.createElement("span", { className: "lab" }, "net retention"), /* @__PURE__ */ React.createElement("span", { className: "big mono" }, "132%", /* @__PURE__ */ React.createElement("span", { className: "delta" }, "trailing 6mo")), /* @__PURE__ */ React.createElement("p", null, "Driven by multi-account / multi-cloud expansion.")), /* @__PURE__ */ React.createElement("div", { className: "bento-cell row-end" }, /* @__PURE__ */ React.createElement("span", { className: "lab" }, "median savings \xB7 first 60 days"), /* @__PURE__ */ React.createElement("span", { className: "big mono" }, "$3,840", /* @__PURE__ */ React.createElement("span", { className: "delta" }, "/ mo")), /* @__PURE__ */ React.createElement("p", null, "Across teams who shipped at least one rightsizing recommendation.")), /* @__PURE__ */ React.createElement("div", { className: "bento-cell row-end span-end" }, /* @__PURE__ */ React.createElement("span", { className: "lab" }, "ttfa \xB7 time to first answer"), /* @__PURE__ */ React.createElement("span", { className: "big mono" }, "4 min", /* @__PURE__ */ React.createElement("span", { className: "delta" }, "install to insight")), /* @__PURE__ */ React.createElement("p", null, "Wizard auto-configures the MCP client. Median end-to-end.")))));
}
const SOLO_FEATURES = [
  "Cost queries across all providers",
  "Anomaly detection",
  "Rightsizing recommendations",
  "All 17 connectors",
  "Local only \u2014 no data leaves your machine",
  "Works in Claude, Cursor, Windsurf, Zed"
];
const TEAM_FEATURES = [
  "Everything in Solo",
  "Terraform remediation: patch files, open PR",
  "Scheduled cost digests via email",
  "Commitment analysis and RI recommendations",
  "Org-level rollups across accounts",
  "Budget enforcement and alerts",
  "Ticket creation (Jira, Linear, GitHub Issues)",
  "RBAC for team access control"
];
function CheckIcon() {
  return /* @__PURE__ */ React.createElement("svg", { width: "15", height: "15", viewBox: "0 0 15 15", fill: "none", "aria-hidden": "true", style: { flexShrink: 0, marginTop: 1 } }, /* @__PURE__ */ React.createElement("circle", { cx: "7.5", cy: "7.5", r: "7", stroke: "currentColor", strokeWidth: "1" }), /* @__PURE__ */ React.createElement("path", { d: "M4.5 7.5L6.5 9.5L10.5 5.5", stroke: "currentColor", strokeWidth: "1.4", strokeLinecap: "round", strokeLinejoin: "round" }));
}
function Pricing() {
  return /* @__PURE__ */ React.createElement("section", { id: "pricing" }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "section-head" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "Pricing"), /* @__PURE__ */ React.createElement("h2", null, "Free to ask.", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "Pay to remediate.")), /* @__PURE__ */ React.createElement("p", null, "Solo is free forever. Team adds the remediation layer: Terraform PRs, digests, budget enforcement, and org rollups.")), /* @__PURE__ */ React.createElement("div", { className: "pricing-grid" }, /* @__PURE__ */ React.createElement("div", { className: "pricing-card" }, /* @__PURE__ */ React.createElement("div", { className: "pricing-top" }, /* @__PURE__ */ React.createElement("div", { className: "pricing-name" }, "Solo"), /* @__PURE__ */ React.createElement("div", { className: "pricing-price" }, /* @__PURE__ */ React.createElement("span", { className: "pricing-amount" }, "Free"), /* @__PURE__ */ React.createElement("span", { className: "pricing-per" }, "forever")), /* @__PURE__ */ React.createElement("p", { className: "pricing-desc" }, "Everything you need to query, investigate, and understand your cloud costs."), /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn btn-ghost pricing-cta",
      onClick: () => {
        document.getElementById("install")?.scrollIntoView({ behavior: "smooth" });
        if (window.posthog) posthog.capture("cta_clicked", { location: "pricing", plan: "solo" });
      }
    },
    "Get started free ",
    /* @__PURE__ */ React.createElement("span", { className: "arr" }, "\u2192")
  )), /* @__PURE__ */ React.createElement("div", { className: "pricing-features" }, SOLO_FEATURES.map((f, i) => /* @__PURE__ */ React.createElement("div", { key: i, className: "pricing-feature" }, /* @__PURE__ */ React.createElement(CheckIcon, null), /* @__PURE__ */ React.createElement("span", null, f))))), /* @__PURE__ */ React.createElement("div", { className: "pricing-card featured" }, /* @__PURE__ */ React.createElement("div", { className: "pricing-badge" }, "7-day free trial"), /* @__PURE__ */ React.createElement("div", { className: "pricing-top" }, /* @__PURE__ */ React.createElement("div", { className: "pricing-name" }, "Team"), /* @__PURE__ */ React.createElement("div", { className: "pricing-price" }, /* @__PURE__ */ React.createElement("span", { className: "pricing-amount" }, "$40"), /* @__PURE__ */ React.createElement("span", { className: "pricing-per" }, "/ mo")), /* @__PURE__ */ React.createElement("p", { className: "pricing-desc" }, "The remediation layer. Finds the waste, writes the fix, opens the PR, tracks whether it actually shipped."), /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "https://buy.stripe.com/3cIcN41Dz9Vk9JCd7c2Nq01",
      target: "_blank",
      rel: "noopener noreferrer",
      className: "btn btn-primary pricing-cta",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "pricing", plan: "team" });
      }
    },
    "Start free trial ",
    /* @__PURE__ */ React.createElement("span", { className: "arr" }, "\u2192")
  )), /* @__PURE__ */ React.createElement("div", { className: "pricing-features" }, TEAM_FEATURES.map((f, i) => /* @__PURE__ */ React.createElement("div", { key: i, className: "pricing-feature" }, /* @__PURE__ */ React.createElement(CheckIcon, null), /* @__PURE__ */ React.createElement("span", null, f)))))), /* @__PURE__ */ React.createElement("p", { className: "mono", style: { marginTop: 32, fontSize: 12, color: "var(--fg-4)", textAlign: "center", letterSpacing: ".04em" } }, "No credit card for Solo. Team trial requires a card, cancel any time.")));
}
function MidCta() {
  return /* @__PURE__ */ React.createElement("section", { id: "mid-cta", style: { borderTop: "1px solid var(--line)", borderBottom: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("div", { className: "wrap", style: { paddingTop: 72, paddingBottom: 72 } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", alignItems: "center", gap: 24, textAlign: "center" } }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h2", { style: { marginBottom: 10 } }, "Ready to stop guessing?"), /* @__PURE__ */ React.createElement("p", { style: { color: "var(--fg-2)", maxWidth: "46ch", margin: "0 auto", lineHeight: 1.6 } }, "Five minutes from install to your first real insight. Free forever for solo use.")), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", justifyContent: "center" } }, /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn btn-primary",
      onClick: () => {
        document.getElementById("install")?.scrollIntoView({ behavior: "smooth" });
        if (window.posthog) posthog.capture("cta_clicked", { location: "mid_cta", cta: "start_free" });
      }
    },
    "Get started free ",
    /* @__PURE__ */ React.createElement("span", { className: "arr" }, "\u2192")
  ), /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "/docs.html",
      className: "btn btn-ghost",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "mid_cta", cta: "docs" });
      }
    },
    "Read the docs"
  )), /* @__PURE__ */ React.createElement("p", { className: "mono", style: { fontSize: 11, color: "var(--fg-4)", letterSpacing: ".05em" } }, "pip install finops-mcp && finops welcome"))));
}
function FootCta() {
  return /* @__PURE__ */ React.createElement("section", { className: "foot-cta", id: "cta" }, /* @__PURE__ */ React.createElement("div", { className: "foot-cta-grid" }), /* @__PURE__ */ React.createElement("div", { className: "wrap", style: { position: "relative" } }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow", style: { marginBottom: 32, display: "inline-flex" } }, /* @__PURE__ */ React.createElement("span", { className: "d" }), " Free tier \xB7 no credit card"), /* @__PURE__ */ React.createElement("h2", { className: "display" }, "Stop staring at graphs.", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "Start closing tickets.")), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 48, display: "flex", flexDirection: "column", alignItems: "center", gap: 16 } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 14 } }, /* @__PURE__ */ React.createElement(
    "button",
    {
      className: "btn btn-primary",
      style: { padding: "14px 22px", fontSize: 14 },
      onClick: () => {
        document.getElementById("install")?.scrollIntoView({ behavior: "smooth" });
        if (window.posthog) posthog.capture("cta_clicked", { location: "footer_cta", cta: "install" });
      }
    },
    "Get started free ",
    /* @__PURE__ */ React.createElement("span", { className: "arr" }, "\u2192")
  ), /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "mailto:chandan@getnable.com?subject=nable - talk to founders",
      target: "_blank",
      rel: "noopener noreferrer",
      className: "btn btn-ghost",
      style: { padding: "14px 22px", fontSize: 14 },
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "footer_cta", cta: "talk_to_founders" });
      }
    },
    "Talk to founders"
  )), /* @__PURE__ */ React.createElement(EmailCapture, { source: "footer", placeholder: "drop your email, we'll send the setup guide", btnLabel: "Send it", center: true })), /* @__PURE__ */ React.createElement("p", { className: "mono", style: { marginTop: 32, fontSize: 12, color: "var(--fg-3)", letterSpacing: ".04em" } }, "$ pip install finops-mcp && finops welcome"), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 24, fontSize: 13, color: "var(--fg-3)" } }, "Building something? ", /* @__PURE__ */ React.createElement("a", { href: "/about", style: { color: "var(--accent-dim)" } }, "Read the founder note and investor thesis \u2192"))));
}
function FounderNote() {
  return /* @__PURE__ */ React.createElement("section", { id: "founder", style: { borderTop: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("div", { className: "wrap", style: { maxWidth: 680, paddingTop: 80, paddingBottom: 80 } }, /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "'Instrument Sans',system-ui,sans-serif", fontWeight: 500, fontSize: 11, color: "var(--accent-dim)", letterSpacing: ".08em", textTransform: "uppercase", display: "flex", alignItems: "center", gap: 10, marginBottom: 24 } }, /* @__PURE__ */ React.createElement("span", { style: { width: 24, height: 1, background: "var(--accent-dim)", display: "inline-block" } }), "Why I built this"), /* @__PURE__ */ React.createElement("p", { style: { fontSize: 17, lineHeight: 1.75, color: "var(--fg-2)", marginBottom: 28 } }, "I built this because I spent most of my day bouncing between dashboards that barely showed what I actually needed, the AWS console, and Claude. I'd ask Claude a question, manually paste in numbers, get an answer, then go back and repeat the whole thing."), /* @__PURE__ */ React.createElement("p", { style: { fontSize: 17, lineHeight: 1.75, color: "var(--fg-2)", marginBottom: 28 } }, "A lot of FinOps tools are shipping MCP integrations now. But they're all built for enterprise, priced for enterprise, and none of them fit the way I actually work. They give you visibility. They don't help you think."), /* @__PURE__ */ React.createElement("p", { style: { fontSize: 17, lineHeight: 1.75, color: "var(--fg-2)", marginBottom: 36 } }, "nable solves the problems I actually had. The recommendations go deeper than anything I've seen out of the box, and for the first time I can actually reason through my own optimization opportunities instead of just staring at a graph."), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 14 } }, /* @__PURE__ */ React.createElement("div", { style: { width: 40, height: 40, borderRadius: "50%", background: "var(--accent)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 } }, /* @__PURE__ */ React.createElement("span", { style: { fontFamily: "var(--mono)", fontSize: 13, fontWeight: 600, color: "var(--bg)" } }, "CB")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { style: { fontSize: 14, fontWeight: 500, color: "var(--fg)" } }, "Chandan Bukkapatnam"), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 13, color: "var(--fg-3)" } }, "Founder \xB7 ", /* @__PURE__ */ React.createElement("a", { href: "mailto:chandan@getnable.com", target: "_blank", rel: "noopener noreferrer", style: { color: "var(--accent)" } }, "chandan@getnable.com"))))));
}
function Footer({ version }) {
  return /* @__PURE__ */ React.createElement("footer", null, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "foot" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("a", { href: "#top", className: "logo", style: { marginBottom: 18 } }, /* @__PURE__ */ React.createElement(LogoMark, null), /* @__PURE__ */ React.createElement("span", null, "nable")), /* @__PURE__ */ React.createElement("p", { style: { color: "var(--fg-3)", fontSize: 13, maxWidth: "34ch", lineHeight: 1.55, marginTop: 10 } }, "Your cloud bill, in your editor. Made in Austin, TX.")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h5", null, "Product"), /* @__PURE__ */ React.createElement("a", { href: "#connectors" }, "Connectors"), /* @__PURE__ */ React.createElement("a", { href: "#pricing" }, "Pricing"), /* @__PURE__ */ React.createElement("a", { href: "#faq" }, "FAQ")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h5", null, "Resources"), /* @__PURE__ */ React.createElement("a", { href: "/docs.html" }, "Docs"), /* @__PURE__ */ React.createElement("a", { href: "/docs.html#quickstart" }, "Quickstart"), /* @__PURE__ */ React.createElement("a", { href: "/docs.html#iam" }, "IAM templates"), /* @__PURE__ */ React.createElement("a", { href: "/docs.html#security" }, "Security brief")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h5", null, "Company"), /* @__PURE__ */ React.createElement("a", { href: "/about" }, "About"), /* @__PURE__ */ React.createElement("a", { href: "/about#investors" }, "Investors"), /* @__PURE__ */ React.createElement("a", { href: "mailto:hello@getnable.com", target: "_blank", rel: "noopener noreferrer" }, "Contact"), /* @__PURE__ */ React.createElement("a", { href: "https://github.com/chaandannn/finopsmcp", target: "_blank", rel: "noopener noreferrer" }, "GitHub"))), /* @__PURE__ */ React.createElement("div", { className: "foot-meta" }, /* @__PURE__ */ React.createElement("span", null, "2026 nable, inc. \xB7 all rights reserved"), /* @__PURE__ */ React.createElement("span", null, "finops-mcp / ", version || "0.8.36", " \xB7 runtime healthy"))));
}
const FAQ_ITEMS = [
  {
    q: "How is this different from just asking Claude?",
    a: "Without nable, you copy numbers from dashboards and paste them into Claude. That works for simple questions. But Claude won't know to cross-reference CloudWatch metrics against Compute Optimizer, run Z-score detection against a 28-day baseline, model your Savings Plan coverage gap, or read your Terraform state to find which resource needs changing. nable ships all of that analysis pre-built. When it surfaces a rightsizing rec, it goes further: reads your Terraform state, patches the .tf file, and opens the PR. The finding and the fix happen in the same conversation."
  },
  {
    q: "Where do my credentials and billing data go?",
    a: "Nowhere. nable runs entirely on your machine. Credentials are stored in your OS keyring (macOS Keychain, Windows Credential Manager, or libsecret on Linux). Billing data is queried directly from provider APIs. We never see it."
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
  }
];
function FAQ() {
  const [open, setOpen] = useState(null);
  return /* @__PURE__ */ React.createElement("section", { id: "faq", className: "alt", style: { borderTop: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("div", { className: "wrap", style: { maxWidth: 720, paddingTop: 80, paddingBottom: 80 } }, /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "'Instrument Sans',system-ui,sans-serif", fontWeight: 500, fontSize: 11, color: "var(--accent-dim)", letterSpacing: ".08em", textTransform: "uppercase", display: "flex", alignItems: "center", gap: 10, marginBottom: 18 } }, /* @__PURE__ */ React.createElement("span", { style: { width: 24, height: 1, background: "var(--accent-dim)", display: "inline-block" } }), "FAQ"), /* @__PURE__ */ React.createElement("h2", { style: { marginBottom: 48 } }, "Questions we actually get."), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column" } }, FAQ_ITEMS.map((item, i) => {
    const isOpen = open === i;
    return /* @__PURE__ */ React.createElement("div", { key: i, style: {
      borderBottom: "1px solid var(--line)"
    } }, /* @__PURE__ */ React.createElement(
      "button",
      {
        onClick: () => setOpen(isOpen ? null : i),
        style: {
          width: "100%",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          padding: "20px 0",
          background: "none",
          border: "none",
          color: "var(--fg)",
          fontFamily: "'Instrument Sans',system-ui,sans-serif",
          fontSize: 16,
          fontWeight: 500,
          textAlign: "left",
          cursor: "pointer",
          gap: 16
        },
        "aria-expanded": isOpen
      },
      /* @__PURE__ */ React.createElement("span", null, item.q),
      /* @__PURE__ */ React.createElement("span", { style: {
        flexShrink: 0,
        width: 22,
        height: 22,
        borderRadius: "50%",
        border: "1px solid var(--line-2)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        color: "var(--fg-3)",
        fontSize: 16,
        transition: "transform .2s",
        transform: isOpen ? "rotate(45deg)" : "none"
      } }, "+")
    ), isOpen && /* @__PURE__ */ React.createElement("p", { style: {
      fontSize: 15,
      lineHeight: 1.7,
      color: "var(--fg-2)",
      paddingBottom: 20,
      margin: 0
    } }, item.a));
  })), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 48, display: "flex", alignItems: "center", gap: 12 } }, /* @__PURE__ */ React.createElement("span", { style: { fontSize: 14, color: "var(--fg-3)" } }, "Still have questions?"), /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "mailto:hello@getnable.com?subject=nable%20question",
      target: "_blank",
      rel: "noopener noreferrer",
      style: { fontSize: 14, color: "var(--accent)", textDecoration: "none", fontWeight: 500 }
    },
    "Email us directly \u2192"
  ))));
}
const PALETTE_OPTIONS = [
  { value: "onyx", label: "Onyx", swatch: ["#0a0a0c", "#5fe8a0", "#15151a"] },
  { value: "graphite", label: "Graphite", swatch: ["#0d0f10", "#4db8d4", "#181c1f"] },
  { value: "paper", label: "Paper", swatch: ["#fbfaf7", "#1f8a5b", "#e3dfcf"] },
  { value: "mono", label: "Mono", swatch: ["#ffffff", "#0a0a0a", "#e6e6e3"] }
];
function PaletteSwatches({ value, onChange }) {
  return /* @__PURE__ */ React.createElement("div", { style: { display: "grid", gridTemplateColumns: "repeat(2,1fr)", gap: 8, marginTop: 6 } }, PALETTE_OPTIONS.map((o) => {
    const on = o.value === value;
    return /* @__PURE__ */ React.createElement(
      "button",
      {
        key: o.value,
        type: "button",
        onClick: () => onChange(o.value),
        style: {
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "7px 9px",
          border: "1px solid",
          borderColor: on ? "var(--accent)" : "rgba(255,255,255,.12)",
          borderRadius: 7,
          background: "rgba(255,255,255,.03)",
          color: "var(--fg)",
          fontFamily: "'DM Sans',sans-serif",
          fontSize: 12,
          cursor: "pointer",
          boxShadow: on ? "0 0 0 2px rgba(95,232,160,.18)" : "none",
          transition: ".15s"
        }
      },
      /* @__PURE__ */ React.createElement("span", { style: { display: "flex", borderRadius: 4, overflow: "hidden", border: "1px solid rgba(255,255,255,.08)", flexShrink: 0 } }, o.swatch.map((c, i) => /* @__PURE__ */ React.createElement("span", { key: i, style: { width: 10, height: 18, background: c, display: "block" } }))),
      /* @__PURE__ */ React.createElement("span", null, o.label)
    );
  }));
}
function Tweaks() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  useEffect(() => {
    applyPalette(t.palette);
  }, [t.palette]);
  useEffect(() => {
    window.dispatchEvent(new CustomEvent("nable:tweaks", { detail: t }));
  }, [t]);
  return /* @__PURE__ */ React.createElement(TweaksPanel, { title: "Tweaks" }, /* @__PURE__ */ React.createElement(TweakSection, { label: "Theme" }, /* @__PURE__ */ React.createElement(PaletteSwatches, { value: t.palette, onChange: (v) => setTweak("palette", v) })), /* @__PURE__ */ React.createElement(TweakSection, { label: "Layout" }, /* @__PURE__ */ React.createElement(
    TweakRadio,
    {
      label: "Hero arrangement",
      value: t.layout,
      options: [{ value: "split", label: "Split" }, { value: "editorial", label: "Editorial" }],
      onChange: (v) => setTweak("layout", v)
    }
  )), /* @__PURE__ */ React.createElement(TweakSection, { label: "Interaction" }, /* @__PURE__ */ React.createElement(
    TweakRadio,
    {
      label: "Console queries",
      value: t.interaction,
      options: [{ value: "cycling", label: "Auto" }, { value: "static", label: "Manual" }],
      onChange: (v) => setTweak("interaction", v)
    }
  )));
}
function App() {
  const [t, setT] = useState(TWEAK_DEFAULTS);
  const [version, setVersion] = useState(null);
  useScrollTracking();
  useEffect(() => {
    applyPalette(t.palette);
    function onTweaks(e) {
      setT(e.detail);
    }
    window.addEventListener("nable:tweaks", onTweaks);
    return () => window.removeEventListener("nable:tweaks", onTweaks);
  }, []);
  useEffect(() => {
    fetch("/api/pypi-version").then((r) => r.ok ? r.json() : null).then((d) => {
      if (d?.version) setVersion(d.version);
    }).catch(() => {
    });
  }, []);
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(Nav, null), /* @__PURE__ */ React.createElement(Hero, { layout: t.layout, interaction: t.interaction }), /* @__PURE__ */ React.createElement(Connectors, null), /* @__PURE__ */ React.createElement(Depth, null), /* @__PURE__ */ React.createElement(Architecture, { version }), /* @__PURE__ */ React.createElement(Pricing, null), /* @__PURE__ */ React.createElement(FAQ, null), /* @__PURE__ */ React.createElement(FootCta, null), /* @__PURE__ */ React.createElement(Footer, { version }), /* @__PURE__ */ React.createElement(Tweaks, null));
}
ReactDOM.createRoot(document.getElementById("app")).render(/* @__PURE__ */ React.createElement(App, null));
