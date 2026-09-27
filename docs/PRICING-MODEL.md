# Pricing model and margin guard

Status: proposal. The only prices on sale are the ones in `src/finops/license.PLANS`: Free, and Pro at $25 and Team at $1,000, monthly only. Everything else below is a proposal that needs the founder's approval and a Stripe product before it appears on the site, in the CLI or in any email: Cloud, Growth and Enterprise, and every annual price, Pro's and Team's included.

The rule has two halves:

1. Gross margin is protected at 80% on every paid plan, in the expected case and in the high case (every capped cost at its cap in the same month), on monthly and annual billing.
2. Pricing is flat. A plan costs the same whatever the customer's cloud bill or savings. Never a percentage of spend, never a percentage of savings.

`src/finops/margin_guard.py` holds the model and `tests/test_margin_guard.py` fails the build when either half stops being true. To see the numbers:

```
nable pricing margins                 # both price options, every paid plan
nable pricing margins --lines cloud   # plus the cost lines for one plan
nable pricing margins --json
```

(`pricing` is an internal command and is not listed in `nable --help`.)

## The ladder (option A, proposed)

| Plan | Price | Runs where | What it includes, in plain words |
|---|---|---|---|
| Free | $0 | Your machine | The CLI, the MCP server, the guard in every agent, every connector, scans, anomaly detection and briefs whenever you ask. |
| Pro | $25 a month; proposed: $250 a year | Your machine | Free plus the Pro features on one install. AI features run on your own model key. Nothing hosted. Email support. |
| Cloud | $129 a month, $1,419 a year | Hosted | 5 cloud accounts. Every night nable scans each one, checks it for anomalies and writes your brief; plus 2 on-demand deep scans a day. Daily and weekly briefs by email, Slack and Teams, a dashboard, 13 months of history, shared guard policy for 10 agents, and $4 a month of hosted AI (the nightly narrative, critiques of new findings, and AI triage of about 40 anomalies a month). Chat runs on your own key. Unlimited seats. |
| Growth | $399 a month, $4,389 a year | Hosted | 15 cloud accounts, the nightly run on each, anomaly checks every 6 hours, 5 on-demand deep scans a day, the hosted @nable Slack bot and chat remediation, SSO, 25 months of history, 25 guarded agents, and $16 a month of hosted AI (the scheduled work plus about 110 Slack chat sessions or 20 root cause investigations). Unlimited seats. |
| Team | $1,000 a month; proposed: $11,000 a year | Hosted | Growth plus 50 accounts, the nightly run on each, anomaly checks every 2 hours, 10 on-demand deep scans a day, 100 guarded agents, SCIM, audit export, a shared Slack channel for support, and $50 a month of hosted AI (about 360 chat sessions or 70 root cause investigations after the scheduled work). Unlimited seats. |
| Enterprise | from $4,000 a month dedicated, from $2,500 self-hosted | A dedicated tenant, or your own cloud | Team on a dedicated database and workers, a DPA, custom retention, invoicing and an SLA. AI runs on your own key by default, with $75 a month included. Self-hosted is a license with support. |

Annual is one month free (Pro: two). Every annual price is a proposal, Pro and Team included: none is on sale until `license.PLANS` and Stripe carry it. Annual Growth, Team and Enterprise are invoiced and paid by ACH; the model charges Stripe ACH (0.8%, capped at $5) plus Stripe Billing's 0.7% on each invoice, on the assumption that they are subscription invoices (Stripe Invoicing's 0.4% is for one-off invoices, which nable does not send).

## Caps

These are the runtime meters the hosted product enforces (`margin_guard.metering_for(plan)`). Support is a modeled budget, not a meter: nobody is refused an answer.

| Plan | AI credit a month | AI a day | Jobs a day | Accounts | Line items a month | Guarded agents | Support budget (model only) |
|---|---|---|---|---|---|---|---|
| Pro | none (your key) | none | none (local) | local | local | local | 2.5 min |
| Cloud | $4 | $0.40 | 7 | 5 | 2M | 10 | 4 min |
| Growth | $16 | $1.60 | 20 | 15 | 6M | 25 | 18 min |
| Team | $50 | $5 | 60 | 50 | 20M | 100 | 40 min |
| Enterprise | $75 | $7.50 | 240 | 200 | 60M | 500 | 3 h |

A job is one run on one account: the nightly scan, anomaly check and brief inputs, or an on-demand deep scan. The daily jobs cap is the nightly run on every account plus the on-demand scans, and the nightly slots are reserved: on-demand scans are held to jobs a day minus accounts (Cloud 2, Growth 5, Team 10, Enterprise 40), so they can never push an account's nightly scan and anomaly check off the day. Intraday anomaly checks are deterministic code, are part of the plan, and are never dropped. The included AI credit is at most 7% of the plan price and is counted at list model price.

## Margin table

Per tenant-month. High is the stress case: the whole AI credit spent with no cache hits, every job slot used for a full scan, every account and line item at its cap, the support budget used, an international card, and about 40 hosted tenants sharing the platform.

