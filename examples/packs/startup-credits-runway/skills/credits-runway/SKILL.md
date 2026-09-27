---
name: credits-runway
description: Use before launching GPU or other large cloud resources at a startup running on cloud credits, or when asked how long the credits will last.
---

# Credits runway

This organization runs on cloud credits, and the months they last are the
company's runway for infrastructure.

- Before launching GPU instances or anything priced over a few hundred dollars
  a month, ask nable for the estimate (`estimate_change_cost`, or `nable guard
  check --command "..."`) and tell the user the monthly figure and what it
  does to runway.
- When asked how long credits will last, call `get_credit_status` and report
  the balance, the current monthly burn and the months left at that rate.
- If the guard asks about a GPU launch, explain the reason it gives and let
  the user decide. Do not look for a way around the question.
