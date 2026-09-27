# Commitments with bounds

`io.github.getnable/commitments-bounds` puts limits on commitment advice and
on commitment purchases an agent tries. nable never buys a commitment; this
pack makes sure what it recommends, and what an agent attempts, stays inside
the org's bounds, and says which bound when it does not.

## What it does

**Commitment bounds** (`policies/bounds.yaml`):

| Bound | Default | What happens to a recommendation outside it |
|---|---|---|
| `coverage_target_pct` | 80 | cut to the amount that reaches the target; dropped when coverage is already at or over it |
| `max_term_months` | 12 | dropped (a shorter offering has other rates, so it is not re-priced) |
| `payment_options` | no-upfront | dropped |
| `blackouts` | none | dropped when its term would run into a migration blackout over its scope |

nable applies these as a post-filter to every commitment purchase it
recommends: the Compute Savings Plan advice and its "if you bought more"
projection in `get_commitment_analysis`, and the Database Savings Plan
advice in `recommend_database_savings_plans`. A cut recommendation keeps its
shape with smaller figures and a `bounds` entry saying what was cut and by
which bound; a dropped one is listed under `cut_by_bounds` with why. Warnings
about unused commitments pass through untouched.

Bounds only restrict. Bounds from several packs combine to the strictest. A
figure nable does not have never loosens one: a scope nable cannot pin down
(a Compute Savings Plan spans every region) counts as overlapping a
blackout, and a recommendation with no coverage figure is dropped under a
coverage target. When an installed pack that provides policies cannot be
loaded, its bounds are unknown, so purchase advice is withheld until
`nable pack audit` is clean.

**Guard rules** (`guard/commitments.yaml`). Every commitment purchase an
agent tries asks a person, on AWS (Savings Plans and every reserved offering),
Google Cloud (committed use discounts) and Azure (reservations), and the
reason names the bound it would breach: the term when the command shows a
longer one, the payment option when it pays up front, and otherwise every
bound the person should check, since coverage and an offering's term cannot
be read from the command. nable's guard already asks about the AWS purchases
as one-way doors; the pack adds the bounds to that question.

**A policy for change records** (`policies/bounds.yaml`, rule
`agent-tried-to-buy-a-commitment`): with the change-control pack installed,
the CC8.1 evidence report lists an agent's attempt to buy a commitment as an
exception for review.

**A skill** (`skills/commitments-bounds/SKILL.md`) telling coding agents
never to buy, and to present advice as cut to the bounds.

## Setting your own bounds

Publish a copy of this pack under your org's namespace with your figures and
blackouts, sign it with your org key (`nable pack sign`), install it, and
remove this one. Another pack can only make these bounds stricter. A
blackout names a window with a UTC offset on each end, a reason, and an
optional scope (providers, types, services, regions, accounts); see the
comment in `policies/bounds.yaml`. Keep the figures in the guard rules'
reasons in step with the bounds.

## What it may read

| Capability | Why |
|---|---|
| `read_data = ["recommendations"]` | its bounds are applied to nable's commitment recommendations |
| `guard = "tighten-only"` | its guard rules only ever tighten a verdict |
| `max_autonomy = "L1"` | it recommends; it proposes no action and buys nothing |

It carries no code: nable validates it, and nothing in it runs.

## Secrets

None.

## Network

None.

## Signing

A first-party claim is honoured only with a signature from nable's
first-party key. The copy in this repository is unsigned; `nable pack
validate packs/commitments-bounds` says so, and install refuses it until a
release is signed.
