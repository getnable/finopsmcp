# Design System — nable

## Product Context

- **What this is:** A local-first FinOps MCP server that connects AWS, Azure, GCP, and 17 billing APIs to Claude, Cursor, and other AI editors. Engineers ask questions in plain English. nable runs the analysis, surfaces recommendations, and closes the loop.
- **Who it's for:** Senior engineers, platform teams, and DevOps leads at startups and mid-size tech companies. People who live in the terminal, read Hacker News, and distrust marketing.
- **Space/industry:** FinOps / cloud cost intelligence. Peers: Infracost, Kubecost, OpenCost.
- **Project type:** Marketing site with interactive product demo.
- **Memorable thing:** "The terminal that knows your bill." Precision tool, not dashboard.

---

## Aesthetic Direction

- **Direction:** Industrial/Utilitarian meets Editorial Precision
- **Decoration level:** Intentional. Almost nothing decorative. When something visual appears, it earns its place (the architecture diagram, the console demo). No grid-line backgrounds. No blob gradients. No pulse animations on non-functional dots. Texture comes from typography and data density.
- **Mood:** Cold, precise, built by someone who cares. Not warm and approachable. Correct and trustworthy. The product says "your EC2 spend is up 18%." Cold precision is more trust-inducing than warmth. Closer to a Bloomberg terminal than a SaaS landing page.
- **Reference sites:** Vercel circa 2022 (structure discipline), Linear (cold precision), Zed editor site (developer-native without trying to be friendly)
- **Anti-patterns to avoid:** Purple gradients, 3-column icon grids in colored circles, centered everything, uniform pill border-radius on all elements, grid-line hero backgrounds, pulse dot animations.

---

## Typography

All fonts are Instrument Sans across the entire site. Fraunces was evaluated and rejected — it read as try-hard at hero scale and inconsistent with the cold precision aesthetic.

- **All headings and body:** Instrument Sans
- **Hero accent lines:** Instrument Sans, weight 600
- **UI/Labels:** Instrument Sans, weight 500, 13px, uppercase where used as section labels
- **Data/Tables/Terminal content:** Geist Mono (primary), JetBrains Mono (fallback). Reserved for actual technical content only. Not section labels. Not nav.

**H1 hero:**
```css
font-size: clamp(40px, 4.6vw, 68px);
line-height: 1.05;
letter-spacing: -0.04em;
font-weight: 300;
```

**H2 section headings:**
```css
letter-spacing: -0.035em;
line-height: 1.08;
font-weight: 400;
```

**Loading:** Google Fonts for Instrument Sans. Geist Mono via jsDelivr CDN (allowed in CSP).

---

## Color

**Palette: Cold Graphite**

```css
:root {
  --bg:        #0d0f10;  /* near-black, cool neutral */
  --bg-1:      #111416;  /* default surface */
  --bg-2:      #181c1f;  /* elevated surface */
  --bg-3:      #1e2327;  /* highest / hover surface */
  --line:      #242a2e;  /* subtle separator */
  --line-2:    #2e3539;  /* active / hover border */
  --fg:        #f0f2f3;  /* primary, cool white */
  --fg-2:      #94a3ab;  /* secondary / body muted */
  --fg-3:      #56656d;  /* tertiary / labels */
  --fg-4:      #2d3a40;  /* disabled */
  --accent:     #4db8d4;  /* ice blue */
  --accent-dim: #2c7d91;  /* dimmed for section labels */
  --success:   #3cba7a;   /* savings, positive deltas */
  --warn:      #e6a840;   /* budget warnings */
  --alert:     #e05c4b;   /* anomalies, cost spikes */
  --grid:      rgba(255,255,255,.02);
}
```

**Why ice blue:** Blue reads as data and precision. Ice blue stands alone in the category. Infracost uses purple. Kubecost uses green. Amber-on-dark is the indie dev tool default.

**Accent usage rules:**
- Primary CTA buttons: `--accent` background
- Savings / positive deltas: `--success`
- Spikes / overages: `--alert`
- Budget warnings: `--warn`
- Section label decorators: `--accent-dim`
- Console status dot: `--success` (health indicator, not brand)
- Everything else: no accent. Use `--fg-3`.

---

## Spacing

- **Section padding:** 80px top/bottom
- **Tight sections:** 56px top/bottom
- **Content max-width:** 1280px

---

## Border Radius

```
--r-xs:   2px   (tags, chips)
--r-sm:   4px   (small badges)
--r-md:   6px   (inputs, install widget)
--r-lg:   8px   (buttons, cards)
--r-xl:   12px  (console surface)
--r-2xl:  16px  (large card containers)
```

No `border-radius: 99px` on non-circular elements.

---

## Layout

- **Hero:** Split layout. Copy left, console right. Console is the product. Never center the hero.
- **Hero background:** Nothing. No grid lines. No radial gradient.
- **Section order (page):** Hero > Depth (why it's special) > Connectors (what it connects to) > Architecture > Pricing > FAQ > Footer
- **Section structure:** Label (Instrument Sans, 11px, uppercase, `--accent-dim`) + H2 + body + content block.

---

## Motion

**Keep:**
- Console typewriter effect
- `fadeup` entrance (300ms ease-out, 6px Y offset)
- FAQ accordion (200ms ease-in-out)
- Hover transitions (150ms)
- Question marquee scroll

**Removed:**
- `dataflow` animation on architecture arrows (static lines)
- `pulse` on decorative dots

**Reduced motion:** Respect `prefers-reduced-motion`.

---

## Component Notes

### Buttons
- Primary: `--accent` bg, `--bg` text, `padding: 10px 18px`, `border-radius: 8px`, min-height 44px
- Ghost: transparent bg, `--line-2` border, hover `--bg-2`
- No gradients

### Tags/Chips
- Border-radius: 2px. Nearly square. Font: Instrument Sans 10px uppercase.

### Install widget
- Font: Geist Mono. Border-radius: 6px.

### Console
- Border-radius: 12px. Status dot uses `--success`.

### Section labels
- Instrument Sans, 11px, weight 500, uppercase, `--accent-dim`
- Never monospace for labels

### Trust strip
- Numbers: Geist Mono tabular-nums
- Sub-labels: Instrument Sans

---

## Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-05-29 | Initial design system | Cold graphite palette, ice blue accent, tight border radius, no decorative motion |
| 2026-05-30 | Fraunces to Instrument Sans | Fraunces read as try-hard at hero scale. Instrument Sans is consistent and clean. |
| 2026-05-30 | Section order: Depth before Connectors | Value before breadth. Show why it's interesting before showing what it connects to. |
| 2026-05-30 | No Postgres required for team mode | Slack and Notion handle team sharing. No self-hosted database needed. |
| 2026-05-30 | Section padding: 80px | Tighter than original 112px. More confident. |