Option A (proposed), Cloud $129, Growth $399:

| Plan | Billing | Revenue a month | COGS low / expected / high | Margin low / expected / high |
|---|---|---|---|---|
| Pro | monthly | $25.00 | $1.47 / $2.75 / $4.17 | 94.1% / 89.0% / 83.3% |
| Pro | annual | $20.83 | $1.04 / $2.33 / $3.69 | 95.0% / 88.8% / 82.3% |
| Cloud | monthly | $129.00 | $7.07 / $12.40 / $23.47 | 94.5% / 90.4% / 81.8% |
| Cloud | annual | $118.25 | $6.41 / $11.74 / $22.65 | 94.6% / 90.1% / 80.8% |
| Growth | monthly | $399.00 | $24.21 / $47.12 / $75.59 | 93.9% / 88.2% / 81.1% |
| Growth | annual | $365.75 | $12.53 / $35.43 / $57.91 | 96.6% / 90.3% / 84.2% |
| Team | monthly | $1,000.00 | $61.90 / $126.40 / $197.18 | 93.8% / 87.4% / 80.3% |
| Team | annual | $916.67 | $32.43 / $96.93 / $152.72 | 96.5% / 89.4% / 83.3% |
| Enterprise (dedicated) | monthly | $4,000.00 | $356.14 / $536.87 / $735.75 | 91.1% / 86.6% / 81.6% |
| Enterprise (dedicated) | annual | $3,666.67 | $349.22 / $529.95 / $728.83 | 90.5% / 85.5% / 80.1% |
| Enterprise (self-hosted) | monthly | $2,500.00 | $142.52 / $262.55 / $322.60 | 94.3% / 89.5% / 87.1% |
| Enterprise (self-hosted) | annual | $2,291.67 | $136.48 / $256.51 / $316.56 | 94.0% / 88.8% / 86.2% |

Option B, Cloud $149 and Growth $499 with the pricing analysis's caps (Cloud: 5 accounts, 24 jobs a day, $6 AI, 8 min support; Growth: 20 accounts, 96 jobs a day, 10M line items, $24 AI, 25 min support). Pro, Team and Enterprise are the same as option A.

| Plan | Billing | Revenue a month | COGS low / expected / high | Margin low / expected / high |
|---|---|---|---|---|
| Cloud | monthly | $149.00 | $7.79 / $13.12 / $33.87 | 94.8% / 91.2% / 77.3% |
| Cloud | annual | $136.58 | $7.07 / $12.40 / $32.96 | 94.8% / 90.9% / 75.9% |
| Growth | monthly | $499.00 | $27.81 / $50.65 / $113.17 | 94.4% / 89.8% / 77.3% |
| Growth | annual | $457.42 | $13.16 / $36.01 / $91.04 | 97.1% / 92.1% / 80.1% |

Option B misses the high-case floor because its caps let more be spent than the price carries: the whole $6 or $24 AI credit, and 24 or 96 full job runs a day. At those caps Cloud would need $176 and Growth $589 to clear 80%.

## What happens at each cap

Nothing past a cap is ever billed on its own. When the next unit of work would take a meter to 80% of its cap, admins are told the burn rate. When it would take the meter past the cap, the product takes a cheaper path, queues, or asks, and says so where the customer will see it (`margin_guard.at_cap(plan, meter, used, next_cost=..., route=..., has_own_key=...)`). The check is on where the unit would leave the meter, `used + next_cost`, not on `used` alone, so the last unit cannot overshoot; a reading that is not a finite number (a broken meter) counts as at the cap.

| Meter | At the cap, scheduled work | At the cap, a person asked |
|---|---|---|
| AI credit a month | With the customer's own model key on file: run on that key at zero markup, never the degraded path. Without one: degrade to the code-only brief, the deterministic brief and rules-based root cause, which is already the default path | With the customer's own key on file: run on it. Without one: ask for the key or a credit pack, in the thread, before running |
| AI a day (a tenth of the month) | With the customer's own key on file: run on it. Without one: code-only brief until tomorrow | With the customer's own key on file: run on it. Without one: ask for the key, or wait until tomorrow |
| Jobs a day | Queue until tomorrow, by priority | An on-demand deep scan is held to the on-demand slots (jobs a day minus accounts), so the nightly scan and anomaly check on every account always keep their slots. Past them: queue until tomorrow |
| Accounts | Hold the new account. Connected accounts keep running; remove one or add 10 more | Same |
| Line items a month | Keep daily aggregates, anomaly checks and briefs; pause new resource-level detail until next month or the add-on | Same |
| Guarded agents | The agent keeps the free local guard; it joins shared policy and the audit trail when a slot frees up or with Fleet Guard | Same |

On a plan that does not include a meter (Pro, Free), AI runs on the customer's own key and scheduled work runs locally when asked.

