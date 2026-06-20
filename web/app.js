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
      fontFamily: "'Bricolage Grotesque',system-ui,sans-serif",
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
      fontFamily: "'Bricolage Grotesque',system-ui,sans-serif"
    } }, "Something went wrong. Try again.")
  );
}
function LogoMark() {
  return /* @__PURE__ */ React.createElement("svg", { width: "26", height: "26", viewBox: "0 0 120 120", className: "mark-img", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("defs", null, /* @__PURE__ */ React.createElement("linearGradient", { id: "nmg", x1: "0", y1: "0", x2: "0", y2: "1" }, /* @__PURE__ */ React.createElement("stop", { offset: "0", stopColor: "#5cc1da" }), /* @__PURE__ */ React.createElement("stop", { offset: "1", stopColor: "#3a9ab6" }))), /* @__PURE__ */ React.createElement("rect", { width: "120", height: "120", rx: "27", fill: "url(#nmg)" }), /* @__PURE__ */ React.createElement("path", { d: "M44 80 L44 56 A16 16 0 0 1 76 56 L76 80", fill: "none", stroke: "#0d0f10", strokeWidth: "13", strokeLinecap: "round", strokeLinejoin: "round" }));
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
  return /* @__PURE__ */ React.createElement("nav", { className: "nav" }, /* @__PURE__ */ React.createElement("div", { className: "nav-inner" }, /* @__PURE__ */ React.createElement("a", { href: "/", className: "logo" }, /* @__PURE__ */ React.createElement(LogoMark, null), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("span", { style: { color: "var(--accent)" } }, "n"), "able")), /* @__PURE__ */ React.createElement("ul", null, /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("button", { className: "nav-link", onClick: () => scrollTo("connectors") }, "Connectors")), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("button", { className: "nav-link", onClick: () => scrollTo("pricing") }, "Pricing")), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("button", { className: "nav-link", onClick: () => {
    scrollTo("faq");
    if (window.posthog) posthog.capture("nav_clicked", { item: "faq" });
  } }, "FAQ")), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("a", { href: "/docs.html", onClick: () => {
    if (window.posthog) posthog.capture("docs_clicked", { location: "nav" });
  } }, "Docs")), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "https://github.com/chaandannn/finopsmcp",
      target: "_blank",
      rel: "noopener noreferrer",
      onClick: () => {
        if (window.posthog) posthog.capture("nav_clicked", { item: "github" });
      }
    },
    "GitHub"
  ))), /* @__PURE__ */ React.createElement("div", { className: "right" }, /* @__PURE__ */ React.createElement("a", { href: "/account.html", className: "nav-signin" }, "Sign in"), /* @__PURE__ */ React.createElement(
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
  )), open && /* @__PURE__ */ React.createElement("div", { className: "nav-mobile-menu" }, /* @__PURE__ */ React.createElement("button", { className: "nav-mobile-item", onClick: () => scrollTo("connectors") }, "Connectors"), /* @__PURE__ */ React.createElement("button", { className: "nav-mobile-item", onClick: () => {
    scrollTo("pricing");
    if (window.posthog) posthog.capture("nav_clicked", { item: "pricing" });
  } }, "Pricing"), /* @__PURE__ */ React.createElement("button", { className: "nav-mobile-item", onClick: () => {
    scrollTo("faq");
    if (window.posthog) posthog.capture("nav_clicked", { item: "faq" });
  } }, "FAQ"), /* @__PURE__ */ React.createElement("a", { className: "nav-mobile-item", href: "/docs.html", onClick: () => {
    setOpen(false);
    if (window.posthog) posthog.capture("docs_clicked", { location: "nav_mobile" });
  } }, "Docs"), /* @__PURE__ */ React.createElement(
    "a",
    {
      className: "nav-mobile-item",
      href: "https://github.com/chaandannn/finopsmcp",
      target: "_blank",
      rel: "noopener noreferrer",
      onClick: () => {
        setOpen(false);
        if (window.posthog) posthog.capture("nav_clicked", { item: "github" });
      }
    },
    "GitHub"
  ), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 24, display: "flex", flexDirection: "column", gap: 10 } }, /* @__PURE__ */ React.createElement("a", { href: "/account.html", className: "btn btn-ghost", style: { justifyContent: "center" }, onClick: () => setOpen(false) }, "Sign in"), /* @__PURE__ */ React.createElement(
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
function Hero({ layout, interaction }) {
  const [showInstall, setShowInstall] = useState(false);
  return /* @__PURE__ */ React.createElement("header", { className: "hero " + (layout === "editorial" ? "editorial" : ""), id: "top" }, /* @__PURE__ */ React.createElement("div", { className: "hero-grid-bg" }), /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "hero-inner" }, /* @__PURE__ */ React.createElement("div", { className: "hero-left" }, /* @__PURE__ */ React.createElement("h1", { className: "display" }, "Stop guessing why cloud costs went up. Ask."), /* @__PURE__ */ React.createElement("p", { className: "lede" }, "Connect AWS, Azure, GCP, Datadog, Snowflake, and more. Get answers, anomalies, and savings opportunities, without sending your billing data to another vendor."), /* @__PURE__ */ React.createElement("div", { className: "hero-cta-row" }, /* @__PURE__ */ React.createElement("button", { className: "btn btn-primary", onClick: () => {
    setShowInstall(true);
    if (window.posthog) posthog.capture("cta_clicked", { location: "hero", cta: "start_free" });
  } }, "Start free ", /* @__PURE__ */ React.createElement("span", { className: "arr" }, "\u2192")), /* @__PURE__ */ React.createElement("a", { className: "btn btn-ghost", href: "https://calendar.app.google/2duYBqjLXaTmX5xC8", target: "_blank", rel: "noopener noreferrer", onClick: () => {
    if (window.posthog) posthog.capture("cta_clicked", { location: "hero", cta: "book_demo" });
  } }, "Book a demo")), showInstall && /* @__PURE__ */ React.createElement("div", { className: "hero-install-reveal" }, /* @__PURE__ */ React.createElement(InstallRow, null), /* @__PURE__ */ React.createElement("p", { className: "install-note" }, "Free for solo use, no credit card \xB7 ", /* @__PURE__ */ React.createElement("a", { href: "/docs.html#install", onClick: () => {
    if (window.posthog) posthog.capture("cta_clicked", { location: "hero", cta: "docs_install" });
  } }, "VS Code, Windsurf, Zed and more"), " \xB7 ", /* @__PURE__ */ React.createElement("a", { href: "https://calendar.app.google/2duYBqjLXaTmX5xC8", target: "_blank", rel: "noopener noreferrer", onClick: () => {
    if (window.posthog) posthog.capture("cta_clicked", { location: "hero", cta: "book_demo" });
  } }, "or book a live demo"))), /* @__PURE__ */ React.createElement(TrustStrip, null), /* @__PURE__ */ React.createElement("div", { className: "hero-mobile-cta" }, /* @__PURE__ */ React.createElement("div", { className: "mini-console", "aria-label": "Example: nable answering a cost question" }, /* @__PURE__ */ React.createElement("div", { className: "mc-bar" }, /* @__PURE__ */ React.createElement("span", { className: "mc-dots" }, /* @__PURE__ */ React.createElement("i", null), /* @__PURE__ */ React.createElement("i", null), /* @__PURE__ */ React.createElement("i", null)), /* @__PURE__ */ React.createElement("span", { className: "mc-title" }, "claude \xB7 mcp[nable]")), /* @__PURE__ */ React.createElement("div", { className: "mc-body" }, /* @__PURE__ */ React.createElement("div", { className: "mc-row" }, /* @__PURE__ */ React.createElement("span", { className: "mc-who" }, "YOU"), /* @__PURE__ */ React.createElement("span", null, "Where are we wasting money on EC2?")), /* @__PURE__ */ React.createElement("div", { className: "mc-row" }, /* @__PURE__ */ React.createElement("span", { className: "mc-who mc-n" }, "NABLE"), /* @__PURE__ */ React.createElement("span", null, "11 instances under 15% CPU for 14 days. Rightsizing them saves ", /* @__PURE__ */ React.createElement("b", { className: "mc-save" }, "$1,840/mo"), ". Want the PR?")))), /* @__PURE__ */ React.createElement("p", { className: "hmc-lead" }, "nable sets up in your terminal, so do it on your laptop. Drop your email and we'll send the 60-second setup guide."), /* @__PURE__ */ React.createElement(EmailCapture, { source: "hero_mobile", placeholder: "your@email.com", btnLabel: "Get the guide" }), /* @__PURE__ */ React.createElement("div", { className: "hmc-links" }, /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "/demo.html",
      className: "hmc-pro",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "hero_mobile", cta: "full_demo" });
      }
    },
    "Ask the live demo ",
    /* @__PURE__ */ React.createElement("span", { className: "arr" }, "\u2192")
  ), /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "#pricing",
      className: "hmc-pro",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "hero_mobile", cta: "pricing_40" });
      }
    },
    "See Team \xB7 $1,000/mo flat ",
    /* @__PURE__ */ React.createElement("span", { className: "arr" }, "\u2192")
  ), /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "https://calendar.app.google/2duYBqjLXaTmX5xC8",
      target: "_blank",
      rel: "noopener noreferrer",
      className: "hmc-book",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "hero_mobile", cta: "book_demo" });
      }
    },
    "Book a live demo ",
    /* @__PURE__ */ React.createElement("span", { className: "arr" }, "\u2192")
  )))), /* @__PURE__ */ React.createElement("div", { className: "hero-right" }, /* @__PURE__ */ React.createElement(Console, { interaction })))));
}
const CURSOR_DEEPLINK = "cursor://anysphere.cursor-deeplink/mcp/install?name=nable&config=eyJjb21tYW5kIjogInV2eCIsICJhcmdzIjogWyItLXB5dGhvbiIsICIzLjEyIiwgImZpbm9wcy1tY3AiXX0=";
const INSTALL_POPUPS = {
  claude: {
    title: "Install in Claude Desktop",
    steps: [
      /* @__PURE__ */ React.createElement(React.Fragment, null, "In your terminal, run the command below. ", /* @__PURE__ */ React.createElement("code", null, "finops welcome"), " writes your Claude Desktop config and stores credentials in your OS keychain."),
      /* @__PURE__ */ React.createElement(React.Fragment, null, "Restart Claude Desktop. nable connects as a local MCP server.")
    ],
    cmdLabel: "In your terminal",
    cmd: "uvx finops-mcp",
    altCmd: "pip install -U finops-mcp && finops welcome",
    note: "uv installs a matching Python for you, so this works on any setup. No uv? brew install uv. Runs on your machine, no nable backend."
  },
  openai: {
    title: "Install in OpenAI Codex",
    steps: [
      /* @__PURE__ */ React.createElement(React.Fragment, null, "In your terminal, install nable and store credentials in your OS keychain:"),
      /* @__PURE__ */ React.createElement(React.Fragment, null, "Add nable to your Codex MCP config below, then restart Codex.")
    ],
    cmdLabel: "In your terminal",
    cmd: "uvx finops-mcp",
    altCmd: "pip install -U finops-mcp && finops welcome",
    toml: '[mcp_servers.nable]\ncommand = "uvx"\nargs = ["--python", "3.12", "finops-mcp"]',
    tomlPath: "~/.codex/config.toml",
    note: "uv installs a matching Python automatically. The ChatGPT app needs a hosted connector, on the roadmap."
  }
};
function CopyCmd({ cmd }) {
  const [copied, setCopied] = useState(false);
  return /* @__PURE__ */ React.createElement("button", { className: "copycmd", onClick: () => {
    navigator.clipboard?.writeText(cmd);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
    if (window.posthog) posthog.capture("install_copied");
  } }, /* @__PURE__ */ React.createElement("span", { className: "prompt" }, "$"), /* @__PURE__ */ React.createElement("span", { className: "cmd" }, cmd), /* @__PURE__ */ React.createElement("span", { className: "copylab" }, copied ? "copied" : "copy"));
}
function InstallPopup({ id, onClose }) {
  const p = INSTALL_POPUPS[id];
  if (!p) return null;
  return /* @__PURE__ */ React.createElement("div", { className: "install-pop", role: "dialog", "aria-label": p.title }, /* @__PURE__ */ React.createElement("div", { className: "install-pop-head" }, /* @__PURE__ */ React.createElement("span", { className: "ipt" }, p.title), /* @__PURE__ */ React.createElement("button", { className: "ipx", onClick: onClose, "aria-label": "Close" }, "\xD7")), /* @__PURE__ */ React.createElement("ol", { className: "install-steps" }, p.steps.map((s, i) => /* @__PURE__ */ React.createElement("li", { key: i }, s))), p.cmdLabel && /* @__PURE__ */ React.createElement("span", { className: "install-cmdlabel" }, /* @__PURE__ */ React.createElement("svg", { width: "12", height: "12", viewBox: "0 0 12 12", fill: "none", stroke: "currentColor", strokeWidth: "1.5", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("path", { d: "M2.5 3.5L5 6l-2.5 2.5M6.5 8.5h3", strokeLinecap: "round", strokeLinejoin: "round" })), p.cmdLabel), /* @__PURE__ */ React.createElement(CopyCmd, { cmd: p.cmd }), p.altCmd && /* @__PURE__ */ React.createElement("p", { className: "install-alt" }, "Already on Python 3.10+? ", /* @__PURE__ */ React.createElement("code", null, p.altCmd)), p.toml && /* @__PURE__ */ React.createElement("div", { className: "install-toml" }, /* @__PURE__ */ React.createElement("span", { className: "tomlpath" }, "Add to ", /* @__PURE__ */ React.createElement("code", null, p.tomlPath)), /* @__PURE__ */ React.createElement("pre", null, p.toml)), /* @__PURE__ */ React.createElement("p", { className: "install-pop-note" }, p.note));
}
const _CHEV = /* @__PURE__ */ React.createElement("svg", { className: "chev", width: "12", height: "12", viewBox: "0 0 12 12", fill: "none", stroke: "currentColor", strokeWidth: "1.6", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("path", { d: "M3 4.5l3 3 3-3", strokeLinecap: "round", strokeLinejoin: "round" }));
function InstallRow() {
  const [open, setOpen] = useState(null);
  const toggle = (id) => {
    setOpen((o) => o === id ? null : id);
    if (window.posthog) posthog.capture("install_opened", { client: id });
  };
  return /* @__PURE__ */ React.createElement("div", { className: "installer", id: "install" }, /* @__PURE__ */ React.createElement("span", { className: "install-cmdlabel" }, /* @__PURE__ */ React.createElement("svg", { width: "12", height: "12", viewBox: "0 0 12 12", fill: "none", stroke: "currentColor", strokeWidth: "1.5", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("path", { d: "M2.5 3.5L5 6l-2.5 2.5M6.5 8.5h3", strokeLinecap: "round", strokeLinejoin: "round" })), "Run this in your terminal"), /* @__PURE__ */ React.createElement(CopyCmd, { cmd: "uvx finops-mcp" }), /* @__PURE__ */ React.createElement("div", { className: "install-row" }, /* @__PURE__ */ React.createElement(
    "a",
    {
      className: "iclient is-primary",
      href: CURSOR_DEEPLINK,
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "hero", cta: "add_to_cursor" });
      }
    },
    /* @__PURE__ */ React.createElement("span", null, "Install in ", /* @__PURE__ */ React.createElement("b", null, "Cursor")),
    /* @__PURE__ */ React.createElement("svg", { className: "ic", width: "12", height: "12", viewBox: "0 0 12 12", fill: "none", stroke: "currentColor", strokeWidth: "1.6", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("path", { d: "M4 8l4-4m0 0H4.5M8 4v3.5", strokeLinecap: "round", strokeLinejoin: "round" }))
  ), /* @__PURE__ */ React.createElement("button", { className: "iclient" + (open === "claude" ? " is-open" : ""), "aria-expanded": open === "claude", onClick: () => toggle("claude") }, /* @__PURE__ */ React.createElement("span", null, "Install in ", /* @__PURE__ */ React.createElement("b", null, "Claude")), _CHEV), /* @__PURE__ */ React.createElement("button", { className: "iclient" + (open === "openai" ? " is-open" : ""), "aria-expanded": open === "openai", onClick: () => toggle("openai") }, /* @__PURE__ */ React.createElement("span", null, "Install in ", /* @__PURE__ */ React.createElement("b", null, "OpenAI")), _CHEV)), open && /* @__PURE__ */ React.createElement(InstallPopup, { id: open, onClose: () => setOpen(null) }));
}
function fmtNum(n) {
  if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
  return String(n);
}
function TrustStrip() {
  const items = [
    { lab: "built-in tools", val: "160+", sub: "cost, anomaly, rightsizing" },
    { lab: "providers", val: "17", sub: "AWS \xB7 Azure \xB7 GCP +" },
    { lab: "on our servers", val: "0 bytes", sub: "nable has no backend" }
  ];
  return /* @__PURE__ */ React.createElement("div", { className: "trust" }, items.map((t, i) => /* @__PURE__ */ React.createElement("div", { className: "ti", key: i }, /* @__PURE__ */ React.createElement("span", { className: "lab" }, t.lab), /* @__PURE__ */ React.createElement("span", { className: "val mono" }, t.val))));
}
const QUERIES = [
  {
    q: "Where are we wasting money on EC2?",
    response: /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, "Cross-referenced CloudWatch metrics with Compute Optimizer. 11 instances are sustained below 15% CPU over 14 days. Top four by savings:"), /* @__PURE__ */ React.createElement("div", { className: "ttable" }, /* @__PURE__ */ React.createElement("div", { className: "r hd" }, /* @__PURE__ */ React.createElement("span", null, "Instance / current"), /* @__PURE__ */ React.createElement("span", null, "Recommended"), /* @__PURE__ */ React.createElement("span", null, "Save / mo")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "i-0a3f \xB7 m5.4xlarge"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "m6i.xlarge"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "$412")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "i-0c91 \xB7 r5.2xlarge"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "r6i.large"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "$298")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "i-0e7d \xB7 m5.2xlarge"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "m6i.large"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "$201")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "i-0b44 \xB7 c5.4xlarge"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "c6i.xlarge"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "$184")), /* @__PURE__ */ React.createElement("div", { className: "r total" }, /* @__PURE__ */ React.createElement("span", null, "11 instances"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "-"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "$1,840 / mo"))), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 12 } }, "Net annualized savings: ", /* @__PURE__ */ React.createElement("span", { style: { color: "var(--accent)" } }, "$22,080"), ". Generate PRs against your IaC repo?"))
  },
  {
    q: "Compute spend across all providers, April vs March.",
    response: /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, "Normalized to USD across the three clouds. Pulled from each provider's billing API just now."), /* @__PURE__ */ React.createElement("div", { className: "ttable" }, /* @__PURE__ */ React.createElement("div", { className: "r hd" }, /* @__PURE__ */ React.createElement("span", null, "Provider \xB7 service"), /* @__PURE__ */ React.createElement("span", null, "April"), /* @__PURE__ */ React.createElement("span", null, "delta MoM")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "AWS \xB7 EC2 + Fargate"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$18,420"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+18.6%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "Azure \xB7 Virtual Machines"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$6,310"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+4.2%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "GCP \xB7 Compute Engine"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$4,870"), /* @__PURE__ */ React.createElement("span", { className: "d down num" }, "-3.4%")), /* @__PURE__ */ React.createElement("div", { className: "r total" }, /* @__PURE__ */ React.createElement("span", null, "Total compute"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$29,600"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+12.0%"))), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 14 } }, "Three new ", /* @__PURE__ */ React.createElement("span", { className: "mono", style: { color: "var(--fg)" } }, "c6i.4xlarge"), " in ", /* @__PURE__ */ React.createElement("span", { className: "mono", style: { color: "var(--fg)" } }, "us-east-1"), " account for $1,890 of the AWS delta. Want me to tag them and open an audit ticket?"))
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
  },
  {
    q: "What's our AI and LLM bill?",
    response: /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, "Token spend across your model providers this month, normalized to USD:"), /* @__PURE__ */ React.createElement("div", { className: "ttable" }, /* @__PURE__ */ React.createElement("div", { className: "r hd" }, /* @__PURE__ */ React.createElement("span", null, "Provider \xB7 model"), /* @__PURE__ */ React.createElement("span", null, "Spend"), /* @__PURE__ */ React.createElement("span", null, "delta MoM")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "OpenAI \xB7 gpt-4o"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$4,120"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+34%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "Anthropic \xB7 Claude"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$2,880"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+21%")), /* @__PURE__ */ React.createElement("div", { className: "r" }, /* @__PURE__ */ React.createElement("span", null, "AWS \xB7 Bedrock"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$1,610"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+12%")), /* @__PURE__ */ React.createElement("div", { className: "r total" }, /* @__PURE__ */ React.createElement("span", null, "Total AI / LLM"), /* @__PURE__ */ React.createElement("span", { className: "v num" }, "$8,610"), /* @__PURE__ */ React.createElement("span", { className: "d up num" }, "+26%"))), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 12 } }, "Your token bill is up ", /* @__PURE__ */ React.createElement("span", { style: { color: "var(--alert)" } }, "26%"), " even as per-token prices fell. A gpt-4o classifier is the driver; route it to a cheaper model to save about ", /* @__PURE__ */ React.createElement("span", { style: { color: "var(--accent)" } }, "$1,400 / mo"), "."))
  }
];
function matchQuery(text) {
  const t = (text || "").toLowerCase();
  if (/what can|capabilit|what do you do|what does nable|use case|everything you|all the tools|how does (this|it) work|what should i ask/.test(t)) return 4;
  if (/\bai\b|llm|token|openai|anthropic|claude|bedrock|\bgpt|inference|model (spend|cost|bill)/.test(t)) return 5;
  if (/discount|savings ?plan|reserved|reservation|\bri\b|commitment|coverage|effective (rate|discount)|\bcud/.test(t)) return 3;
  if (/anomal|spike|spiking|surge|unusual|datadog|went up|going up|jump/.test(t)) return 2;
  if (/across (all )?(provider|cloud)|all providers|multi-?cloud|aws.*(vs|versus|and).*(azure|gcp)|month.?over.?month|\bmom\b|april.*march|march.*april|compute.*(across|provider|month|vs|versus)/.test(t)) return 1;
  if (/ec2|wast|idle|rightsiz|right-?siz|over-?provision|low cpu|cut (cost|spend)|save money|saving money|where can i save|trim/.test(t)) return 0;
  return -1;
}
const GATE = /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, "That's exactly the kind of question nable answers against your ", /* @__PURE__ */ React.createElement("b", null, "own"), " account, with your real numbers. This demo only knows the sample account above."), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 12 } }, "Connect it in about a minute, then ask away on your real bill. It runs on your machine, nothing leaves it:"), /* @__PURE__ */ React.createElement("div", { className: "gate-cmd" }, /* @__PURE__ */ React.createElement(CopyCmd, { cmd: "uvx finops-mcp" })), /* @__PURE__ */ React.createElement("p", { className: "gate-sub" }, "Free for solo use, no signup. Runs on your machine."));
const OFFTOPIC = /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("p", null, "That one's outside this demo. nable here only covers cloud and AI cost, so ask about your AWS, Azure, GCP, Kubernetes or AI spend, or try a prompt below."));
const FINANCE_RE = /cost|spend|bill|budget|forecast|sav(e|ing)|money|cheap|expensive|pric(e|ing)|discount|invoice|usage|waste|idle|optimi[sz]e|rightsiz|reserved|reservation|commitment|anomal|cloud|aws|azure|gcp|ec2|\bs3\b|rds|lambda|fargate|eks|kubernetes|k8s|container|cluster|instance|\bvm\b|server|database|storage|snowflake|databricks|datadog|gpu|\bai\b|llm|token|openai|anthropic|claude|bedrock|gpt|\bmodel\b|provider|region|account|\btag|dollar|\$/;
const CHIPS = [
  { label: "What can you do?", idx: 4 },
  { label: "Where's the EC2 waste?", idx: 0 },
  { label: "What's our AI bill?", idx: 5 },
  { label: "Any anomalies this week?", idx: 2 },
  { label: "Effective discount rate?", idx: 3 }
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
  const firstRun = useRef(true);
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
    if (firstRun.current) {
      firstRun.current = false;
      return;
    }
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
function Thesis() {
  const cards = [
    { n: "01 \xB7 TAM", h: "Cloud spend is the #2 line item in modern software.", p: "$700B+ annual cloud + SaaS spend, growing 18% YoY. Every dollar is unaccountable until someone reconciles 8 dashboards and a CSV. That reconciliation work is the wedge." },
    { n: "02 \xB7 Shift", h: "FinOps moved from a quarterly review to a real-time question.", p: 'AI editors made conversational access to live data the default interface. Asking "what spiked" is now cheaper than building a dashboard. The dashboard era is the legacy era.' },
    { n: "03 \xB7 Moat", h: "Local-first compounds with every connector.", p: "Credentials in the OS keyring. No data lake. No SOC-2 surface area. Each new connector is a feature shipment, not a security review. Enterprise sells itself." }
  ];
  return /* @__PURE__ */ React.createElement("section", { id: "thesis" }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "section-head" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "Thesis"), /* @__PURE__ */ React.createElement("h2", null, "The dashboard ", /* @__PURE__ */ React.createElement("em", null, "was"), " the product.", /* @__PURE__ */ React.createElement("br", null), "The interface is the product now."), /* @__PURE__ */ React.createElement("p", null, "Three forces converge in 2026. nable is the runtime where they meet.")), /* @__PURE__ */ React.createElement("div", { className: "thesis" }, cards.map((c, i) => /* @__PURE__ */ React.createElement("div", { className: "thesis-card", key: i }, /* @__PURE__ */ React.createElement("span", { className: "n" }, c.n), /* @__PURE__ */ React.createElement("h3", null, c.h), /* @__PURE__ */ React.createElement("p", null, c.p))))));
}
function Depth() {
  const cards = [
    {
      n: "01",
      h: "Your biggest savings, in one question.",
      p: "Ask 'where am I wasting money?' and get a ranked list of every opportunity across your infrastructure, sorted by dollar impact. No dashboard to configure. No report to schedule. No knowing what to look for. Just results.",
      chips: ["ranked by $", "works day one", "no setup", "19 scanners"]
    },
    {
      n: "02",
      h: "From recommendation to merged PR.",
      p: "Most tools stop at 'you should downsize that.' nable reads your Terraform, patches the file, and opens the pull request. After it merges, nable checks whether the saving actually landed and records the realized amount.",
      chips: ["Terraform", "PR opened", "saving verified", "end-to-end"]
    },
    {
      n: "03",
      h: "AI spend tracked like a first-class cost.",
      p: "Bedrock, OpenAI, Anthropic. These don't fit in the usual cost buckets. nable tracks AI spend by model and by team, so it shows up as a first-class line in every report instead of a mystery lump buried in the bill.",
      chips: ["by model", "by team", "first-class", "AI-native"]
    },
    {
      n: "04",
      h: "Always-on, or on demand.",
      p: "Ask in your editor whenever you want, or run `finops serve` for always-on monitoring that catches spikes 24/7. When spend jumps, nable attributes the anomaly to the team or service that caused it and alerts whoever owns it in Slack or Teams. Before finance notices.",
      chips: ["always-on or on-demand", "team attribution", "Slack / Teams", "28-day baseline"]
    }
  ];
  return /* @__PURE__ */ React.createElement("section", { id: "depth", style: { borderTop: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "section-head" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "What's under the hood"), /* @__PURE__ */ React.createElement("h2", null, "Not a pipe.", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "An analyst.")), /* @__PURE__ */ React.createElement("p", null, "The value isn't connecting Claude to your bill. It's the analysis that runs before Claude ever responds.")), /* @__PURE__ */ React.createElement("div", { className: "depth-grid" }, cards.map((c, i) => /* @__PURE__ */ React.createElement("div", { className: "depth-card", key: i }, /* @__PURE__ */ React.createElement("span", { className: "depth-n" }, c.n), /* @__PURE__ */ React.createElement("h3", { className: "depth-h" }, c.h), /* @__PURE__ */ React.createElement("p", { className: "depth-p" }, c.p), /* @__PURE__ */ React.createElement("div", { className: "depth-chips" }, c.chips.map((ch, j) => /* @__PURE__ */ React.createElement("span", { key: j }, ch))))))));
}
function AiCost() {
  const copy = () => {
    if (navigator.clipboard) navigator.clipboard.writeText("uvx finops-mcp");
    if (window.posthog) posthog.capture("cta_clicked", { location: "ai_cost", cta: "copy_install" });
  };
  return /* @__PURE__ */ React.createElement("section", { id: "ai", className: "alt", style: { borderTop: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "ee-grid" }, /* @__PURE__ */ React.createElement("div", { className: "ee-left" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "Your AI bill"), /* @__PURE__ */ React.createElement("h2", null, "Tools chart your AI spend.", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "nable finds the waste.")), /* @__PURE__ */ React.createElement("p", { className: "ee-lede" }, "Most of an AI bill is input tokens billed at full price, plus calls sent to a frontier model a cheaper one would have answered the same way. nable reads the split from your real usage and shows you the cheapest way to get the same output. No caching guesswork."), /* @__PURE__ */ React.createElement("ul", { className: "ee-points" }, /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("span", { className: "ee-plus" }, "+"), /* @__PURE__ */ React.createElement("span", null, "Input, output and cache, ", /* @__PURE__ */ React.createElement("b", null, "split from your actual bill"))), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("span", { className: "ee-plus" }, "+"), /* @__PURE__ */ React.createElement("span", null, "Flags ", /* @__PURE__ */ React.createElement("b", null, "frontier-model calls"), " a cheaper model handles the same")), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("span", { className: "ee-plus" }, "+"), /* @__PURE__ */ React.createElement("span", null, "Separates ", /* @__PURE__ */ React.createElement("b", null, "what you can bank today"), " from what needs a closer look")))), /* @__PURE__ */ React.createElement("div", { className: "ee-right" }, /* @__PURE__ */ React.createElement("div", { className: "aicost-panel" }, /* @__PURE__ */ React.createElement("div", { className: "aicost-tag" }, "Real numbers \xB7 real dollars \xB7 first scan"), /* @__PURE__ */ React.createElement("div", { className: "aicost-stat" }, /* @__PURE__ */ React.createElement("div", { className: "aicost-big" }, "89", /* @__PURE__ */ React.createElement("span", { className: "aicost-unit" }, "%")), /* @__PURE__ */ React.createElement("p", null, "of an early user's Bedrock bill was input tokens, billed at full price with ", /* @__PURE__ */ React.createElement("b", null, "no caching"))), /* @__PURE__ */ React.createElement("div", { className: "aicost-rule" }), /* @__PURE__ */ React.createElement("div", { className: "aicost-stat" }, /* @__PURE__ */ React.createElement("div", { className: "aicost-big accent" }, "$10.7k", /* @__PURE__ */ React.createElement("span", { className: "aicost-unit" }, "/yr")), /* @__PURE__ */ React.createElement("p", null, /* @__PURE__ */ React.createElement("b", null, "= $896/mo"), " in prompt-caching savings, about a quarter of the AI bill, on the first scan")), /* @__PURE__ */ React.createElement("div", { className: "aicost-foot" }, "From an early user's first scan. Real numbers, name withheld for now."), /* @__PURE__ */ React.createElement("div", { className: "aicost-cta" }, /* @__PURE__ */ React.createElement("span", { className: "aicost-cta-l" }, "This is a small account. See your own number, free:"), /* @__PURE__ */ React.createElement("code", { className: "aicost-cmd", onClick: copy }, "uvx finops-mcp")))))));
}
function Architecture({ version }) {
  return /* @__PURE__ */ React.createElement("section", { id: "arch", className: "alt" }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "section-head" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "Architecture"), /* @__PURE__ */ React.createElement("h2", null, "No vendor backend,", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "by design.")), /* @__PURE__ */ React.createElement("p", null, "nable is not SaaS. It runs on your own machine, holds credentials in the OS keyring, queries provider APIs directly, and surfaces tools to whichever AI editor is open. Your credentials never leave your machine, and your cost data never touches a nable server, there is no backend or data lake to breach. The figures you ask about go to your editor's own AI to answer the question, the same as any prompt, and nowhere else.")), /* @__PURE__ */ React.createElement("div", { className: "arch" }, /* @__PURE__ */ React.createElement("div", { className: "arch-grid" }), /* @__PURE__ */ React.createElement("div", { className: "arch-row" }, /* @__PURE__ */ React.createElement("div", { className: "arch-col" }, /* @__PURE__ */ React.createElement("span", { className: "lab" }, "your editor"), /* @__PURE__ */ React.createElement("div", { className: "arch-node" }, /* @__PURE__ */ React.createElement("h4", null, "Claude \xB7 Cursor \xB7 Zed"), /* @__PURE__ */ React.createElement("span", { className: "sub" }, "MCP client"), /* @__PURE__ */ React.createElement("div", { className: "chips" }, /* @__PURE__ */ React.createElement("span", null, "tools/list"), /* @__PURE__ */ React.createElement("span", null, "tools/call")))), /* @__PURE__ */ React.createElement("div", { className: "arch-arrow" }, /* @__PURE__ */ React.createElement("span", null, "stdio"), /* @__PURE__ */ React.createElement("span", { className: "line" }), /* @__PURE__ */ React.createElement("span", null, "jsonrpc")), /* @__PURE__ */ React.createElement("div", { className: "arch-col" }, /* @__PURE__ */ React.createElement("span", { className: "lab" }, "runtime \xB7 local"), /* @__PURE__ */ React.createElement("div", { className: "arch-node center" }, /* @__PURE__ */ React.createElement("h4", null, "nable runtime"), /* @__PURE__ */ React.createElement("span", { className: "sub" }, "nable"), /* @__PURE__ */ React.createElement("div", { className: "chips" }, /* @__PURE__ */ React.createElement("span", null, "keyring"), /* @__PURE__ */ React.createElement("span", null, "fernet"), /* @__PURE__ */ React.createElement("span", null, "read-only"), /* @__PURE__ */ React.createElement("span", null, "audit-log")))), /* @__PURE__ */ React.createElement("div", { className: "arch-arrow" }, /* @__PURE__ */ React.createElement("span", null, "https"), /* @__PURE__ */ React.createElement("span", { className: "line" }), /* @__PURE__ */ React.createElement("span", null, "signed")), /* @__PURE__ */ React.createElement("div", { className: "arch-col" }, /* @__PURE__ */ React.createElement("span", { className: "lab" }, "provider apis"), /* @__PURE__ */ React.createElement("div", { className: "arch-node" }, /* @__PURE__ */ React.createElement("h4", null, "17 connectors"), /* @__PURE__ */ React.createElement("span", { className: "sub" }, "cost \xB7 usage \xB7 billing"), /* @__PURE__ */ React.createElement("div", { className: "chips" }, /* @__PURE__ */ React.createElement("span", null, "AWS CE/CUR"), /* @__PURE__ */ React.createElement("span", null, "Azure CM"), /* @__PURE__ */ React.createElement("span", null, "GCP BQ"), /* @__PURE__ */ React.createElement("span", null, "+14"))))))));
}
const STEPS = [
  { n: "01", h: "Connect", p: "Point nable at AWS, Azure, GCP and 14 more sources. Credentials land in your OS keyring, never on our servers.", ex: "finops setup aws" },
  { n: "02", h: "Ask", p: "Open Claude, Cursor, or any MCP editor and just ask. nable turns the question into live, read-only API calls.", ex: '"What drove our bill up last week?"' },
  { n: "03", h: "Act", p: "Approve a rightsizing PR, open a ticket, post to Slack. Answers become actions, every one written to an audit log.", ex: '"Open a PR to downsize the idle instances."' }
];
function HowItWorks() {
  return /* @__PURE__ */ React.createElement("section", { id: "how", style: { borderTop: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "section-head" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "How it works"), /* @__PURE__ */ React.createElement("h2", null, "Live in ", /* @__PURE__ */ React.createElement("em", null, "four minutes.")), /* @__PURE__ */ React.createElement("p", null, "No data pipeline. No dashboard to build. A single MCP entry turns any AI editor into a FinOps console.")), /* @__PURE__ */ React.createElement("div", { className: "steps" }, STEPS.map((s, i) => /* @__PURE__ */ React.createElement("div", { className: "step", key: i }, /* @__PURE__ */ React.createElement("div", { className: "step-n" }, s.n), /* @__PURE__ */ React.createElement("h3", { className: "step-h" }, s.h), /* @__PURE__ */ React.createElement("p", { className: "step-p" }, s.p), /* @__PURE__ */ React.createElement("div", { className: "step-ex" }, s.ex))))));
}
const EDITOR_TABS = [
  { id: "terminal", label: "Terminal", bar: "bash", lines: [
    { k: "cmd", t: "$ uvx finops-mcp" },
    { k: "dim", t: "  fetching finops-mcp + a matching python\u2026" },
    { k: "ok", t: "\u2713 runtime registered \xB7 ask nable in your editor" }
  ] },
  { id: "claudecode", label: "Claude Code", bar: "terminal claude cli \xB7 /plugin", lines: [
    { k: "dim", t: "# in the terminal claude cli, run one at a time" },
    { k: "cmd", t: "/plugin marketplace add chaandannn/finopsmcp" },
    { k: "cmd", t: "/plugin install nable@nable" },
    { k: "ok", t: "\u2713 nable installed \xB7 ask in your editor" }
  ] },
  { id: "claude", label: "Claude Desktop", bar: "claude_desktop_config.json", lines: [
    { k: "p", t: "{" },
    { k: "p", t: '  "mcpServers": {' },
    { k: "p", t: '    "nable": {' },
    { k: "p", t: '      "command": "uvx",' },
    { k: "p", t: '      "args": ["--python", "3.12", "finops-mcp"]' },
    { k: "p", t: "    }" },
    { k: "p", t: "  }" },
    { k: "p", t: "}" }
  ] },
  { id: "cursor", label: "Cursor", bar: "~/.cursor/mcp.json", lines: [
    { k: "p", t: "{" },
    { k: "p", t: '  "mcpServers": {' },
    { k: "p", t: '    "nable": { "command": "uvx", "args": ["--python", "3.12", "finops-mcp"] }' },
    { k: "p", t: "  }" },
    { k: "p", t: "}" }
  ] }
];
function EveryEditor() {
  const [tab, setTab] = useState("terminal");
  const active = EDITOR_TABS.find((t) => t.id === tab) || EDITOR_TABS[0];
  const copy = () => {
    if (navigator.clipboard) navigator.clipboard.writeText(active.lines.map((l) => l.t).join("\n"));
    if (window.posthog) posthog.capture("cta_clicked", { location: "every_editor", cta: "copy_config", tab });
  };
  return /* @__PURE__ */ React.createElement("section", { id: "editors", className: "alt", style: { borderTop: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "ee-grid" }, /* @__PURE__ */ React.createElement("div", { className: "ee-left" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "Runtime"), /* @__PURE__ */ React.createElement("h2", null, "One entry.", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "Every editor.")), /* @__PURE__ */ React.createElement("p", { className: "ee-lede" }, "nable speaks the Model Context Protocol, so the same runtime works in whatever your team already uses. Drop in the config, restart, and ask."), /* @__PURE__ */ React.createElement("ul", { className: "ee-points" }, /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("span", { className: "ee-plus" }, "+"), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("b", null, "160+ tools"), " your AI can call, from a cost question to an open PR")), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("span", { className: "ee-plus" }, "+"), /* @__PURE__ */ React.createElement("span", null, "Tracks ", /* @__PURE__ */ React.createElement("b", null, "AI spend by model"), " alongside cloud, Kubernetes, and SaaS")), /* @__PURE__ */ React.createElement("li", null, /* @__PURE__ */ React.createElement("span", { className: "ee-plus" }, "+"), /* @__PURE__ */ React.createElement("span", null, "Real API integrations, with ", /* @__PURE__ */ React.createElement("b", null, "new connectors every month")))), /* @__PURE__ */ React.createElement("div", { className: "ee-runs" }, "RUNS IN ", /* @__PURE__ */ React.createElement("b", null, "CLAUDE"), " \xB7 ", /* @__PURE__ */ React.createElement("b", null, "CURSOR"), " \xB7 ", /* @__PURE__ */ React.createElement("b", null, "VS CODE"), " \xB7 ", /* @__PURE__ */ React.createElement("b", null, "ZED"), " \xB7 ", /* @__PURE__ */ React.createElement("b", null, "WINDSURF"), " \xB7 ", /* @__PURE__ */ React.createElement("b", null, "CLINE"))), /* @__PURE__ */ React.createElement("div", { className: "ee-right" }, /* @__PURE__ */ React.createElement("div", { className: "ee-panel" }, /* @__PURE__ */ React.createElement("div", { className: "ee-tabs" }, EDITOR_TABS.map((t) => /* @__PURE__ */ React.createElement("button", { key: t.id, className: "ee-tab" + (t.id === tab ? " on" : ""), onClick: () => setTab(t.id) }, t.label))), /* @__PURE__ */ React.createElement("div", { className: "ee-bar" }, /* @__PURE__ */ React.createElement("span", { className: "ee-dots" }, /* @__PURE__ */ React.createElement("i", null), /* @__PURE__ */ React.createElement("i", null), /* @__PURE__ */ React.createElement("i", null)), /* @__PURE__ */ React.createElement("span", { className: "ee-file" }, active.bar), /* @__PURE__ */ React.createElement("span", { className: "ee-copy", onClick: copy }, "copy")), /* @__PURE__ */ React.createElement("pre", { className: "ee-code" }, active.lines.map((l, i) => /* @__PURE__ */ React.createElement("div", { key: i, className: "ee-ln ee-" + l.k }, l.t))))))));
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
const MONTHLY_STRIPE_LINK = "https://buy.stripe.com/3cI3cucid6J85tm3wC2Nq08";
const ANNUAL_STRIPE_LINK = "https://buy.stripe.com/14A6oG0zvgjI9JCffk2Nq09";
const BOOK_CALL_LINK = "https://calendar.app.google/2duYBqjLXaTmX5xC8";
const PRICE_ROWS = [
  { label: "Users", solo: "Just you", pro: "Your whole team", team: "Your whole team", ent: "Your whole org" },
  { label: "Core FinOps: cost queries, anomalies, rightsizing, AI/LLM tracking, 17 connectors, local-first", solo: true, pro: true, team: true, ent: true },
  { label: "AWS cost data", solo: "Cost Explorer", pro: "Explorer + CUR", team: "Explorer + CUR", ent: "Explorer + CUR" },
  { label: "Terraform remediation: patch + open the PR", solo: false, pro: true, team: true, ent: true },
  { label: "Slack / Teams alerts, digests + tickets (Jira, Linear, GitHub)", solo: false, pro: true, team: true, ent: true },
  { label: "Budgets, commitments + BI dashboards", solo: false, pro: true, team: true, ent: true },
  { label: "Slack bot: ask cost questions, no editor needed", solo: false, pro: false, team: true, ent: true },
  { label: "RCA + chat remediation: drafts the fix, a human approves", solo: false, pro: false, team: true, ent: true },
  { label: "Managed AI included (or bring your own key)", solo: false, pro: false, team: true, ent: true },
  { label: "SSO + audit logs", solo: false, pro: false, team: false, ent: true },
  { label: "Support", solo: "Slack", pro: "Slack", team: "Slack", ent: "Slack + SLA" }
];
function PCell({ v }) {
  if (v === true) return /* @__PURE__ */ React.createElement("span", { className: "pcheck" }, /* @__PURE__ */ React.createElement(CheckIcon, null));
  if (v === false) return /* @__PURE__ */ React.createElement("span", { className: "pdash" }, "\u2013");
  return /* @__PURE__ */ React.createElement("span", { className: "pval" }, v);
}
function PricingCards({ annual, proPrice, proPer, proSub, proLink, proPlan, teamPrice, teamPer, teamSub, teamLink, teamPlan }) {
  const tiers = [
    {
      key: "solo",
      name: "Solo",
      price: "Free",
      per: "forever",
      sub: null,
      rec: false,
      primary: false,
      cta: "Start free",
      href: "/docs.html",
      plan: "solo",
      ext: false
    },
    {
      key: "pro",
      name: "Pro",
      price: proPrice,
      per: proPer,
      sub: proSub,
      rec: false,
      primary: false,
      cta: annual ? "Get annual" : "Get Pro",
      href: proLink,
      plan: proPlan,
      ext: true
    },
    {
      key: "team",
      name: "Team",
      price: teamPrice,
      per: teamPer,
      sub: teamSub,
      rec: true,
      primary: true,
      cta: annual ? "Get annual" : "Get Team",
      href: teamLink,
      plan: teamPlan,
      ext: true
    },
    {
      key: "ent",
      name: "Enterprise",
      price: "Custom",
      per: "",
      sub: null,
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
  const proSub = annual ? "$83 / mo \xB7 2 months free" : "7-day free trial";
  const proLink = annual ? PRO_ANNUAL_LINK : PRO_MONTHLY_LINK;
  const proPlan = annual ? "pro_annual" : "pro_monthly";
  const teamPrice = annual ? "$10,000" : "$1,000";
  const teamPer = annual ? "/ yr flat" : "/ mo flat";
  const teamSub = annual ? "$833 / mo \xB7 2 months free" : "7-day free trial";
  const teamLink = annual ? ANNUAL_STRIPE_LINK : MONTHLY_STRIPE_LINK;
  const teamPlan = annual ? "team_annual" : "team_monthly";
  return /* @__PURE__ */ React.createElement("section", { id: "pricing" }, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "section-head" }, /* @__PURE__ */ React.createElement("div", { className: "label" }, "Pricing"), /* @__PURE__ */ React.createElement("h2", null, "Free to ask.", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "Pay to remediate.")), /* @__PURE__ */ React.createElement("p", null, "Solo is free forever. Pro adds the remediation layer: PRs, tickets, alerts, dashboards. Team adds the conversational Slack bot and managed AI. Enterprise adds SSO, audit logs, and an SLA."), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 12, justifyContent: "center", marginTop: 24 } }, /* @__PURE__ */ React.createElement("span", { style: { fontSize: 13, color: annual ? "var(--fg-3)" : "var(--fg)", fontWeight: annual ? 400 : 500, transition: "color .15s" } }, "Monthly"), /* @__PURE__ */ React.createElement(
    "button",
    {
      onClick: () => setAnnual((a) => !a),
      style: {
        width: 44,
        height: 24,
        borderRadius: 6,
        border: "1px solid var(--line-2)",
        background: annual ? "var(--accent)" : "var(--bg-2)",
        position: "relative",
        cursor: "pointer",
        transition: "background .2s",
        flexShrink: 0
      },
      "aria-label": "Toggle annual billing"
    },
    /* @__PURE__ */ React.createElement("span", { style: {
      position: "absolute",
      top: 3,
      left: annual ? 20 : 3,
      width: 16,
      height: 16,
      borderRadius: "50%",
      background: annual ? "var(--bg)" : "var(--fg-3)",
      transition: "left .2s, background .2s",
      display: "block"
    } })
  ), /* @__PURE__ */ React.createElement("span", { style: { display: "flex", alignItems: "center", gap: 6 } }, /* @__PURE__ */ React.createElement("span", { style: { fontSize: 13, color: annual ? "var(--fg)" : "var(--fg-3)", fontWeight: annual ? 500 : 400, transition: "color .15s" } }, "Annual"), /* @__PURE__ */ React.createElement("span", { style: { fontSize: 11, fontWeight: 500, color: "var(--success)", background: "rgba(60,186,122,.12)", padding: "2px 7px", borderRadius: 2, letterSpacing: ".03em" } }, "SAVE 17%")))), /* @__PURE__ */ React.createElement("div", { className: "ptable-wrap" }, /* @__PURE__ */ React.createElement("div", { className: "ptable" }, /* @__PURE__ */ React.createElement("div", { className: "ph ph-corner" }), /* @__PURE__ */ React.createElement("div", { className: "ph" }, /* @__PURE__ */ React.createElement("div", { className: "pt-name" }, "Solo"), /* @__PURE__ */ React.createElement("div", { className: "pt-price" }, /* @__PURE__ */ React.createElement("span", { className: "pt-amt" }, "Free"), /* @__PURE__ */ React.createElement("span", { className: "pt-per" }, "forever")), /* @__PURE__ */ React.createElement(
    "a",
    {
      className: "btn btn-ghost pt-cta",
      href: "/docs.html",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "pricing", plan: "solo" });
      }
    },
    "Start free"
  )), /* @__PURE__ */ React.createElement("div", { className: "ph" }, /* @__PURE__ */ React.createElement("div", { className: "pt-name" }, "Pro"), /* @__PURE__ */ React.createElement("div", { className: "pt-price" }, /* @__PURE__ */ React.createElement("span", { className: "pt-amt" }, proPrice), /* @__PURE__ */ React.createElement("span", { className: "pt-per" }, proPer)), /* @__PURE__ */ React.createElement("div", { className: "pt-sub" }, proSub), /* @__PURE__ */ React.createElement(
    "a",
    {
      className: "btn btn-ghost pt-cta",
      href: proLink,
      target: "_blank",
      rel: "noopener noreferrer",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "pricing", plan: proPlan, billing: annual ? "annual" : "monthly" });
      }
    },
    annual ? "Get annual" : "Get Pro"
  )), /* @__PURE__ */ React.createElement("div", { className: "ph pcol-team" }, /* @__PURE__ */ React.createElement("div", { className: "pt-rec" }, "Recommended"), /* @__PURE__ */ React.createElement("div", { className: "pt-name" }, "Team"), /* @__PURE__ */ React.createElement("div", { className: "pt-price" }, /* @__PURE__ */ React.createElement("span", { className: "pt-amt" }, teamPrice), /* @__PURE__ */ React.createElement("span", { className: "pt-per" }, teamPer)), /* @__PURE__ */ React.createElement("div", { className: "pt-sub" }, teamSub), /* @__PURE__ */ React.createElement(
    "a",
    {
      className: "btn btn-primary pt-cta",
      href: teamLink,
      target: "_blank",
      rel: "noopener noreferrer",
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "pricing", plan: teamPlan, billing: annual ? "annual" : "monthly" });
      }
    },
    annual ? "Get annual" : "Get Team"
  )), /* @__PURE__ */ React.createElement("div", { className: "ph" }, /* @__PURE__ */ React.createElement("div", { className: "pt-name" }, "Enterprise"), /* @__PURE__ */ React.createElement("div", { className: "pt-price" }, /* @__PURE__ */ React.createElement("span", { className: "pt-amt" }, "Custom")), /* @__PURE__ */ React.createElement(
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
  )), PRICE_ROWS.map((r, i) => /* @__PURE__ */ React.createElement(React.Fragment, { key: i }, /* @__PURE__ */ React.createElement("div", { className: "pr pr-label" }, r.label), /* @__PURE__ */ React.createElement("div", { className: "pr pr-cell" }, /* @__PURE__ */ React.createElement(PCell, { v: r.solo })), /* @__PURE__ */ React.createElement("div", { className: "pr pr-cell" }, /* @__PURE__ */ React.createElement(PCell, { v: r.pro })), /* @__PURE__ */ React.createElement("div", { className: "pr pr-cell pcol-team" }, /* @__PURE__ */ React.createElement(PCell, { v: r.team })), /* @__PURE__ */ React.createElement("div", { className: "pr pr-cell" }, /* @__PURE__ */ React.createElement(PCell, { v: r.ent })))))), /* @__PURE__ */ React.createElement(PricingCards, { annual, proPrice, proPer, proSub, proLink, proPlan, teamPrice, teamPer, teamSub, teamLink, teamPlan }), /* @__PURE__ */ React.createElement("p", { className: "pfoot" }, "No credit card for Solo. Team trial requires a card, cancel any time."), /* @__PURE__ */ React.createElement("p", { className: "pfoot pdemo" }, "Weighing Team for your org?", " ", /* @__PURE__ */ React.createElement(
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
function MidCta() {
  return /* @__PURE__ */ React.createElement("section", { id: "mid-cta", style: { borderTop: "1px solid var(--line)", borderBottom: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("div", { className: "wrap", style: { paddingTop: 76, paddingBottom: 76 } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", alignItems: "center", gap: 22, textAlign: "center" } }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h2", { style: { marginBottom: 12 } }, "Ready to stop guessing?"), /* @__PURE__ */ React.createElement("p", { style: { color: "var(--fg-2)", maxWidth: "38ch", margin: "0 auto", lineHeight: 1.55, textWrap: "balance" } }, "Minutes from install to your first real insight. Free forever for solo use.")), /* @__PURE__ */ React.createElement("div", { style: { display: "inline-flex", alignItems: "stretch", background: "var(--bg-1)", border: "1px solid var(--line-2)", borderRadius: "var(--r-md)", fontFamily: "var(--mono)", fontSize: 13.5, overflow: "hidden", maxWidth: "100%" } }, /* @__PURE__ */ React.createElement("span", { style: { padding: "12px 13px", color: "var(--fg-3)", background: "var(--bg-2)", borderRight: "1px solid var(--line)" } }, "$"), /* @__PURE__ */ React.createElement("span", { style: { padding: "12px 16px", color: "var(--fg)", whiteSpace: "nowrap", overflowX: "auto" } }, "uvx finops-mcp")), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", justifyContent: "center" } }, /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "/docs.html#install",
      className: "btn btn-primary",
      onClick: () => {
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
  )))));
}
function FootCta() {
  return /* @__PURE__ */ React.createElement("section", { className: "foot-cta", id: "cta" }, /* @__PURE__ */ React.createElement("div", { className: "foot-cta-grid" }), /* @__PURE__ */ React.createElement("div", { className: "wrap", style: { position: "relative" } }, /* @__PURE__ */ React.createElement("div", { className: "eyebrow", style: { marginBottom: 32, display: "inline-flex" } }, /* @__PURE__ */ React.createElement("span", { className: "d" }), " Free tier \xB7 no credit card"), /* @__PURE__ */ React.createElement("h2", { className: "display" }, "Stop staring at graphs.", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "Start closing tickets.")), /* @__PURE__ */ React.createElement("div", { style: { marginTop: 48, display: "flex", flexDirection: "column", alignItems: "center", gap: 16 } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 14 } }, /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "/docs.html",
      className: "btn btn-primary",
      style: { padding: "14px 22px", fontSize: 14 },
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "footer_cta", cta: "install" });
      }
    },
    "Get started free ",
    /* @__PURE__ */ React.createElement("span", { className: "arr" }, "\u2192")
  ), /* @__PURE__ */ React.createElement(
    "a",
    {
      href: "https://calendar.app.google/2duYBqjLXaTmX5xC8",
      target: "_blank",
      rel: "noopener noreferrer",
      className: "btn btn-ghost",
      style: { padding: "14px 22px", fontSize: 14 },
      onClick: () => {
        if (window.posthog) posthog.capture("cta_clicked", { location: "footer_cta", cta: "book_demo" });
      }
    },
    "Book a live demo"
  )), /* @__PURE__ */ React.createElement(EmailCapture, { source: "footer", placeholder: "drop your email, we'll send the setup guide", btnLabel: "Send it", center: true })), /* @__PURE__ */ React.createElement("p", { className: "mono", style: { marginTop: 32, fontSize: 12, color: "var(--fg-3)", letterSpacing: ".04em" } }, "$ uvx finops-mcp"), /* @__PURE__ */ React.createElement("p", { style: { marginTop: 24, fontSize: 13, color: "var(--fg-3)" } }, "Building something? ", /* @__PURE__ */ React.createElement("a", { href: "/about", style: { color: "var(--accent-dim)" } }, "Read the founder note and investor thesis \u2192"))));
}
function FounderNote() {
  return /* @__PURE__ */ React.createElement("section", { id: "founder", style: { borderTop: "1px solid var(--line)" } }, /* @__PURE__ */ React.createElement("div", { className: "wrap", style: { maxWidth: 680, paddingTop: 80, paddingBottom: 80 } }, /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "'Bricolage Grotesque',system-ui,sans-serif", fontWeight: 500, fontSize: 11, color: "var(--accent-dim)", letterSpacing: ".08em", textTransform: "uppercase", display: "flex", alignItems: "center", gap: 10, marginBottom: 24 } }, /* @__PURE__ */ React.createElement("span", { style: { width: 24, height: 1, background: "var(--accent-dim)", display: "inline-block" } }), "Why I built this"), /* @__PURE__ */ React.createElement("p", { style: { fontSize: 17, lineHeight: 1.75, color: "var(--fg-2)", marginBottom: 28 } }, "I built this because I spent most of my day bouncing between dashboards that barely showed what I actually needed, the AWS console, and Claude. I'd ask Claude a question, manually paste in numbers, get an answer, then go back and repeat the whole thing."), /* @__PURE__ */ React.createElement("p", { style: { fontSize: 17, lineHeight: 1.75, color: "var(--fg-2)", marginBottom: 28 } }, "A lot of FinOps tools are shipping MCP integrations now. But they're all built for enterprise, priced for enterprise, and none of them fit the way I actually work. They give you visibility. They don't help you think."), /* @__PURE__ */ React.createElement("p", { style: { fontSize: 17, lineHeight: 1.75, color: "var(--fg-2)", marginBottom: 36 } }, "nable solves the problems I actually had. The recommendations go deeper than anything I've seen out of the box, and for the first time I can actually reason through my own optimization opportunities instead of just staring at a graph."), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 14 } }, /* @__PURE__ */ React.createElement("div", { style: { width: 40, height: 40, borderRadius: "50%", background: "var(--accent)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 } }, /* @__PURE__ */ React.createElement("span", { style: { fontFamily: "var(--mono)", fontSize: 13, fontWeight: 600, color: "var(--bg)" } }, "CB")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { style: { fontSize: 14, fontWeight: 500, color: "var(--fg)" } }, "Chandan Bukkapatnam"), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 13, color: "var(--fg-3)" } }, "Founder \xB7 ", /* @__PURE__ */ React.createElement("a", { href: "mailto:chandan@getnable.com", target: "_blank", rel: "noopener noreferrer", style: { color: "var(--accent)" } }, "chandan@getnable.com"))))));
}
function Footer({ version }) {
  return /* @__PURE__ */ React.createElement("footer", null, /* @__PURE__ */ React.createElement("div", { className: "wrap" }, /* @__PURE__ */ React.createElement("div", { className: "foot" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("a", { href: "#top", className: "logo", style: { marginBottom: 18 } }, /* @__PURE__ */ React.createElement(LogoMark, null), /* @__PURE__ */ React.createElement("span", null, "nable")), /* @__PURE__ */ React.createElement("p", { style: { color: "var(--fg-3)", fontSize: 13, maxWidth: "34ch", lineHeight: 1.55, marginTop: 10 } }, "Your cloud and AI bill, answered. Made in Austin, TX.")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h5", null, "Product"), /* @__PURE__ */ React.createElement("a", { href: "#connectors" }, "Connectors"), /* @__PURE__ */ React.createElement("a", { href: "#pricing" }, "Pricing"), /* @__PURE__ */ React.createElement(
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
  ), /* @__PURE__ */ React.createElement("a", { href: "#faq" }, "FAQ")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h5", null, "Resources"), /* @__PURE__ */ React.createElement("a", { href: "/docs.html" }, "Docs"), /* @__PURE__ */ React.createElement("a", { href: "/docs.html#quickstart" }, "Quickstart"), /* @__PURE__ */ React.createElement("a", { href: "/docs.html#iam" }, "IAM templates"), /* @__PURE__ */ React.createElement("a", { href: "/security" }, "Security")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h5", null, "Company"), /* @__PURE__ */ React.createElement("a", { href: "/about" }, "About"), /* @__PURE__ */ React.createElement("a", { href: "/about#investors" }, "Investors"), /* @__PURE__ */ React.createElement("a", { href: "mailto:hello@getnable.com", target: "_blank", rel: "noopener noreferrer" }, "Contact"), /* @__PURE__ */ React.createElement("a", { href: "https://github.com/chaandannn/finopsmcp", target: "_blank", rel: "noopener noreferrer" }, "GitHub"), /* @__PURE__ */ React.createElement("a", { href: "https://www.linkedin.com/company/getnable/", target: "_blank", rel: "noopener noreferrer" }, "LinkedIn"))), /* @__PURE__ */ React.createElement("div", { className: "foot-meta" }, /* @__PURE__ */ React.createElement("span", null, "2026 nable \xB7 ", /* @__PURE__ */ React.createElement("a", { href: "/privacy", style: { color: "var(--fg-3)" } }, "Privacy"), " \xB7 ", /* @__PURE__ */ React.createElement("a", { href: "/terms", style: { color: "var(--fg-3)" } }, "Terms")), /* @__PURE__ */ React.createElement("span", null, "nable \xB7 runtime healthy"))));
}
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
    a: "A few minutes. Run `uvx finops-mcp` (uv fetches a matching Python and runs the setup wizard, no PATH setup needed), or `pip install -U finops-mcp && finops welcome` if you're already on Python 3.10+. The wizard connects Claude, connects your cloud, and shows your first cost number right in the terminal. Want to see it first? `uvx finops-mcp welcome --demo` runs it on sample data."
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
  }
];
function FAQ() {
  const [open, setOpen] = useState(null);
  return /* @__PURE__ */ React.createElement("section", { id: "faq", className: "alt" }, /* @__PURE__ */ React.createElement("div", { className: "wrap", style: { maxWidth: 720, paddingTop: 80, paddingBottom: 80 } }, /* @__PURE__ */ React.createElement("div", { style: { fontFamily: "'Bricolage Grotesque',system-ui,sans-serif", fontWeight: 500, fontSize: 11, color: "var(--accent-dim)", letterSpacing: ".08em", textTransform: "uppercase", display: "flex", alignItems: "center", gap: 10, marginBottom: 18 } }, /* @__PURE__ */ React.createElement("span", { style: { width: 24, height: 1, background: "var(--accent-dim)", display: "inline-block" } }), "FAQ"), /* @__PURE__ */ React.createElement("h2", { style: { marginBottom: 48 } }, "Questions we actually get."), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column" } }, FAQ_ITEMS.map((item, i) => {
    const isOpen = open === i;
    return /* @__PURE__ */ React.createElement("div", { key: i, style: {
      borderBottom: "1px solid var(--line)"
    } }, /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "faq-q",
        onClick: () => setOpen(isOpen ? null : i),
        style: {
          width: "100%",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          padding: "22px 0",
          background: "none",
          border: "none",
          color: isOpen ? "var(--fg)" : "var(--fg-2)",
          fontFamily: "'Bricolage Grotesque',system-ui,sans-serif",
          fontSize: 16,
          fontWeight: 500,
          textAlign: "left",
          cursor: "pointer",
          gap: 16,
          transition: "color .15s"
        },
        "aria-expanded": isOpen
      },
      /* @__PURE__ */ React.createElement("span", null, item.q),
      /* @__PURE__ */ React.createElement("span", { className: "faq-plus", style: {
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
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(Nav, null), /* @__PURE__ */ React.createElement(Hero, { layout: t.layout, interaction: t.interaction }), /* @__PURE__ */ React.createElement(HowItWorks, null), /* @__PURE__ */ React.createElement(EveryEditor, null), /* @__PURE__ */ React.createElement(AiCost, null), /* @__PURE__ */ React.createElement(Connectors, null), /* @__PURE__ */ React.createElement(Architecture, { version }), /* @__PURE__ */ React.createElement(Pricing, null), /* @__PURE__ */ React.createElement(MidCta, null), /* @__PURE__ */ React.createElement(FAQ, null), /* @__PURE__ */ React.createElement(FootCta, null), /* @__PURE__ */ React.createElement(Footer, { version }), /* @__PURE__ */ React.createElement(Tweaks, null));
}
ReactDOM.createRoot(document.getElementById("app")).render(/* @__PURE__ */ React.createElement(App, null));
