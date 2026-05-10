"""
PR cost impact comments for GitHub and GitLab.

Webhook server that listens for pull request events, parses infrastructure
diffs (Terraform, CDK, CloudFormation, Helm), estimates monthly cost
impact, and posts a factual comment on the PR.

Run:
  finops-pr-webhook

Required env vars:
  GITHUB_TOKEN             — PAT with repo:write scope
  GITHUB_WEBHOOK_SECRET    — secret for validating webhook payloads
  ANTHROPIC_API_KEY        — for parsing complex diffs

Optional:
  PR_WEBHOOK_PORT          — HTTP port (default 8080)
  PR_COST_THRESHOLD_USD    — only comment if estimated impact > $N/mo (default 10)
"""
