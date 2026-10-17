---
name: commitments-bounds
description: Use when asked about Savings Plans, Reserved Instances, committed use discounts or reservations, or before any command that would buy one.
---

# Commitments with bounds

This organization bounds its commitments: coverage of at most 80% of
eligible spend, a term of at most 12 months, no money up front, and no
purchase whose term would run into a planned migration.

- Never buy a commitment, and never run a command that does. Recommend, and
  let a person decide and buy.
- nable's commitment advice (`get_commitment_analysis`,
  `recommend_database_savings_plans`) is already cut to these bounds. When a
  recommendation carries `bounds` or the result lists `cut_by_bounds`, say
  what was cut and which bound cut it, rather than presenting the original
  figure.
- If the guard asks about a purchase, pass on the bound its reason names and
  let the user decide. Do not look for another way to buy it.
