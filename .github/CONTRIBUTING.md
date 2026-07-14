# Contributing to nable

Thanks for your interest in contributing.

## Running locally

```bash
git clone https://github.com/getnable/finopsmcp
cd finopsmcp
pip install -e ".[dev]"
finops setup          # connect at least one provider
finops-mcp            # start the MCP server
```

Run tests with:

```bash
pytest
```

## Adding a connector

### SaaS provider (Datadog, Snowflake, etc.)

1. Create `src/finops/connectors/saas/<name>.py`. Follow the pattern in an existing connector (e.g., `datadog.py`): implement a class with a `fetch_costs()` method that returns cost records.
2. Add the required env vars to `setup_wizard.py` so `finops setup` can prompt for them.
3. Wire up an MCP tool in `server.py` following the existing tool pattern.
4. Add the provider to the connector list in `README.md`.

### AWS/Azure/GCP service

For most new cloud services, you do not need a new connector file. The universal connector in `src/finops/connectors/universal.py` already handles any service via the provider's native cost API:

- AWS: Cost Explorer SERVICE dimension (200+ services)
- Azure: Cost Management ServiceName dimension
- GCP: Cloud Billing BigQuery export

If the service has a short name or abbreviation users will type, add it to `_AWS_ALIASES` in `universal.py`.

For services that need deeper analysis beyond cost data (e.g., CloudWatch metrics, rightsizing, utilization), create a dedicated file under `src/finops/connectors/aws_services/<name>.py` and wire up a tool in `server.py`.

## PR guidelines

- Keep PRs focused: one feature or fix per PR.
- Add a test for any new connector or behaviour.
- Run `ruff check src/` and `pytest` before opening a PR.
- Follow the existing code style (ruff, no em dashes in comments or docstrings).
- Update `README.md` if you add or change user-facing behaviour.

## Security issues

Do not open a public issue for security vulnerabilities. See [SECURITY.md](SECURITY.md) for the responsible disclosure process.
