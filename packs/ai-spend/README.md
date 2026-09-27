# AI spend

A data pack for teams that spend on LLM APIs and GPUs. It adds no connector:
nable already reads OpenAI projects, Anthropic workspaces, Bedrock, Vertex AI,
OpenRouter, LiteLLM and Langfuse, and the coding agents' own usage (Claude
Code, Codex, Cursor). This pack turns that data into a policy, guard rules, a
skill and a report. Nothing in it runs.

```
nable pack validate packs/ai-spend
nable pack install <a signed release of io.github.getnable/ai-spend>
nable pack report io.github.getnable/ai-spend --days 30
```

`--days 30` is the same as `--since 30d`; `--until`, `--json` and `--out`
work as for any report.

## What it does

| Content | File | What it does |
|---|---|---|
| Policy | `policies/attribution.yaml` | Flags a period where less than 80% of AI spend over 100 USD carries a `feature:` tag or a `customer:` tag, and escalates when more than 5,000 USD has no feature tag. |
| Guard rules | `guard/gpu.yaml` | Every GPU or accelerator launch asks, whatever it costs: AWS p2 to p6 (p4d, p5, p6-b200), g3 to g6 (g4dn, g5, g6e), gr6, trn, inf and dl families, SageMaker `ml.*` types included; GCP a2, a3, a4, g2, g4 and TPU machine types or any `--accelerator`; Azure N-series (NC, ND, NV, NG); and MCP tools that launch compute with one of those types. |
| Skill | `skills/check-ai-budget/SKILL.md` | Tells a coding agent to call `check_ai_budget` with its token estimate before a long task, relay the verdict and let the person decide. |
| Report | `reports/ai-spend.md` | AI spend by vendor, model, feature and customer, the tagged share, what the policy flags, and what the numbers do not cover. |

### The attribution convention

No AI vendor records a feature or a customer. Tag each LLM call with
`feature:<name>` and, where there is one, `customer:<id>`, as a LiteLLM
request tag or a Langfuse trace tag. The report reads those tags through
`get_ai_cost_attribution`. When LiteLLM and Langfuse both log a call, a tag's
spend is the larger of the two, never their sum. Bedrock and Vertex have no
request tags, so their spend is split by model only.

### The GPU rules only tighten

The guard already asks when a launch it can price is over the auto threshold
(500 USD a month by default). A single g4dn.xlarge is about 380 USD a month,
and a GCP g2 or an Azure NC launch is not priced at all, so each would pass.
These rules lower the threshold for GPU and accelerator families to zero:
every such launch asks. They can turn an allow into an ask and nothing else;
an ask or a deny from the guard or the org policy stands.

## What it reads

- `focus.cost` (declared in `read_data`): the report reads AI spend nable
  already has, through `get_llm_costs` and `get_ai_cost_attribution`, with
  the provider keys nable is already set up with. The pack itself holds no
  key and reads nothing: the core fills the report.
- The guard rules read the command or MCP call the guard is judging, as every
  guard rule does.

## Secrets

None. The report uses the provider connections nable already has
(`nable openai`, `nable anthropic`, `nable litellm`, `nable langfuse`).

## Network

None declared, and the pack runs no code. Reading AI spend for the report
reaches the providers nable is connected to, as `get_llm_costs` does today.

## Signing

Releases are signed with nable's first-party key (`support =
"first-party"`). A first-party claim is honoured only with a signature from
that key. The copy in this repository is unsigned; `nable pack validate
packs/ai-spend` says so, and install refuses it until a release is signed.
The pack carries no code, so no allowlist entry is needed to use it.