`at_cap` answers for one reading and cannot stop two workers that read the same `used` from both going ahead. The hosted side must reserve atomically: check and increment the meter in one transaction (for example `UPDATE ... SET used = used + :next_cost WHERE used + :next_cost <= :cap RETURNING used`), run the work only if the reservation succeeded, and settle an AI reservation to the priced `response.usage` afterwards.

## Add-ons

Flat prices, each at least 5x its modeled cost at its own cap (tested).

| Add-on | Price a month | Modeled cost | Multiple |
|---|---|---|---|
| Fleet Guard, 25 more guarded agents | $49 | $3.17 | 15.4x |
| 10 more connected accounts | $49 | $5.93 | 8.3x |
| 10M more billing line items a month | $49 | $6.55 | 7.5x |
| 12 more months of history | $29 | $1.75 | 16.6x |
| AI credit pack ($10 of model work at list) | $50 | $10.00 | 5.0x |
| Compliance pack (7-year guard ledger, SIEM and evidence export, DPA) | $199 | $38.40 | 5.2x |

## The promises

- Flat. The price is a constant per plan. Nothing in the model can read the customer's spend or savings, and the test checks the structure, not just the numbers.
- Never a percentage of cloud spend, and never a percentage of savings.
- The customer's own model key, at zero markup, is always available on every plan and for every AI feature, local or hosted. A hosted tenant with its key on file never gets the degraded path at an AI cap: the work runs on the key.
- Never silent overage. At a cap the product runs on the customer's own key if one is on file, or degrades, queues or asks. A customer pays more only by choosing an add-on or a pack.
- Unlimited seats on every hosted plan.

## The assumptions that move the margin most

Margin in the high case (expected / high monthly / high annual) when one assumption is wrong, option A:

| Change | Cloud | Growth | Team |
|---|---|---|---|
| Base model | 90.4% / 81.8% / 80.8% | 88.2% / 81.1% / 84.2% | 87.4% / 80.3% / 83.3% |
| Shared platform $600 a month, not $300 (or half the tenants) | 88.9% / 78.2% / 76.9% | 87.3% / 78.7% / 81.6% | 86.6% / 78.4% / 81.3% |
| Support budget 50% over | 90.4% / 80.3% / 79.2% | 88.2% / 78.8% / 81.7% | 87.4% / 78.3% / 81.2% |
| Worker time per job doubled | 90.1% / 80.7% / 79.7% | 88.0% / 80.1% / 83.1% | 87.1% / 79.1% / 82.0% |
| Card fees 1 point higher | 89.4% / 80.8% / 79.8% | 87.2% / 80.1% / 84.2% | 86.4% / 79.3% / 83.3% |
| Database storage per line item doubled | 90.3% / 81.3% / 80.3% | 87.7% / 80.1% / 83.1% | 86.9% / 79.0% / 82.0% |
| Observability doubled | 89.8% / 80.6% / 79.6% | 88.0% / 80.7% / 83.8% | 87.3% / 80.1% / 83.2% |

Headroom over 80% in the high case is one to two points, so check these first against the real hosted bill, in this order:

1. The shared platform cost and how many tenants share it. `platform_fixed_usd_month` is $300 (pooled database compute, queue, load balancer, secrets, backups, the always-on API) and the high case spreads it over about 40 tenants. If the real fixed bill is $600, or there are 20 tenants at launch, every hosted plan falls under 80% in the high case on monthly billing. Below about 40 tenants the platform is an investment, not a cost any price fixes.
2. Worker-seconds per job. The high case prices every job slot at 300 seconds of a 1 vCPU, 2 GiB worker. Measure a real nightly run and a deep scan per account from the Cloud Run bill.
3. Database storage per million line items retained. The model uses 0.3 GB per million (rows plus indexes) at $0.35 per GB-month. Measure it from the Postgres bill once a real billing export is loaded.

Two that are not on the hosted bill but move the result as much: support minutes per tenant (Cloud has a 4-minute monthly budget; every extra minute is about 0.8 points on Cloud) and the share of international cards (Stripe 2.9% + $0.30, Stripe Billing 0.7%, international +1.5%). Invoiced plans carry Stripe ACH plus Stripe Billing's 0.7% of the invoice, an assumption that they are billed as Stripe subscriptions.

Not modeled: Stripe Tax (0.5% where nable is registered to collect), refunds and disputes, prompt cache writes (small next to the reads they serve; the high case spends the whole AI credit regardless), and vendor SSO (built in-house here; a vendor at about $125 a connection would take Growth well under 80%).

## Updating the model

The hosted product meters tokens, worker-seconds, jobs, line items and guard events per tenant per day. The monthly margin report runs `plan_cogs` on metered quantities, so model and bill are compared with one function. When any tenant's metered COGS is above 20% of its price two months in a row, review that plan's caps here, and the test keeps the result honest. Model prices are read from `finops.llm_prices`, and the Pro and Team prices from `finops.license.PLANS`, so a change in either place re-runs the check.
