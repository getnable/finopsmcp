# Spec: Agent cost controls, v1

Source: /plan-ceo-review 2026-06-30 (HOLD SCOPE), the agent-vs-prompt re-center, and
the opt-in remediation decision, all 2026-06-30. Do not re-litigate the locked
decisions (advisory-by-default, cached budget baseline, MCP distribution, fail-by-stakes).

## What this is, and what it is not

This governs an AGENT'S RUN and the resources that run touches. It is not a model
router. A model router optimizes one call (per-inference, model-only, key-scoped, a
commodity: LiteLLM, Portkey, OpenRouter). This governs the whole trajectory of an
agent pursuing a goal: its model spend plus the cloud it provisions, per task, in the
loop, propose-only by default with an opt-in remediation mode. Model routing is one
minor ingredient here, never the headline.

The moat line: LLM gateways cap the model bill per API key. nable governs the agent's
whole footprint (model + the cloud it touches) per task, in the editor, propose-only,
because it is the only tool that sees the cloud bill.

## Context

Agents run loops of many model and tool calls toward a goal, and increasingly take
real-world actions (terraform apply, GPU jobs, spinning up scratch infra). Cost
accrues across the run without a human watching each step, and the big money is in the
cloud the agent provisions, not the tokens. No tool governs that today. nable already
sees the whole bill and already lives in the editor via MCP, so it is placed to be the
agent's cost conscience at the moment of action.

## Current state (verified 2026-06-30)

The engine mostly exists. This is agent-centric packaging plus the new pieces below.

| Piece | Where | Notes |
|-------|-------|-------|
| Pre-action gate | `check_action_policy` at `server.py:8057` (calls `estimate_change_cost` at `server.py:7954`) | Already agent-right: it gates a proposed ACTION. Advisory. Keep, lead with it. |
| Policy classification | `evaluate_action_gate` at `policy.py:71` | allow/block/escalate by `door_of` (two-way vs one-way) + `max_auto_monthly_usd`. This is the safety spine for remediation modes. |
| Savings ledger + ROI | `get_savings_ledger`, `get_nable_roi`, `verify_savings`, `get_savings_summary` | Reuse for the savings view. |
| Model routing | `route_request` at `slack_bot/llm.py:156` | INGREDIENT only, not the identity. |
| Personas | `_SANDBOX_AGENTS` at `server_web.py:1444` | rca, reco, arch. |
| MCP registration | `@mcp.tool()` on `mcp = FastMCP("nable")` at `server.py:64` | |

Gaps (the agent-specific work): a run/task budget spanning model + cloud, orphaned-
resource and loop-waste detection, attribution by agent/task/goal, and the opt-in
remediation mode.

## Proposed change

### T1: run/task cost tracking + budget (the agent-specific core)

An agent (or the wrapper around it) reports its task: `agent_id`, `task_id`, an
optional `goal`, and a task budget. nable accumulates the run's cost = model spend
(from the token usage the agent loop already surfaces) + the cloud impact of the
actions it takes or proposes (from `estimate_change_cost`). Returns a run verdict:
within budget / approaching / over, with the split by model vs cloud. This is the unit
a model router does not have: the whole run, not one call. Reuses the budget enforcer.

Run lifecycle: a local run row opens on the first gate call for a `task_id`,
accumulates across the run, and closes on an explicit end signal or a TTL (default a
few hours), so an agent that never signals done cannot leave a run accumulating forever
or wrongly block a task budget.

### T2: pre-action gate on what the agent DOES

The existing `check_action_policy` on terraform / helm / infra actions, extended to
return the cheaper path and the budget block with an age label. Propose-only by
default. This is the agent's actions in the world, not its prompts. Return shape:

```
{
  "gate": "allow" | "warn" | "block" | "escalate",
  "reason": "...",
  "action_type": "...", "door": "two_way" | "one_way",
  "monthly_delta_usd": 4100.0,
  "cost": {                         // present only when a change was described
    "verdict": "ok|warn|over_budget|no_budget",
    "monthly_delta_usd": 4100.0, "annual_delta_usd": 49200.0,
    "budget": {"name":"platform","limit_usd":20000,"current_run_rate_usd":15200,
               "projected_pct_of_limit":91.2,"headroom_usd":-100,
               "as_of":"2026-07-01T02:00:00Z","age_hours":6.0},   // null if no budget
    "breakdown": [ ... ]
  },
  "cheaper_path": {"summary":"...","estimated_monthly_usd":1200.0,
                   "estimated_saving_usd":2900.0,"is_estimate":true},  // null if none
  "remediation": {"mode":"propose"|"auto","applied":false},
  "policy_note": "..."
}
```

