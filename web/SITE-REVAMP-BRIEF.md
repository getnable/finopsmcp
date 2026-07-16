# getnable.com site revamp brief

Reference bar: **Kestrel AI** (usekestrel.ai) — same DNA as us (AI agent that turns
plain English into deterministic, approval-gated actions across your stack), and
they present it better. Goal: match their production quality and, more importantly,
their **structural** choices (live demo, product shown multiple ways, "connects to
everything" moment), in **our** identity.

## Verdict on the current site (grounded, not from memory)

It is **clean and on-brand, not mid.** The hero headline ("The cost brain for the
AI era"), the copy, the pricing page are all good. The problem is the product is
**under-shown**, not badly styled:

1. The hero cost-console is **canned** — it plays a script. Kestrel's you type into.
2. The product is shown **exactly one way** (a terminal). The new dashboard (drawer,
   streaming analyst, scorecard, 5,628-resource table) appears **nowhere**.
3. There is **no "connects to your entire stack" visual**, despite connecting to
   AWS/GCP/Azure/Kubernetes + 13 SaaS + the LLM providers.
4. **Low motion/interactivity.** Kestrel feels alive; ours is static below the hero.

## Design direction (settles the recurring "why the terminal look" question)

The terminal aesthetic is **not required and not what makes us finops.** Decision:

- **Keep** the ice-blue (#4db8d4) — it's an ownable asset.
- **Keep monospace for numbers/tables/code/IDs** — that's functional (tabular figures
  align), not taste.
- **Relax everything else toward clean, modern, high-air SaaS formatting** (Kestrel /
  Linear / Vercel structure): large product imagery, generous whitespace, sectioned
  scroll, tasteful motion. True-black is fine as a base but not a mandate; gradients
  and depth are allowed where they help product imagery pop.
- The differentiator is **showing the product connect to a real stack and answer a
  cost question live**, not the background color. Build around that.

This updates DESIGN.md's "industrial/terminal, no gradients" stance for the marketing
site. Reflect it there once approved. The in-product dashboard keeps its own system.

## Reality check (after auditing the live site, not just the hero)

The site is **well-built below the fold** and most of what I first prescribed already
exists:
- Console is interactive (type → answer, matched against a curated library; the right
  call for a hero — reliable, free, no abuse surface). **M3 effectively done.**
- Connects-to-your-stack exists: `Connectors` section, FOCUS 1.2, logo marquee of 15+
  providers. **M2 done** (could be flexed harder, not rebuilt).
- Architecture section (flow / data-residency / run-or-host) + honest pricing. Good.

## The one real gap (do this)

**M1 — Show the dashboard.** The hardened dashboard (the $5.1M overview, instant
drill-down drawer, streaming AI Analyst, efficiency scorecard, 5,628-resource table)
appears **nowhere** on the marketing site. It is the best-looking thing we own and it
is invisible. Add a product-showcase section to the homepage using real imagery from
the StreamCo demo box. This is the whole revamp for now.

Where it goes: a new section after `SeeItWork` / before or after `AiCost`. Frame it as
"and your finance team lives here, not a terminal" — the dashboard is the non-engineer
surface, the console is the engineer surface. Two audiences, one scroll (the exact
FinOps-lead + VP-Eng room).

## Smaller polish (optional, after M1)

- Flex the Connectors breadth harder (counts: "9 clouds & platforms · 13 SaaS · every
  model provider") — presentation, not new build.
- Named scenario cards: concrete finops stories ("CloudFront egress spiked → traced to
  the PR → drafted the revert", "GPU pool idle off-peak → scheduled → verified $8k/mo",
  "gpt-4o uncached catalog context → caching recovers $36k/mo").
- Tasteful motion on section reveals.

## Pages

- **Home**: M1–M5 above, in that scroll order after the hero.
- **Pricing**: keep the honest two-tier. Add a value line to Enterprise (managed
  single-tenant, always-on, SSO/RBAC, data never pooled) with a small product image,
  not just a bullet list. Consider a "what you get" comparison strip.
- **Platform/Product page**: currently thin. Build a real one anchored on the
  dashboard + the watch→propose→verify→learn loop, with imagery.
- **Docs**: interactive and code-forward — runnable examples inline, the MCP tool
  reference as a first-class page, the "words → tool call → result" pattern.

## Keep (do not lose in the revamp)

- The hero headline and honest subhead (finds waste, prices on your real rates, ships
  the fix as a PR you approve).
- The propose-only / "PR you approve" trust story — package it as prominently as
  Kestrel packages "deterministic by design, no surprises."
- The free-local-funnel framing (`uvx nable`, read-only, no signup).

## Scope

- **Today (pre-demo, so it doesn't look thin if Vizio looks us up):** M1 + M2. Both
  are front-end, high visual impact, low risk.
- **Next:** M3 (interactive demo), M4, M5, the platform page, docs.
- Keep every change front-end and additive; no backend dependency for M1/M2.
