# Contributing to nable

Thanks for your interest in contributing.

## Running locally

```bash
git clone https://github.com/nable-sh/finops-mcp
cd finops-mcp
pip install -e ".[dev]"
finops setup          # connect at least one provider
finops-mcp            # start the MCP server
```

Run tests with:

```bash
pytest
```

## Adding a connector

1. Create `src/finops/connectors/<name>.py`. Follow the pattern in an existing connector (e.g., `datadog.py`): implement a `fetch_costs()` function that returns a list of cost records.
2. Register it in `src/finops/connectors/__init__.py`.
3. Add the required env vars to `setup_wizard.py` so `finops setup` can prompt for them.
4. Document the connector in `web/docs.html` (copy the pattern from an existing connector section) and add it to the nav and the env-var table.

## PR guidelines

- Keep PRs focused: one feature or fix per PR.
- Add a test for any new connector or behaviour.
- Run `pytest` and fix any failures before opening a PR.
- Follow the existing code style (black, ruff).
- Update `web/docs.html` if you add or change user-facing behaviour.

## Security issues

Please do not open a public issue for security vulnerabilities. See [SECURITY.md](SECURITY.md) for the responsible disclosure process.