Contract note: the budget block, `annual_delta_usd`, and `breakdown` are nested under
`cost`, not at the top level. An agent reads the budget age at
`result["cost"]["budget"]["age_hours"]`; `cheaper_path` and `remediation` are top-level.

### T3: agent-loop + orphaned-resource waste

Tag resources an agent provisions (an `agent_id`/`task_id` tag on create actions it
proposes) and flag ones still running after the task ends: "your agents left N
orphaned resources this month." Detect redundant or looping tool calls within a run.
This is agent-specific waste a router cannot see.

### T4: attribution + savings by agent/task/goal (measured vs estimated)

Write the value the controls deliver into the existing savings ledger, tagged source
`agent_control`, attributed to agent/task/goal:
- Cheaper path taken (MEASURED on apply, ESTIMATED on propose): monthly delta saved.
- Orphaned resource cleaned up (MEASURED): the recovered run-rate.
- Blocked or escalated over-budget action (ESTIMATED, counterfactual): avoided spend,
  labeled an estimate, never blended into the measured total.
- Model routing (MEASURED, minor): cost saved by a cheaper model that fit.

The view (a dashboard card + `get_agent_savings`, or extend `get_savings_summary`):
"your coding agents cost $X this month, and the controls saved $Y by catching Z
oversized infra changes and cleaning up N orphaned resources," split MEASURED vs
ESTIMATED so the number survives a buyer's scrutiny. Reuses the `reco` persona's
envelope discipline: a measured number and a counterfactual are never one headline.

### T5: remediation mode, opt-in (propose vs auto)

A per-policy setting `remediation_mode`, default `propose`:

- `propose` (DEFAULT, everyone): nable drafts the fix (PR or ticket), a human applies.
  Today's behavior. Stays the default so the trust story holds for anyone who does not
  opt in.
- `auto` (OPT-IN, default off): nable applies a fix by itself ONLY when ALL hold:
  1. `evaluate_action_gate` returns `allow`, and
  2. the action is a two-way door (`is_one_way(action_type)` is False), and
  3. the cost is within `max_auto_remediation_usd` (a separate cap from the action
     threshold), and
  4. it targets the agent's OWN footprint, proven by nable's local run-provenance
     ledger: a resource nable itself recorded the agent creating during this run, or
     an action type in an explicit `auto_remediation_allowlist`. A cloud tag is NOT
     sufficient (it is mutable by anything with write access); absent a provenance
     record, escalate to a human. Fail closed.

  Examples it may auto-do: tear down an orphaned scratch DB the agent created, cancel
  the agent's redundant job, apply the cheaper model choice.

**The line that does not move (safety spine, all non-negotiable defaults):**
- One-way doors (delete, terminate, purchase a commitment), over-budget actions, and
  anything on customer or production infrastructure ALWAYS escalate to a human, even
  in `auto` mode. Auto never does anything irreversible and never touches the
  customer's real infrastructure.
- Auto is bounded by `max_auto_remediation_usd/mo`.
- Every auto action is audited (what, when, cost, reversal path) and reversible.
- A kill switch (`FINOPS_REMEDIATION_MODE=propose` env, or the policy) reverts to
  propose instantly.
- The own-footprint auth boundary is nable's local run-provenance ledger, never a
  cloud tag. No provenance record for a resource means escalate, not auto.
- `door_of` MUST default an unknown or unclassified `action_type` to `one_way`
  (escalate), so a new action type can never auto-fire before it is deliberately
  classified and added to the allowlist.

This reuses `policy.py` exactly: auto mode changes only what happens on an
already-safe `allow` + two-way + in-budget verdict (apply instead of propose). The
`escalate` and `block` paths are untouched, so propose-only's guarantees hold for
everything dangerous. This is the opt-in enforcement mode deferred in CD-1.

## Locked behavior (from the CEO review, carried in)

- Advisory by default (T5 `propose`); `auto` is the opt-in exception, bounded above.
- Budget baseline is the cached local snapshot with an `age_hours` label. No live Cost
  Explorer call on the gate path. The action's own cost is priced fresh per call.
