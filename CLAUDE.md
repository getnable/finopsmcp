# Writing Style Rules

**No em dashes.** Never use — in any output: docs, copy, briefs, comments, commit messages, anywhere. Use a comma, period, colon, or rewrite the sentence instead.

**Write like a human, not a consultant.** Short sentences. No filler phrases ("it's worth noting", "it's important to understand", "at its core", "in essence", "furthermore"). Say the thing directly.

**No exclamation points** in professional output.

**No Oxford-comma abuse** — keep punctuation minimal.

These rules apply to every file written or edited in this project: markdown docs, landing page copy, README, VC briefs, emails, code comments, everything.

## Design System

Always read DESIGN.md before making any visual or UI decisions.
All font choices, colors, spacing, border-radius, and aesthetic direction are defined there.
Do not deviate without explicit user approval.
In QA mode, flag any code that does not match DESIGN.md.

Key rules that will catch you:
- Display font is Fraunces (serif), not Space Grotesk or Inter.
- Body font is Instrument Sans, not Inter.
- Monospace (Berkeley Mono or Geist Mono) is reserved for actual technical content: code, commands, instance IDs, hex values, data table numbers. Not section labels. Not nav links.
- Accent is ice blue (#4db8d4), not amber.
- Background is cool graphite (#0d0f10), not warm graphite.
- Border-radius: 2px chips, 6px inputs, 8px buttons, 12px surfaces. No 99px pill radius on non-circular elements.
- No grid-line backgrounds. No pulse animations on decorative dots. No dataflow animations.
- Section padding is 80px, not 112px.
