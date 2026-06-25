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
    "--bg": "#000000",
    "--bg-1": "#0a0a0c",
    "--bg-2": "#121214",
    "--bg-3": "#1a1a1d",
    "--line": "#232327",
    "--line-2": "#2d2d32",
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
    const sections = ["demo", "connectors", "architecture", "pricing", "foot-cta"];
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
function LogoMark() {
  return /* @__PURE__ */ React.createElement("svg", { width: "26", height: "26", viewBox: "0 0 120 120", className: "mark-img", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("defs", null, /* @__PURE__ */ React.createElement("linearGradient", { id: "nmg", x1: "0", y1: "0", x2: "0", y2: "1" }, /* @__PURE__ */ React.createElement("stop", { offset: "0", stopColor: "#5cc1da" }), /* @__PURE__ */ React.createElement("stop", { offset: "1", stopColor: "#3a9ab6" }))), /* @__PURE__ */ React.createElement("rect", { width: "120", height: "120", rx: "27", fill: "url(#nmg)" }), /* @__PURE__ */ React.createElement("path", { d: "M44 80 L44 56 A16 16 0 0 1 76 56 L76 80", fill: "none", stroke: "#000000", strokeWidth: "13", strokeLinecap: "round", strokeLinejoin: "round" }));
}
function Ticker({ installs, version }) {
  return /* @__PURE__ */ React.createElement("div", { className: "ticker" }, /* @__PURE__ */ React.createElement("div", { className: "ticker-inner" }, /* @__PURE__ */ React.createElement("span", { className: "seg" }, /* @__PURE__ */ React.createElement("span", { className: "dot" }), /* @__PURE__ */ React.createElement("b", null, "nable"), /* @__PURE__ */ React.createElement("span", null, "runtime healthy")), /* @__PURE__ */ React.createElement("span", { className: "sep" }, "\xB7"), /* @__PURE__ */ React.createElement("span", { className: "seg" }, "4k+ PyPI downloads / mo"), /* @__PURE__ */ React.createElement("span", { className: "sep" }, "\xB7"), /* @__PURE__ */ React.createElement("span", { className: "seg" }, "17 connectors \xB7 AWS \xB7 Azure \xB7 GCP +14"), /* @__PURE__ */ React.createElement("span", { className: "sep" }, "\xB7"), /* @__PURE__ */ React.createElement("span", { className: "seg" }, /* @__PURE__ */ React.createElement("a", { href: "/about", style: { color: "var(--accent)", textDecoration: "none", fontWeight: 500 } }, "About & investors \u2192"))));
}
function Nav() {
  const [open, setOpen] = useState(false);
  function scrollTo(id) {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth" });
    setOpen(false);
  }
  return /* @__PURE__ */ React.createElement("nav", { className: "nav" }, /* @__PURE__ */ React.createElement("div", { className: "nav-inner" }, /* @__PURE__ */ React.createElement("a", { href: "/", className: "logo" }, /* @__PURE__ */ React.createElement(LogoMark, null), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("span", { style: { color: "var(--accent)" } }, "n"), "able")), /* @__PURE__ */ React.createElement("ul", null, /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("button", { className: "nav-link", onClick: () => scrollTo("connectors") }, "Connectors")), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("button", { className: "nav-link", onClick: () => scrollTo("demo") }, "Demo")), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("button", { className: "nav-link", onClick: () => scrollTo("pricing") }, "Pricing")), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("a", { href: "/docs.html", onClick: () => {
    if (window.posthog) posthog.capture("docs_clicked", { location: "nav" });
  } }, "Docs"))), /* @__PURE__ */ React.createElement("div", { className: "right" }, /* @__PURE__ */ React.createElement("a", { href: "/account.html", className: "nav-signin" }, "Sign in"), /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "https://calendar.app.google/2duYBqjLXaTmX5xC8",
      target: "_blank",
      rel: "noopener noreferrer",
      className: "btn btn-ghost",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "nav", cta: "book_demo" });
      }
    },
    "Book a demo"
  ), /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "/docs.html",
      className: "btn btn-primary",
      onClick: () => {
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
  )), open && /* @__PURE__ */ React.createElement("div", { className: "nav-mobile-menu" }, /* @__PURE__ */ React.createElement("button", { className: "nav-mobile-item", onClick: () => scrollTo("connectors") }, "Connectors"), /* @__PURE__ */ React.createElement("button", { className: "nav-mobile-item", onClick: () => scrollTo("demo") }, "Demo"), /* @__PURE__ */ React.createElement("button", { className: "nav-mobile-item", onClick: () => {
    scrollTo("pricing");
    if (window.posthog) posthog.capture("nav_clicked", { item: "pricing" });
  } }, "Pricing"), /* @__PURE__ */ React.createElement("a", { className: "nav-mobile-item", href: "/docs.html", onClick: () => {
    setOpen(false);
    if (window.posthog) posthog.capture("docs_clicked", { location: "nav_mobile" });
  } }, "Docs"), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 24, display: "flex", flexDirection: "column", gap: 10 } }, /* @__PURE__ */ React.createElement("a", { href: "/account.html", className: "btn btn-ghost", style: { justifyContent: "center" }, onClick: () => setOpen(false) }, "Sign in"), /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "/docs.html",
      className: "btn btn-primary",
      style: { justifyContent: "center" },
      onClick: () => {
        setOpen(false);
        if (window.posthog) posthog.capture("cta_clicked", { location: "nav_mobile", cta: "start_free" });
      }
    },
    "Get started free ",
    /* @__PURE__ */ React.createElement("span", { className: "arr" }, "\u2192")
  ))));
}
function Hero() {
  return /* @__PURE__ */ React.createElement("header", { className: "hero hero-centered", id: "top" }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "hero-c" }, /* @__PURE__ */ React.createElement("h1", { className: "display" }, "Stop guessing why cloud costs went up. ", /* @__PURE__ */ React.createElement("span", { className: "h1-ask" }, "Ask.")), /* @__PURE__ */ React.createElement("p", { className: "hero-sub" }, "Then it finds the waste, writes the fix for you to approve, and proves the savings on your next bill."), /* @__PURE__ */ React.createElement("div", { className: "hero-actions" }, /* @__PURE__ */ React.createElement(CopyCmd, { cmd: "uvx nable" }), /* @__PURE__ */ React.createElement("a", { className: "btn btn-primary", href: "/docs.html", onClick: () => {
    if (window.posthog) posthog.capture("cta_clicked", { location: "hero", cta: "start_free" });
  } }, "Get started free ", /* @__PURE__ */ React.createElement("span", { className: "arr" }, "\u2192"))), /* @__PURE__ */ React.createElement("p", { className: "hero-trustline" }, "Every cloud + AI bill in ", /* @__PURE__ */ React.createElement("b", null, "one place"), " \xB7 works in any editor \xB7 free for solo use"))));
}
function CopyCmd({ cmd }) {
  const [copied, setCopied] = useState(false);
  return /* @__PURE__ */ React.createElement("button", { className: "copycmd", onClick: () => {
    navigator.clipboard?.writeText(cmd);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
    if (window.posthog) posthog.capture("install_copied");
  } }, /* @__PURE__ */ React.createElement("span", { className: "prompt" }, "$"), /* @__PURE__ */ React.createElement("span", { className: "cmd" }, cmd), /* @__PURE__ */ React.createElement("span", { className: "copylab" }, copied ? "copied" : "copy"));
}
function fmtNum(n) {
  if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
  return String(n);
}
const QUERIES = [
  {
    q: "How much are we spending on databases?",
    response: /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, "Pulled every managed database across your clouds and normalized to USD. This month so far:"), /* @__PURE__ */ React.createElement("div", { className: "ttable" }, /* @__PURE__ */ React.createElement("div", { className: "r hd" }, /* @__PURE__ */ React.createElement("span", null, "Provider \xB7 service"), /* @__PURE__ */ React.createElement("span", null, "Spend"), /* @__PURE__ */ React.createElement("span", null, "delta MoM")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "AWS \xB7 RDS + Aurora"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$9,240"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+11.4%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "GCP \xB7 Cloud SQL"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$3,180"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+6.2%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "MongoDB \xB7 Atlas"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$2,460"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "-2.1%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "Snowflake \xB7 compute"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$1,910"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+18.7%")), /* @__PURE__ */ React.createElement("div", { className: "r total" }, /* @__PURE__ */ React.createElement("span", null, "Total databases"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$16,790"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+9.8%"))), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 12 } }, "Two Aurora instances sit below 20% utilization. Rightsizing them saves about ", /* @__PURE__ */ React.createElement("span", { style: { color: "var(--accent)" } }, "$640 / mo"), ". Want the breakdown?"))
  },
  {
    q: "What's our compute cost across AWS and GCP?",
    response: /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, "Normalized to USD and pulled from each provider's billing API just now. This month:"), /* @__PURE__ */ React.createElement("div", { className: "ttable" }, /* @__PURE__ */ React.createElement("div", { className: "r hd" }, /* @__PURE__ */ React.createElement("span", null, "Provider \xB7 service"), /* @__PURE__ */ React.createElement("span", null, "Spend"), /* @__PURE__ */ React.createElement("span", null, "delta MoM")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "AWS \xB7 EC2 + Fargate"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$18,420"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+18.6%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "GCP \xB7 Compute Engine"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$4,870"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "-3.4%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "GCP \xB7 Cloud Run"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$1,240"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+7.1%")), /* @__PURE__ */ React.createElement("div", { className: "r total" }, /* @__PURE__ */ React.createElement("span", null, "Total compute"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$24,530"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+12.9%"))), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 14 } }, "Most of the AWS jump is three new instances in ", /* @__PURE__ */ React.createElement("span", { className: "mono", style: { color: "var(--fg)" } }, "us-east-1"), ", about $1,890. Want me to tag them and open an audit ticket?"))
  },
  {
    q: "Where is our AI spend going this month?",
    response: /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, "Token spend across your model providers this month, normalized to USD:"), /* @__PURE__ */ React.createElement("div", { className: "ttable" }, /* @__PURE__ */ React.createElement("div", { className: "r hd" }, /* @__PURE__ */ React.createElement("span", null, "Provider \xB7 model"), /* @__PURE__ */ React.createElement("span", null, "Spend"), /* @__PURE__ */ React.createElement("span", null, "delta MoM")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "OpenAI \xB7 gpt-4o"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$4,120"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+34%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "Anthropic \xB7 Claude"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$2,880"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+21%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "AWS \xB7 Bedrock"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$1,610"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+12%")), /* @__PURE__ */ React.createElement("div", { className: "r total" }, /* @__PURE__ */ React.createElement("span", null, "Total AI / LLM"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$8,610"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+26%"))), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 12 } }, "Your token bill is up ", /* @__PURE__ */ React.createElement("span", { style: { color: "var(--alert)" } }, "26%"), " even as per-token prices fell. A gpt-4o classifier is the driver; route it to a cheaper model to save about ", /* @__PURE__ */ React.createElement("span", { style: { color: "var(--accent)" } }, "$1,400 / mo"), "."))
  },
  {
    q: "Which provider grew fastest this month?",
    response: /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, "Ranked every connected provider by month-over-month growth, normalized to USD:"), /* @__PURE__ */ React.createElement("div", { className: "ttable" }, /* @__PURE__ */ React.createElement("div", { className: "r hd" }, /* @__PURE__ */ React.createElement("span", null, "Provider"), /* @__PURE__ */ React.createElement("span", null, "Spend"), /* @__PURE__ */ React.createElement("span", null, "delta MoM")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "OpenAI"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$4,120"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+34%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "Snowflake"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$1,910"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+18.7%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "AWS"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$28,400"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+12.4%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "GCP"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$9,300"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+3.1%")), /* @__PURE__ */ React.createElement("div", { className: "r total" }, /* @__PURE__ */ React.createElement("span", null, "Fastest grower"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "OpenAI"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+34%"))), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 12 } }, "OpenAI grew fastest in percent, but AWS added the most dollars: ", /* @__PURE__ */ React.createElement("span", { style: { color: "var(--alert)" } }, "+$3,130"), ". Want either one traced to the team that caused it?"))
  },
  {
    q: "Any anomalies this week?",
    response: /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, /* @__PURE__ */ React.createElement("span", { className: "anomaly" }, "Datadog spike detected."), " Usage is up ", /* @__PURE__ */ React.createElement("span", { style: { color: "var(--alert)" } }, "+127%"), " vs your same-weekday baseline. Z-score 4.8 against the 28-day window."), /* @__PURE__ */ React.createElement("div", { className: "ttable" }, /* @__PURE__ */ React.createElement("div", { className: "r hd" }, /* @__PURE__ */ React.createElement("span", null, "Tag driver"), /* @__PURE__ */ React.createElement("span", null, "Delta"), /* @__PURE__ */ React.createElement("span", null, "% of spike")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "team=platform"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+$2,290"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "78%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "team=infra"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+$480"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "16%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "(untagged)"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+$180"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "6%"))), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 12 } }, "Opened ", /* @__PURE__ */ React.createElement("span", { className: "mono", style: { color: "var(--fg)" } }, "JIRA-2841"), ", paged @sre, posted to ", /* @__PURE__ */ React.createElement("span", { className: "mono", style: { color: "var(--fg)" } }, "#cost-alerts"), ". ", /* @__PURE__ */ React.createElement("span", { style: { color: "var(--accent)" } }, "Drift contained.")))
  },
  {
    q: "What's our effective discount rate this quarter?",
    response: /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, "Blended across Savings Plans, RIs, and committed-use discounts on GCP. Coverage measured against on-demand list:"), /* @__PURE__ */ React.createElement("div", { className: "ttable" }, /* @__PURE__ */ React.createElement("div", { className: "r hd" }, /* @__PURE__ */ React.createElement("span", null, "Commitment"), /* @__PURE__ */ React.createElement("span", null, "Coverage"), /* @__PURE__ */ React.createElement("span", null, "Effective rate")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "AWS \xB7 Savings Plans (1y)"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "82%"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "-24.1%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "AWS \xB7 RIs (RDS, ElastiCache)"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "71%"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "-31.8%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "GCP \xB7 CUDs (compute)"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "64%"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "-20.4%")), /* @__PURE__ */ React.createElement("div", { className: "r total" }, /* @__PURE__ */ React.createElement("span", null, "Blended effective discount"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "-"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "-26.7%"))), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 12 } }, "You'd unlock another ", /* @__PURE__ */ React.createElement("span", { style: { color: "var(--accent)" } }, "$8,200 / mo"), " by raising Compute SP coverage to 92%. Model it?"))
  },
  {
    q: "What can you actually do?",
    response: /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, "A lot, and all of it from your editor. On a connected account I can:"), /* @__PURE__ */ React.createElement("ul", { className: "caps" }, /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("b", null, "Answer cost questions"), " across AWS, Azure, GCP, Kubernetes and 13+ SaaS and AI providers"), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("b", null, "Catch anomalies"), " with Z-score detection and name the tag driving the spike"), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("b", null, "Find savings"), ": rightsizing, idle cleanup, commitment and discount coverage"), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("b", null, "Track AI spend"), " by model and forecast where your token bill lands"), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("b", null, "Act"), ": open a rightsizing PR against your IaC, file a ticket, post to Slack")), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 12 } }, /* @__PURE__ */ React.createElement("span", { style: { color: "var(--accent)" } }, "160+ tools"), " in all. Pick a prompt below to run a real one."))
  }
];
function matchQuery(text) {
  const t = (text || "").toLowerCase();
  if (/what can|capabilit|what do you do|what does nable|use case|everything you|all the tools|how does (this|it) work|what should i ask/.test(t)) return 6;
  if (/\bai\b|llm|token|openai|anthropic|claude|bedrock|\bgpt|inference|model (spend|cost|bill)/.test(t)) return 2;
  if (/discount|savings ?plan|reserved|reservation|\bri\b|commitment|coverage|effective (rate|discount)|\bcud/.test(t)) return 5;
  if (/anomal|spike|spiking|surge|unusual|datadog|went up|going up|jump/.test(t)) return 4;
  if (/grew fastest|grow(ing)? fastest|fastest grow|biggest (mover|grow|increase|jump)|which provider|who grew|ranked? by growth/.test(t)) return 3;
  if (/database|\brds\b|aurora|cloud ?sql|postgres|mysql|mongo|snowflake|warehouse|\bdb\b/.test(t)) return 0;
  if (/across (all )?(provider|cloud)|all providers|multi-?cloud|aws.*(vs|versus|and).*(azure|gcp)|gcp.*(vs|versus|and).*aws|month.?over.?month|\bmom\b|compute.*(across|provider|month|vs|versus|cost)|ec2|fargate|compute engine/.test(t)) return 1;
  if (/wast|idle|rightsiz|right-?siz|over-?provision|low cpu|cut (cost|spend)|save money|saving money|where can i save|trim/.test(t)) return 0;
  return -1;
}
const GATE = /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, "That's exactly the kind of question nable answers against your ", /* @__PURE__ */ React.createElement("b", null, "own"), " account, with your real numbers. This demo only knows the sample account above."), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 12 } }, "Connect it in about a minute, then ask away on your real bill. It runs on your machine, nothing leaves it:"), /* @__PURE__ */ React.createElement("div", { className: "gate-cmd" }, /* @__PURE__ */ React.createElement(CopyCmd, { cmd: "uvx nable" })), /* @__PURE__ */ React.createElement("p", { className: "gate-sub" }, "Free for solo use, no signup. Runs on your machine."));
const OFFTOPIC = /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, "That one's outside this demo. nable here only covers cloud and AI cost, so ask about your AWS, Azure, GCP, Kubernetes or AI spend, or try a prompt below."));
const FINANCE_RE = /cost|spend|bill|budget|forecast|sav(e|ing)|money|cheap|expensive|pric(e|ing)|discount|invoice|usage|waste|idle|optimi[sz]e|rightsiz|reserved|reservation|commitment|anomal|cloud|aws|azure|gcp|ec2|\bs3\b|rds|lambda|fargate|eks|kubernetes|k8s|container|cluster|instance|\bvm\b|server|database|storage|snowflake|databricks|datadog|gpu|\bai\b|llm|token|openai|anthropic|claude|bedrock|gpt|\bmodel\b|provider|region|account|\btag|dollar|\$/;
const CHIPS = [
  { label: "What can you do?", idx: 6 },
  { label: "Spend on databases?", idx: 0 },
  { label: "Compute across AWS and GCP?", idx: 1 },
  { label: "Where's our AI spend going?", idx: 2 },
  { label: "Which provider grew fastest?", idx: 3 }
];
function Console({ interaction }) {
  const [phase, setPhase] = useState("answered");
  const [typed, setTyped] = useState(QUERIES[0].q);
  const [answer, setAnswer] = useState(QUERIES[0].response);
  const [asked, setAsked] = useState(false);
  const [focused, setFocused] = useState(false);
  const [input, setInput] = useState("");
  const [cycleIdx, setCycleIdx] = useState(0);
  const [isGate, setIsGate] = useState(false);
  const [offTopic, setOffTopic] = useState(false);
  const timers = useRef([]);
  function clearTimers() {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  }
  function runExchange(qText, ansJSX) {
    clearTimers();
    setTyped("");
    setAnswer(ansJSX);
    setPhase("typing");
    let i = 0;
    (function step() {
      if (i <= qText.length) {
        setTyped(qText.slice(0, i));
        i++;
        timers.current.push(setTimeout(step, 16 + Math.random() * 20));
      } else {
        timers.current.push(setTimeout(() => setPhase("thinking"), 280));
        timers.current.push(setTimeout(() => setPhase("answered"), 1e3));
      }
    })();
  }
  useEffect(() => {
    if (interaction !== "cycling" || asked || focused) return;
    if (phase !== "answered") return;
    const t = setTimeout(() => {
      const next = (cycleIdx + 1) % QUERIES.length;
      setCycleIdx(next);
      runExchange(QUERIES[next].q, QUERIES[next].response);
    }, 6500);
    return () => clearTimeout(t);
  }, [phase, interaction, asked, focused, cycleIdx]);
  useEffect(() => () => clearTimers(), []);
  function ask(text) {
    const q = (text || "").trim();
    if (!q) return;
    setAsked(true);
    setInput("");
    const m = matchQuery(q);
    let kind;
    if (m >= 0) {
      setIsGate(false);
      setOffTopic(false);
      runExchange(q, QUERIES[m].response);
      kind = "answer";
    } else if (FINANCE_RE.test(q.toLowerCase())) {
      setIsGate(true);
      setOffTopic(false);
      runExchange(q, GATE);
      kind = "gate";
    } else {
      setIsGate(false);
      setOffTopic(true);
      runExchange(q, OFFTOPIC);
      kind = "offtopic";
    }
    if (window.posthog) posthog.capture("hero_demo_ask", { kind });
  }
  function pickChip(c) {
    setAsked(true);
    setInput("");
    setIsGate(false);
    setOffTopic(false);
    runExchange(QUERIES[c.idx].q, QUERIES[c.idx].response);
    if (window.posthog) posthog.capture("hero_demo_chip", { idx: c.idx });
  }
  return /* @__PURE__ */ React.createElement("div", { className: "console", id: "runtime" }, /* @__PURE__ */ React.createElement("div", { className: "console-bar" }, /* @__PURE__ */ React.createElement("div", { className: "dots" }, /* @__PURE__ */ React.createElement("i", null), /* @__PURE__ */ React.createElement("i", null), /* @__PURE__ */ React.createElement("i", null)), /* @__PURE__ */ React.createElement("span", { className: "title" }, "claude \xB7 mcp[nable] \xB7 ~/projects/platform-infra"), /* @__PURE__ */ React.createElement("span", { className: "status" }, "runtime active")), /* @__PURE__ */ React.createElement("div", { className: "console-body" }, /* @__PURE__ */ React.createElement("div", { className: "msg" }, /* @__PURE__ */ React.createElement("div", { className: "av you" }, "you"), /* @__PURE__ */ React.createElement("div", { className: "bubble user" }, /* @__PURE__ */ React.createElement("p", null, typed, /* @__PURE__ */ React.createElement("span", { className: "cursor" })))), phase === "thinking" && /* @__PURE__ */ React.createElement("div", { className: "msg" }, /* @__PURE__ */ React.createElement("div", { className: "av ai" }, "nable"), /* @__PURE__ */ React.createElement("div", { className: "bubble" }, /* @__PURE__ */ React.createElement("div", { className: "thinking" }, /* @__PURE__ */ React.createElement("i", null), /* @__PURE__ */ React.createElement("i", null), /* @__PURE__ */ React.createElement("i", null)))), phase === "answered" && /* @__PURE__ */ React.createElement("div", { className: "msg" }, /* @__PURE__ */ React.createElement("div", { className: "av ai" }, "nable"), /* @__PURE__ */ React.createElement("div", { className: "bubble" }, answer))));
}
function SeeItWork({ interaction }) {
  return /* @__PURE__ */ React.createElement("section", { id: "demo", className: "demo-sec" }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "section-head center" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "See it work"), /* @__PURE__ */ React.createElement("h2", null, "Ask your bill like you'd", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "ask a teammate.")), /* @__PURE__ */ React.createElement("p", null, "nable pulls every connected provider, normalizes to USD, and answers in plain English. Watch it run through real questions, or ask your own.")), /* @__PURE__ */ React.createElement("div", { className: "console-stage" }, /* @__PURE__ */ React.createElement(Console, { interaction }))));
}
function AiCost() {
  const copy = () => {
    if (navigator.clipboard) navigator.clipboard.writeText("uvx nable");
    if (window.posthog) posthog.capture("cta_clicked", { location: "ai_cost", cta: "copy_install" });
  };
  return /* @__PURE__ */ React.createElement("section", { id: "ai", className: "alt", style: { borderTop: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "ee-grid" }, /* @__PURE__ */ React.createElement("div", { className: "ee-left" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "Your AI bill"), /* @__PURE__ */ React.createElement("h2", null, "Tools chart your AI spend.", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "nable finds the waste.")), /* @__PURE__ */ React.createElement("p", { className: "ee-lede" }, "Most of an AI bill is input tokens billed at full price, plus calls sent to a frontier model a cheaper one would have answered the same way. nable reads the split from your real usage and shows you the cheapest way to get the same output. No caching guesswork."), /* @__PURE__ */ React.createElement("ul", { className: "ee-points" }, /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("span", { className: "ee-plus" }, "+"), /* @__PURE__ */ React.createElement("span", null, "Input, output and cache, ", /* @__PURE__ */ React.createElement("b", null, "split from your actual bill"))), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("span", { className: "ee-plus" }, "+"), /* @__PURE__ */ React.createElement("span", null, "Flags ", /* @__PURE__ */ React.createElement("b", null, "frontier-model calls"), " a cheaper model handles the same")), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("span", { className: "ee-plus" }, "+"), /* @__PURE__ */ React.createElement("span", null, "Separates ", /* @__PURE__ */ React.createElement("b", null, "what you can bank today"), " from what needs a closer look")))), /* @__PURE__ */ React.createElement("div", { className: "ee-right" }, /* @__PURE__ */ React.createElement("div", { className: "aicost-panel" }, /* @__PURE__ */ React.createElement("div", { className: "aicost-tag" }, "Real numbers \xB7 real dollars \xB7 first scan"), /* @__PURE__ */ React.createElement("div", { className: "aicost-stat" }, /* @__PURE__ */ React.createElement("div", { className: "aicost-big" }, "89", /* @__PURE__ */ React.createElement("span", { className: "aicost-unit" }, "%")), /* @__PURE__ */ React.createElement("p", null, "of an early user's Bedrock bill was input tokens, billed at full price with ", /* @__PURE__ */ React.createElement("b", null, "no caching"))), /* @__PURE__ */ React.createElement("div", { className: "aicost-rule" }), /* @__PURE__ */ React.createElement("div", { className: "aicost-stat" }, /* @__PURE__ */ React.createElement("div", { className: "aicost-big accent" }, "$10.7k", /* @__PURE__ */ React.createElement("span", { className: "aicost-unit" }, "/yr")), /* @__PURE__ */ React.createElement("p", null, /* @__PURE__ */ React.createElement("b", null, "= $896/mo"), " in prompt-caching savings, about a quarter of the AI bill, on the first scan")), /* @__PURE__ */ React.createElement("div", { className: "aicost-foot" }, "From an early user's first scan. Real numbers, name withheld for now."), /* @__PURE__ */ React.createElement("div", { className: "aicost-cta" }, /* @__PURE__ */ React.createElement("span", { className: "aicost-cta-l" }, "This is a small account. See your own number, free:"), /* @__PURE__ */ React.createElement("code", { className: "aicost-cmd", onClick: copy }, "uvx nable")))))));
}
function Architecture({ version }) {
  return /* @__PURE__ */ React.createElement("section", { id: "arch", className: "alt" }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "section-head" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "Architecture"), /* @__PURE__ */ React.createElement("h2", null, "Run it yourself,", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "or let us host it.")), /* @__PURE__ */ React.createElement("p", null, "Same runtime, your choice of where it runs. Point it at your providers, ask in your editor, and the same analysis runs either way. The connector holds the credentials and pulls the bills directly; nothing is pooled across customers.")), /* @__PURE__ */ React.createElement("div", { className: "arch" }, /* @__PURE__ */ React.createElement("div", { className: "arch-grid" }), /* @__PURE__ */ React.createElement("div", { className: "arch-row" }, /* @__PURE__ */ React.createElement("div", { className: "arch-col" }, /* @__PURE__ */ React.createElement("span", { className: "lab" }, "your editor"), /* @__PURE__ */ React.createElement("div", { className: "arch-node" }, /* @__PURE__ */ React.createElement("h4", null, "Claude \xB7 Cursor \xB7 Zed"), /* @__PURE__ */ React.createElement("span", { className: "sub" }, "MCP client"), /* @__PURE__ */ React.createElement("div", { className: "chips" }, /* @__PURE__ */ React.createElement("span", null, "tools/list"), /* @__PURE__ */ React.createElement("span", null, "tools/call")))), /* @__PURE__ */ React.createElement("div", { className: "arch-arrow" }, /* @__PURE__ */ React.createElement("span", null, "stdio"), /* @__PURE__ */ React.createElement("span", { className: "line" }), /* @__PURE__ */ React.createElement("span", null, "jsonrpc")), /* @__PURE__ */ React.createElement("div", { className: "arch-col" }, /* @__PURE__ */ React.createElement("span", { className: "lab" }, "runtime \xB7 local or hosted"), /* @__PURE__ */ React.createElement("div", { className: "arch-node center" }, /* @__PURE__ */ React.createElement("h4", null, "nable runtime"), /* @__PURE__ */ React.createElement("span", { className: "sub" }, "your machine or a single-tenant host"), /* @__PURE__ */ React.createElement("div", { className: "chips" }, /* @__PURE__ */ React.createElement("span", null, "keyring"), /* @__PURE__ */ React.createElement("span", null, "fernet"), /* @__PURE__ */ React.createElement("span", null, "read-only"), /* @__PURE__ */ React.createElement("span", null, "audit-log")))), /* @__PURE__ */ React.createElement("div", { className: "arch-arrow" }, /* @__PURE__ */ React.createElement("span", null, "https"), /* @__PURE__ */ React.createElement("span", { className: "line" }), /* @__PURE__ */ React.createElement("span", null, "signed")), /* @__PURE__ */ React.createElement("div", { className: "arch-col" }, /* @__PURE__ */ React.createElement("span", { className: "lab" }, "provider apis"), /* @__PURE__ */ React.createElement("div", { className: "arch-node" }, /* @__PURE__ */ React.createElement("h4", null, "17 connectors"), /* @__PURE__ */ React.createElement("span", { className: "sub" }, "cost \xB7 usage \xB7 billing"), /* @__PURE__ */ React.createElement("div", { className: "chips" }, /* @__PURE__ */ React.createElement("span", null, "AWS CE/CUR"), /* @__PURE__ */ React.createElement("span", null, "Azure CM"), /* @__PURE__ */ React.createElement("span", null, "GCP BQ"), /* @__PURE__ */ React.createElement("span", null, "+14")))))), /* @__PURE__ */ React.createElement("div", { className: "host-opts" }, /* @__PURE__ */ React.createElement("div", { className: "host-opt" }, /* @__PURE__ */ React.createElement("span", { className: "host-tag" }, "Run it yourself"), /* @__PURE__ */ React.createElement("h4", null, "Local-first, on your machine"), /* @__PURE__ */ React.createElement("p", null, "Install with one command. Credentials live in your OS keyring, cost data caches in a local SQLite file, and queries hit your provider APIs directly. There is no nable backend in the path and no data lake to breach. For zero AI exposure, use the local dashboard or CLI, which never call a model."), /* @__PURE__ */ React.createElement("div", { className: "gate-cmd" }, /* @__PURE__ */ React.createElement(CopyCmd, { cmd: "uvx nable" }))), /* @__PURE__ */ React.createElement("div", { className: "host-opt" }, /* @__PURE__ */ React.createElement("span", { className: "host-tag" }, "Or let us host it"), /* @__PURE__ */ React.createElement("h4", null, "Managed, single-tenant"), /* @__PURE__ */ React.createElement("p", null, "Want it always on without running it yourself? We deploy and manage a single-tenant instance for your org: your own runtime, your own store, isolated from every other customer. Same connectors, same analysis, plus the dashboard with SSO (Okta, Entra ID, Google Workspace), RBAC, and share links. Single-tenant by design, never a shared pool."), /* @__PURE__ */ React.createElement(
    "a",
    {
      className: "btn btn-ghost host-cta",
      href: BOOK_CALL_LINK,
      target: "_blank",
      rel: "noopener noreferrer",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "architecture", cta: "hosted_demo" });
      }
    },
    "Talk to us about hosting ",
    /* @__PURE__ */ React.createElement("span", { className: "arr" }, "\u2192")
  )))));
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
  { nm: "Databricks", px: "DBU usage \xB7 job costs", tag: "live" },
  { nm: "OpenAI", px: "Usage API \xB7 per-model spend", tag: "live" },
  { nm: "Anthropic", px: "Org usage \xB7 per-model spend", tag: "live" },
  { nm: "Stripe", px: "Billing meter \xB7 platform fees", tag: "beta" },
  { nm: "PagerDuty", px: "License spend \xB7 on-call costs", tag: "beta" },
  { nm: "Coming soon", px: "Vote on the next connector", tag: "soon" }
];
const LOGOS = [
  { n: "AWS", f: "aws" },
  { n: "Azure", f: "azure" },
  { n: "GCP", f: "gcp" },
  { n: "OpenAI", f: "openai", icon: true },
  { n: "Anthropic", f: "anthropic", icon: true },
  { n: "Stripe", f: "stripe" },
  { n: "Datadog", f: "datadog", icon: true },
  { n: "Snowflake", f: "snowflake" },
  { n: "GitHub", f: "github" }
];
function Connectors() {
  return /* @__PURE__ */ React.createElement("section", { id: "connectors", className: "alt" }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "section-head" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "Connectors"), /* @__PURE__ */ React.createElement("h2", null, "All 17 sources,", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "one conversation.")), /* @__PURE__ */ React.createElement("p", null, "Every connector is a real API integration, not a CSV export. New providers ship monthly."))), /* @__PURE__ */ React.createElement("div", { className: "logo-marquee" }, /* @__PURE__ */ React.createElement("div", { className: "logo-track" }, [...LOGOS, ...LOGOS, ...LOGOS].map((l, i) => /* @__PURE__ */ React.createElement("img", { className: "logo-img" + (l.icon ? " is-icon" : ""), key: i, src: "/vendor/logos/" + l.f + ".svg", alt: l.n, title: l.n, loading: "lazy" })))), /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("p", { className: "logo-band-note" }, "+ 8 more connectors \xB7 new providers ship monthly")));
}
function CheckIcon() {
  return /* @__PURE__ */ React.createElement("svg", { width: "15", height: "15", viewBox: "0 0 15 15", fill: "none", "aria-hidden": "true", style: { flexShrink: 0, marginTop: 1 } }, /* @__PURE__ */ React.createElement("circle", { cx: "7.5", cy: "7.5", r: "7", stroke: "currentColor", strokeWidth: "1" }), /* @__PURE__ */ React.createElement("path", { d: "M4.5 7.5L6.5 9.5L10.5 5.5", stroke: "currentColor", strokeWidth: "1.4", strokeLinecap: "round", strokeLinejoin: "round" }));
}
const PRO_MONTHLY_LINK = "https://buy.stripe.com/9B600igyt1oO1d69V02Nq06";
const PRO_ANNUAL_LINK = "https://buy.stripe.com/bJe5kCbe97Nc0924AG2Nq07";
const STARTUP_MONTHLY_LINK = "https://buy.stripe.com/3cI3cucid6J85tm3wC2Nq08";
const STARTUP_ANNUAL_LINK = "https://buy.stripe.com/14A6oG0zvgjI9JCffk2Nq09";
const BOOK_CALL_LINK = "https://calendar.app.google/2duYBqjLXaTmX5xC8";
const PRICE_ROWS = [
  { label: "Users", dev: "Just you", pro: "Your team", startup: "Your team", ent: "Your org" },
  { label: "Cost queries, anomalies, rightsizing, 17 connectors", dev: true, pro: true, startup: true, ent: true },
  { label: "Remediation PRs, alerts, dashboards, Slack bot", dev: false, pro: true, startup: true, ent: true },
  { label: "Runs", dev: "Your machine", pro: "Your machine", startup: "Hosted, single-tenant", ent: "Hosted or self-host" },
  { label: "Managed AI", dev: "Your own key", pro: "Your own key", startup: "Included, metered", ent: "Custom" },
  { label: "SSO + audit logs", dev: false, pro: false, startup: false, ent: true },
  { label: "Support", dev: "Slack", pro: "Slack", startup: "Slack", ent: "Slack + SLA" }
];
function PCell({ v }) {
  if (v === true) return /* @__PURE__ */ React.createElement("span", { className: "pcheck" }, /* @__PURE__ */ React.createElement(CheckIcon, null));
  if (v === false) return /* @__PURE__ */ React.createElement("span", { className: "pdash" }, "\u2013");
  return /* @__PURE__ */ React.createElement("span", { className: "pval" }, v);
}
function PricingCards({ annual, proPrice, proPer, proSub, proLink, proPlan, startupPrice, startupPer, startupSub, startupLink, startupPlan }) {
  const tiers = [
    {
      key: "dev",
      name: "Dev",
      price: "Free",
      per: "forever",
      sub: "solo \xB7 no credit card",
      rec: false,
      primary: false,
      cta: "Start free",
      href: "/docs.html",
      plan: "dev",
      ext: false
    },
    {
      key: "pro",
      name: "Pro",
      price: proPrice,
      per: proPer,
      sub: proSub,
      rec: true,
      primary: true,
      cta: annual ? "Get annual" : "Get Pro",
      href: proLink,
      plan: proPlan,
      ext: true
    },
    {
      key: "startup",
      name: "Startups",
      price: startupPrice,
      per: startupPer,
      sub: startupSub,
      rec: false,
      primary: false,
      cta: "Get Startups",
      href: startupLink,
      plan: startupPlan,
      ext: true
    },
    {
      key: "ent",
      name: "Enterprise",
      price: "Custom",
      per: "annual",
      sub: "SSO, audit logs + SLA",
      rec: false,
      primary: false,
      cta: "Contact us",
      href: BOOK_CALL_LINK,
      plan: "enterprise",
      ext: true
    }
  ];
  return /* @__PURE__ */ React.createElement("div", { className: "pcards" }, tiers.map((t) => /* @__PURE__ */ React.createElement("div", { className: "pcard" + (t.rec ? " pcard-rec" : ""), key: t.key }, t.rec && /* @__PURE__ */ React.createElement("div", { className: "pcard-badge" }, "Recommended"), /* @__PURE__ */ React.createElement("div", { className: "pcard-name" }, t.name), /* @__PURE__ */ React.createElement("div", { className: "pcard-price" }, /* @__PURE__ */ React.createElement("span", { className: "pcard-amt" }, t.price), t.per && /* @__PURE__ */ React.createElement("span", { className: "pcard-per" }, t.per)), t.sub && /* @__PURE__ */ React.createElement("div", { className: "pcard-sub" }, t.sub), /* @__PURE__ */ React.createElement(
    "a",
    {
      className: "btn " + (t.primary ? "btn-primary" : "btn-ghost") + " pcard-cta",
      href: t.href,
      ...t.ext ? { target: "_blank", rel: "noopener noreferrer" } : {},
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "pricing_mobile", plan: t.plan, billing: annual ? "annual" : "monthly" });
      }
    },
    t.cta
  ), /* @__PURE__ */ React.createElement("ul", { className: "pcard-feats" }, PRICE_ROWS.filter((r) => r[t.key] !== false).map((r, i) => /* @__PURE__ */ React.createElement("li", { key: i }, /* @__PURE__ */ React.createElement(CheckIcon, null), /* @__PURE__ */ React.createElement("span", null, r.label, typeof r[t.key] === "string" ? /* @__PURE__ */ React.createElement("em", { className: "pcard-val" }, " \xB7 ", r[t.key]) : null)))))));
}
function Pricing() {
  const [annual, setAnnual] = useState(false);
  const proPrice = annual ? "$1,000" : "$100";
  const proPer = annual ? "/ yr flat" : "/ mo flat";
  const proSub = annual ? "$83 / mo \xB7 2 months free" : "flat, not per-seat \xB7 7-day free trial";
  const proLink = annual ? PRO_ANNUAL_LINK : PRO_MONTHLY_LINK;
  const proPlan = annual ? "pro_annual" : "pro_monthly";
  const startupPrice = annual ? "$10,000" : "$1,000";
  const startupPer = annual ? "/ yr" : "/ mo";
  const startupSub = annual ? "2 months free \xB7 hosted \xB7 managed AI" : "hosted single-tenant \xB7 managed AI";
  const startupLink = annual ? STARTUP_ANNUAL_LINK : STARTUP_MONTHLY_LINK;
  const startupPlan = annual ? "startups_annual" : "startups_monthly";
  return /* @__PURE__ */ React.createElement("section", { id: "pricing" }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "section-head" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "Pricing"), /* @__PURE__ */ React.createElement("h2", null, "Free to ask.", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "Pay to remediate.")), /* @__PURE__ */ React.createElement("p", null, "Dev is free forever, local, your own LLM key. Pro is one flat $100 a month for your whole team: remediation PRs, tickets, alerts, dashboards, the Slack bot, still your key. Startups is $1,000 a month: we host it single-tenant and run a managed AI agent, with usage metered above the included allowance. Enterprise adds SSO, audit logs, and an SLA."), /* @__PURE__ */ React.createElement("div", { className: "bill-toggle", role: "group", "aria-label": "Billing period" }, /* @__PURE__ */ React.createElement("div", { className: "seg" }, /* @__PURE__ */ React.createElement("button", { className: "seg-btn" + (annual ? "" : " active"), onClick: () => setAnnual(false), "aria-pressed": !annual }, "Monthly"), /* @__PURE__ */ React.createElement("button", { className: "seg-btn" + (annual ? " active" : ""), onClick: () => setAnnual(true), "aria-label": "Toggle annual billing", "aria-pressed": annual }, "Annual")), /* @__PURE__ */ React.createElement("span", { className: "seg-save" }, "SAVE 17%"))), /* @__PURE__ */ React.createElement("div", { className: "ptable-wrap" }, /* @__PURE__ */ React.createElement("div", { className: "ptable ptable-4" }, /* @__PURE__ */ React.createElement("div", { className: "ph ph-corner" }), /* @__PURE__ */ React.createElement("div", { className: "ph" }, /* @__PURE__ */ React.createElement("div", { className: "pt-name" }, "Dev"), /* @__PURE__ */ React.createElement("div", { className: "pt-price" }, /* @__PURE__ */ React.createElement("span", { className: "pt-amt" }, "Free"), /* @__PURE__ */ React.createElement("span", { className: "pt-per" }, "forever")), /* @__PURE__ */ React.createElement("div", { className: "pt-sub" }, "solo \xB7 no credit card"), /* @__PURE__ */ React.createElement(
    "a",
    {
      className: "btn btn-ghost pt-cta",
      href: "/docs.html",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "pricing", plan: "dev" });
      }
    },
    "Start free"
  )), /* @__PURE__ */ React.createElement("div", { className: "ph pcol-team" }, /* @__PURE__ */ React.createElement("div", { className: "pt-rec" }, "Recommended"), /* @__PURE__ */ React.createElement("div", { className: "pt-name" }, "Pro"), /* @__PURE__ */ React.createElement("div", { className: "pt-price" }, /* @__PURE__ */ React.createElement("span", { className: "pt-amt" }, proPrice), /* @__PURE__ */ React.createElement("span", { className: "pt-per" }, proPer)), /* @__PURE__ */ React.createElement("div", { className: "pt-sub" }, proSub), /* @__PURE__ */ React.createElement(
    "a",
    {
      className: "btn btn-primary pt-cta",
      href: proLink,
      target: "_blank",
      rel: "noopener noreferrer",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "pricing", plan: proPlan, billing: annual ? "annual" : "monthly" });
      }
    },
    annual ? "Get annual" : "Get Pro"
  )), /* @__PURE__ */ React.createElement("div", { className: "ph" }, /* @__PURE__ */ React.createElement("div", { className: "pt-name" }, "Startups"), /* @__PURE__ */ React.createElement("div", { className: "pt-price" }, /* @__PURE__ */ React.createElement("span", { className: "pt-amt" }, startupPrice), /* @__PURE__ */ React.createElement("span", { className: "pt-per" }, startupPer)), /* @__PURE__ */ React.createElement("div", { className: "pt-sub" }, startupSub), /* @__PURE__ */ React.createElement(
    "a",
    {
      className: "btn btn-ghost pt-cta",
      href: startupLink,
      target: "_blank",
      rel: "noopener noreferrer",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "pricing", plan: startupPlan, billing: annual ? "annual" : "monthly" });
      }
    },
    "Get Startups"
  )), /* @__PURE__ */ React.createElement("div", { className: "ph" }, /* @__PURE__ */ React.createElement("div", { className: "pt-name" }, "Enterprise"), /* @__PURE__ */ React.createElement("div", { className: "pt-price" }, /* @__PURE__ */ React.createElement("span", { className: "pt-amt" }, "Custom"), /* @__PURE__ */ React.createElement("span", { className: "pt-per" }, "annual")), /* @__PURE__ */ React.createElement("div", { className: "pt-sub" }, "SSO, audit logs + SLA"), /* @__PURE__ */ React.createElement(
    "a",
    {
      className: "btn btn-ghost pt-cta",
      href: BOOK_CALL_LINK,
      target: "_blank",
      rel: "noopener noreferrer",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "pricing", plan: "enterprise" });
      }
    },
    "Contact us"
  )), PRICE_ROWS.map((r, i) => /* @__PURE__ */ React.createElement(React.Fragment, { key: i }, /* @__PURE__ */ React.createElement("div", { className: "pr pr-label" }, r.label), /* @__PURE__ */ React.createElement("div", { className: "pr pr-cell" }, /* @__PURE__ */ React.createElement(PCell, { v: r.dev })), /* @__PURE__ */ React.createElement("div", { className: "pr pr-cell pcol-team" }, /* @__PURE__ */ React.createElement(PCell, { v: r.pro })), /* @__PURE__ */ React.createElement("div", { className: "pr pr-cell" }, /* @__PURE__ */ React.createElement(PCell, { v: r.startup })), /* @__PURE__ */ React.createElement("div", { className: "pr pr-cell" }, /* @__PURE__ */ React.createElement(PCell, { v: r.ent })))))), /* @__PURE__ */ React.createElement(PricingCards, { annual, proPrice, proPer, proSub, proLink, proPlan, startupPrice, startupPer, startupSub, startupLink, startupPlan }), /* @__PURE__ */ React.createElement("p", { className: "pfoot" }, "No credit card for Dev. Pro and Startups trials require a card, cancel any time."), /* @__PURE__ */ React.createElement("p", { className: "pfoot pdemo" }, "Weighing Pro or Startups for your org?", " ", /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "https://calendar.app.google/2duYBqjLXaTmX5xC8",
      target: "_blank",
      rel: "noopener noreferrer",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "pricing", cta: "book_demo" });
      }
    },
    "Book a 20-min demo"
  ), " and we'll run it on your own bill.")));
}
const FAQ_QA = [
  [
    "What is nable?",
    "nable is a local-first, AI-native FinOps tool. It is an MCP server you install on your own machine to ask about your AWS, Azure, GCP, and AI or LLM spend right inside Claude, Cursor, or any MCP editor. Your credentials never leave your machine."
  ],
  [
    "Is nable free?",
    "Yes. The Dev tier is free with no credit card and no expiry: cost queries, anomaly detection, rightsizing, LLM spend tracking, and every connector. Paid tiers add remediation pull requests, alerts, scheduled digests, and single-tenant hosting."
  ],
  [
    "Does nable see or store my cloud credentials?",
    "No. nable runs on your machine. Credentials stay in your OS keyring and cost data caches in a local SQLite database. There is no nable backend that holds your data, and nothing is shipped to a vendor."
  ],
  [
    "Can nable change my cloud infrastructure on its own?",
    "No. nable is propose-only. It drafts a pull request or opens a ticket with the fix, and a human reviews and applies it. It never edits, deletes, or buys anything in your environment autonomously."
  ],
  [
    "What clouds and tools does nable support?",
    "AWS, Azure, GCP, and Kubernetes, plus more than ten SaaS and AI providers including Datadog, Snowflake, Databricks, Stripe, OpenAI, Anthropic, and Amazon Bedrock. It exposes 165+ read-only tools your editor can call."
  ],
  [
    "How is nable different from Vantage, CloudHealth, or the AWS FinOps agent?",
    "nable is local-first, your credentials and bills never leave your machine; AI-native, it lives in Claude or Cursor instead of a separate dashboard; and genuinely cross-cloud, including AI and LLM spend in the same answer. It proposes fixes as pull requests for human approval rather than acting on its own."
  ],
  [
    "What is a FinOps MCP server?",
    "MCP, the Model Context Protocol, lets AI editors call external tools. A FinOps MCP server exposes cloud-cost tools to your AI editor, so you ask about spend in your own words and the editor calls the right tool. nable is a local-first FinOps MCP server."
  ],
  [
    "Can nable show what my AI coding costs?",
    "Yes. nable attributes merged pull requests and commits to the AI model that wrote them and joins your LLM spend by model, so you can see what each model shipped and what it cost per pull request or per commit."
  ]
];
function Faq() {
  return /* @__PURE__ */ React.createElement("section", { className: "faq", id: "faq" }, /* @__PURE__ */ React.createElement("div", { className: "wrap faq-wrap" }, /* @__PURE__ */ React.createElement("div", { className: "foot-label" }, /* @__PURE__ */ React.createElement("span", { className: "foot-dash" }), "FAQ"), /* @__PURE__ */ React.createElement("h2", { className: "faq-h" }, "Common questions"), /* @__PURE__ */ React.createElement("div", { className: "faq-list" }, FAQ_QA.map(([q, a], i) => /* @__PURE__ */ React.createElement("details", { className: "faq-item", key: i }, /* @__PURE__ */ React.createElement("summary", { className: "faq-q" }, q), /* @__PURE__ */ React.createElement("p", { className: "faq-a" }, a))))));
}
function FootCta() {
  return /* @__PURE__ */ React.createElement("section", { className: "foot-cta", id: "cta" }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "foot-label" }, /* @__PURE__ */ React.createElement("span", { className: "foot-dash" }), "Get started"), /* @__PURE__ */ React.createElement("h2", { className: "display" }, "One command.", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "Then just ask.")), /* @__PURE__ */ React.createElement("div", { className: "foot-cta-actions" }, /* @__PURE__ */ React.createElement("div", { className: "foot-install" }, /* @__PURE__ */ React.createElement(CopyCmd, { cmd: "uvx nable" })), /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "/docs.html#quickstart",
      className: "foot-quicklink",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "footer_cta", cta: "quickstart" });
      }
    },
    "or read the quickstart ",
    /* @__PURE__ */ React.createElement("span", { className: "arr" }, "\u2192")
  ))));
}
function Footer({ version }) {
  return /* @__PURE__ */ React.createElement("footer", null, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "foot" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("a", { href: "#top", className: "logo", style: { marginBottom: 18 } }, /* @__PURE__ */ React.createElement(LogoMark, null), /* @__PURE__ */ React.createElement("span", null, "nable")), /* @__PURE__ */ React.createElement("p", { style: { color: "var(--fg-3)", fontSize: 13, maxWidth: "34ch", lineHeight: 1.55, marginTop: 10 } }, "Your cloud and AI bill, answered. Made in Austin, TX.")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h5", null, "Product"), /* @__PURE__ */ React.createElement("a", { href: "#demo" }, "Demo"), /* @__PURE__ */ React.createElement("a", { href: "#connectors" }, "Connectors"), /* @__PURE__ */ React.createElement("a", { href: "#pricing" }, "Pricing"), /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "https://calendar.app.google/2duYBqjLXaTmX5xC8",
      target: "_blank",
      rel: "noopener noreferrer",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "footer_nav", cta: "book_demo" });
      }
    },
    "Book a demo"
  )), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h5", null, "Resources"), /* @__PURE__ */ React.createElement("a", { href: "/docs.html" }, "Docs"), /* @__PURE__ */ React.createElement("a", { href: "/docs.html#quickstart" }, "Quickstart"), /* @__PURE__ */ React.createElement("a", { href: "/docs.html#iam" }, "IAM templates"), /* @__PURE__ */ React.createElement("a", { href: "/security" }, "Security")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h5", null, "Company"), /* @__PURE__ */ React.createElement("a", { href: "/about" }, "About"), /* @__PURE__ */ React.createElement("a", { href: "/about#investors" }, "Investors"), /* @__PURE__ */ React.createElement("a", { href: "mailto:hello@getnable.com", target: "_blank", rel: "noopener noreferrer" }, "Contact"), /* @__PURE__ */ React.createElement("a", { href: "https://github.com/chaandannn/finopsmcp", target: "_blank", rel: "noopener noreferrer" }, "GitHub"), /* @__PURE__ */ React.createElement("a", { href: "https://www.linkedin.com/company/getnable/", target: "_blank", rel: "noopener noreferrer" }, "LinkedIn"))), /* @__PURE__ */ React.createElement("div", { className: "foot-meta" }, /* @__PURE__ */ React.createElement("span", null, "2026 nable \xB7 ", /* @__PURE__ */ React.createElement("a", { href: "/privacy", style: { color: "var(--fg-3)" } }, "Privacy"), " \xB7 ", /* @__PURE__ */ React.createElement("a", { href: "/terms", style: { color: "var(--fg-3)" } }, "Terms")), /* @__PURE__ */ React.createElement("span", null, "nable \xB7 runtime healthy"))));
}
const PALETTE_OPTIONS = [
  { value: "onyx", label: "Onyx", swatch: ["#0a0a0c", "#5fe8a0", "#15151a"] },
  { value: "graphite", label: "Graphite", swatch: ["#000000", "#4db8d4", "#121214"] },
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
          fontFamily: "'Bricolage Grotesque',system-ui,sans-serif",
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
  return /* @__PURE__ */ React.createElement(TweaksPanel, { title: "Tweaks" }, /* @__PURE__ */ React.createElement(TweakSection, { label: "Theme" }, /* @__PURE__ */ React.createElement(PaletteSwatches, { value: t.palette, onChange: (v) => setTweak("palette", v) })), /* @__PURE__ */ React.createElement(TweakSection, { label: "Interaction" }, /* @__PURE__ */ React.createElement(
    TweakRadio,
    {
      label: "Console queries",
      value: t.interaction,
      options: [{ value: "cycling", label: "Auto" }, { value: "static", label: "Manual" }],
      onChange: (v) => setTweak("interaction", v)
    }
  )));
}
function Loop() {
  return /* @__PURE__ */ React.createElement("section", { id: "loop", className: "alt", style: { borderTop: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "section-head center" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "How it works"), /* @__PURE__ */ React.createElement("h2", null, "Most tools show you the problem.", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "nable fixes it.")), /* @__PURE__ */ React.createElement("p", null, "Other cost tools just tell you the bill went up. nable finds what caused it, writes the fix, and proves the savings, getting smarter about your setup every time.")), /* @__PURE__ */ React.createElement("div", { className: "loop-grid" }, /* @__PURE__ */ React.createElement("div", { className: "loop-step" }, /* @__PURE__ */ React.createElement("div", { className: "loop-n" }, "01"), /* @__PURE__ */ React.createElement("h3", null, "Find the cause"), /* @__PURE__ */ React.createElement("p", null, "It points to the exact change that drove your bill up, down to the day it happened, so you stop digging through dashboards.")), /* @__PURE__ */ React.createElement("div", { className: "loop-step" }, /* @__PURE__ */ React.createElement("div", { className: "loop-n" }, "02"), /* @__PURE__ */ React.createElement("h3", null, "Fix it"), /* @__PURE__ */ React.createElement("p", null, "It writes the fix and waits for your go-ahead. nable never changes anything on its own. You're always in control.")), /* @__PURE__ */ React.createElement("div", { className: "loop-step" }, /* @__PURE__ */ React.createElement("div", { className: "loop-n" }, "03"), /* @__PURE__ */ React.createElement("h3", null, "Prove it"), /* @__PURE__ */ React.createElement("p", null, "After you approve, it checks your next bill to confirm the money was really saved, then learns what works for you and gets smarter every time.")))));
}
function ProofBand() {
  return /* @__PURE__ */ React.createElement("section", { className: "proof-band" }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("p", { className: "proof-line" }, "Every other tool claims a number. ", /* @__PURE__ */ React.createElement("em", null, "nable proves it on your bill"), ", and gets smarter every week.")));
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
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "page-atmos", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("svg", { className: "atmos-svg", width: "100%", height: "100%", preserveAspectRatio: "xMidYMid slice" }, /* @__PURE__ */ React.createElement("defs", null, /* @__PURE__ */ React.createElement("pattern", { id: "natmos", width: "260", height: "260", patternUnits: "userSpaceOnUse" }, /* @__PURE__ */ React.createElement("g", { stroke: "#ffffff", strokeOpacity: "0.05", strokeWidth: "1", fill: "none" }, /* @__PURE__ */ React.createElement("path", { d: "M0 70 H160 V260" }), /* @__PURE__ */ React.createElement("path", { d: "M260 188 H104 V0" }), /* @__PURE__ */ React.createElement("path", { d: "M0 214 H48 V128 H132" })), /* @__PURE__ */ React.createElement("g", { fill: "#4db8d4", fillOpacity: "0.45" }, /* @__PURE__ */ React.createElement("circle", { cx: "160", cy: "70", r: "2.1" }), /* @__PURE__ */ React.createElement("circle", { cx: "104", cy: "188", r: "2.1" }), /* @__PURE__ */ React.createElement("circle", { cx: "132", cy: "128", r: "1.8" })))), /* @__PURE__ */ React.createElement("rect", { width: "100%", height: "100%", fill: "url(#natmos)" }))), /* @__PURE__ */ React.createElement("div", { className: "page-content" }, /* @__PURE__ */ React.createElement(Nav, null), /* @__PURE__ */ React.createElement(Hero, null), /* @__PURE__ */ React.createElement(SeeItWork, { interaction: t.interaction }), /* @__PURE__ */ React.createElement(Loop, null), /* @__PURE__ */ React.createElement(ProofBand, null), /* @__PURE__ */ React.createElement(AiCost, null), /* @__PURE__ */ React.createElement(Connectors, null), /* @__PURE__ */ React.createElement(Architecture, { version }), /* @__PURE__ */ React.createElement(Pricing, null), /* @__PURE__ */ React.createElement(Faq, null), /* @__PURE__ */ React.createElement(FootCta, null), /* @__PURE__ */ React.createElement(Footer, { version }), /* @__PURE__ */ React.createElement(Tweaks, null)));
}
ReactDOM.createRoot(document.getElementById("app")).render(/* @__PURE__ */ React.createElement(App, null));
