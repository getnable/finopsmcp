---
name: change-control
description: Use when changing infrastructure in an organization that keeps SOC 2 change-management evidence, when the guard mentions a change freeze, or when asked for change or approval evidence.
---

# Change control

This organization keeps change-management evidence (SOC 2 CC8.1) from nable's
guard ledger. Every infrastructure change an agent attempts is recorded with
what the guard decided and who approved it.

- During a change freeze, the guard asks before deploys and denies
  teardowns. When it asks, tell the user what the change is, which freeze is
  in force and until when, and let them decide. Do not look for another
  command that does the same thing, and never retry a denied command in
  another form.
- Never merge with `--admin`, force-push a protected branch, or change branch
  protection or rulesets. Propose the change to a person instead.
- When asked for change evidence, run
  `nable pack report io.github.getnable/change-control cc8.1-evidence --since 90d`
  (add `--json` for the records). Say plainly that it is evidence drawn from
  the guard ledger, not a SOC 2 report or a certification.
- Approval chains the pack's adapter finds in CODEOWNERS and GitHub exports
  are proposals. Show them to the user; never confirm an org fact yourself.