- Distribution: MCP tools + a published `cost_guard` instruction pattern, baked into
  nable's own persona. Rides `uvx nable`.
- Fail-by-stakes when the gate is unreachable: two-way/cheap fail-open with a logged
  `ungated` note; one-way/large fail-closed to a human. Reuses `door_of`/`is_one_way`.

## Acceptance criteria

1. A run with `agent_id`/`task_id` accumulates model + cloud cost against a task
   budget and returns a run verdict split by model vs cloud.
2. `check_action_policy` returns gate + monthly/annual $ + budget-with-age +
   cheaper_path + the remediation block in one response.
3. Orphaned-resource detection flags a resource tagged agent-created that is still
   running after its task, and T4 records the recovered run-rate as MEASURED.
4. The savings view reports MEASURED and ESTIMATED savings separately, attributed to
   agent/task, and never sums them into one headline.
5. `remediation_mode` defaults to `propose`. An EXHAUSTIVE parametrized test over every
   allowlisted action type crossed with door and budget states proves auto applies IF
   AND ONLY IF `allow` + two-way + in-cap + a provenance record; a one-way, over-budget,
   unclassified, or no-provenance action always escalates. An unknown action type is
   treated as one-way.
6. Every auto action writes an audit record with a reversal path; the kill switch
   reverts to propose within one request.
7. Propose-only guarantees hold: no `escalate`/`block` action is ever auto-applied, in
   any mode. Covered by tests.
8. Fail-by-stakes: gate-data-unavailable returns `allow`+ungated for a two-way door,
   `escalate` for a one-way. Covered by tests.
9. Full `pytest -q` suite green.

## Testing plan

| Layer | What | Count |
|-------|------|-------|
| Unit | gate branches (exist), cheaper_path, age_hours, fail-by-stakes door branch, the exhaustive auto-mode invariant (apply iff allow+two-way+in-cap+provenance), door_of unknown-type defaults to one_way, provenance fail-closed, run lifecycle + TTL, kill switch | +12 |
| Integration | run budget across model + cloud; `check_action_policy` end to end on a Terraform fixture in propose and auto modes; orphaned-resource flag | +4 |
| Eval | `cost_guard` persona calls the gate before an apply and honors escalate | +1 |

## Rollback

New optional tool fields, one persona, a policy setting defaulting to the safe
`propose`, additive only. Revert the commit. Kill switch is `FINOPS_REMEDIATION_MODE`.
No migration.

## Effort (human / CC)

- T1 run/task budget across model + cloud: ~2 days / ~45 min
- T2 pre-action gate extensions: ~1 day / ~30 min
- T3 orphaned-resource + loop waste: ~1.5 days / ~40 min
- T4 attribution + savings view: ~1.5 days / ~35 min
- T5 opt-in remediation mode + safety spine + audit/kill switch: ~2 days / ~45 min

## Files reference

| File | Change |
|------|--------|
| `src/finops/server.py:8057` | Extend `check_action_policy`: cheaper_path, budget+age, remediation block, `agent_id`/`task_id`/`goal` params |
| `src/finops/policy.py:71` | Reuse gate; add cheaper_path hook, fail-by-stakes helper, the four auto-mode preconditions |
| `src/finops/slack_bot/llm.py:156` | Reuse `route_request` (ingredient), surface run token usage for T1 |
| `src/finops/budget/enforcer.py` | Run/task budget spanning model + cloud |
| `src/finops/recommendations/savings_tracker.py` | `agent_control` source, measured vs estimated, per-agent/task attribution |
| `src/finops/server_web.py:1444` | `cost_guard` persona |
| `src/finops/static/dashboard.html` | Agent-savings card, orphaned-resource view |
| `src/finops/remediation*` (new/extend) | The `auto` apply path, audit record, reversal, kill switch |
| `tests/test_agent_cost_controls.py` (new) | T1-T5 coverage, propose-only invariants |

## Out of scope (deferred)

- In-path enforcement / proxy (advisory + opt-in auto only, never a network proxy).
- Framework SDK / middleware (MCP + instruction pattern only).
- Live per-call budget fetch (cached baseline only).
- Model routing as a headline feature. It is an ingredient, not the product.
- The hosted-dashboard approve UI for escalations (return contract only in v1).
