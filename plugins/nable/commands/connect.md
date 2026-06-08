---
description: Connect a cloud account and verify nable works, with a guided first cost number
---

You are helping the user connect nable (their local-first FinOps copilot) and confirm it works, right here inside the editor. Be concise and concrete. No em dashes.

Do these steps in order:

1. Check what nable can already see. Call `list_connected_providers` and `check_connector_health`. AWS often works with zero extra setup because nable uses the machine's existing AWS credential chain (environment variables, `~/.aws/credentials`, IAM role, SSO).

2. If at least one provider is connected and healthy:
   - Call `get_cost_summary` for the last 30 days. Report the total, the top 3 services, and the trend versus the prior period.
   - Call `get_llm_costs` (or `get_llm_cost_by_model`). If there is AI/LLM spend, report it and what share of the total bill it is.
   - Suggest three questions they can ask next, for example: "what drove our bill up last week?", "which EC2 instances should we downsize?", "what is our spend by model?"
   - Stop here. Do not tell them to run any setup. They are already connected.

3. If nothing is connected (no credentials found):
   - Say in one line that nable reads costs locally with their own credentials and nothing leaves their machine.
   - Tell them to run ONE of these in their terminal, matching their provider:
     - AWS: `uvx --from finops-mcp finops setup aws`
     - Azure: `uvx --from finops-mcp finops setup azure`
     - GCP: `uvx --from finops-mcp finops setup gcp`
   - Mention they can preview on sample data first with `uvx --from finops-mcp finops welcome --demo`.
   - Tell them to re-run this command to verify once they have finished.

4. If a provider is configured but unhealthy (auth error or a missing permission), read the health output and give the specific fix, for example the exact IAM permission that is missing, then the command to re-authenticate.

Lead with the cost number whenever you have one. Never invent numbers. Only report what the tools return.
