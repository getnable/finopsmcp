# Contributing to nable

Thanks for your interest in contributing.

## Set up

nable needs Python 3.11 or newer; CI tests 3.11 and 3.12.

```bash
git clone https://github.com/getnable/finopsmcp
cd finopsmcp
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

`[dev]` adds pytest, pytest-asyncio, ruff and the Google Cloud Recommender SDK
(its tests run against Google's real types). In a fresh venv the install takes
about half a minute. The other cloud SDKs (`[azure]`, `[gcp]`, `[all]`) are not
needed for the tests, which stub them, and CI does not install them.

Try it without a cloud account:

```bash
nable scan --demo          # the terminal scan on sample data
nable guard try            # four sample commands through the guard, nothing executed
nable --help               # every command, grouped
```

`finops-mcp` (and `nable` with no arguments) starts the MCP server only when an
editor runs it over stdio; in a terminal it starts onboarding instead. To try
the server, point Claude Desktop, Cursor or another MCP client at the
`finops-mcp` command in your venv. `finops setup` connects a provider.

## Tests

```bash
pytest -q
```

That is what CI runs (`.github/workflows/test.yml`), on 3.11 and 3.12. The
suite is about 7,300 tests and takes 12 to 16 minutes on a 4-core machine; the
time is spread across many tests rather than a few slow ones (the slowest
single test is about 13 seconds). Nothing in it needs cloud credentials or the
network: a session fixture in `tests/conftest.py` makes any billed AWS call
(Cost Explorer, Athena, Cost Optimization Hub) fail the test that reached it,
and the keyring, the guard ledger, the org model and the packs root are
sandboxed per test. Unset your `AWS_*` variables anyway, so a test that
forgets a stub fails instead of reading your account.

While you work, run the files for the area you touched (measured under load:
the docs set below in about 4 seconds, the guard's 31 files in about 2 minutes):

```bash
pytest -q tests/test_guard*.py                 # the guard
pytest -q tests/test_packs*.py                 # packs
pytest -q tests/test_capabilities_doc_counts.py tests/test_upgrade_pin.py \
          tests/test_release_gap.py            # the numbers the docs state, the version carriers
```

then the full suite once before you open a pull request.

Run as root (a container, some CI images), one test fails by design:
`tests/test_guard_settings_safety.py::test_an_unwritable_settings_file_does_not_corrupt_it`
makes a file unwritable with `chmod`, which root ignores.

## Lint and security checks

CI does not run ruff, and the tree has a lint backlog: `ruff check src/` reports
findings on code you did not touch. Lint what you change, and keep it clean:

```bash
ruff check <the files you changed>
```

`[dev]` pins only `ruff>=0.4`, and new ruff releases add rules, so two
machines can disagree; say which version you ran if it matters.

`.github/workflows/security.yml` runs on every pull request and fails on:

```bash
bandit -r src -q --severity-level high --confidence-level high
detect-secrets-hook --baseline .secrets.baseline $(git ls-files)
pip-audit                                     # on an install of the package alone
```

Install them with `pip install bandit detect-secrets pip-audit` to run them
first. A new false positive in detect-secrets is accepted by regenerating the
baseline (`detect-secrets scan --baseline .secrets.baseline`) and committing it.

## Adding an MCP tool

Tools live in `src/finops/tools/<area>.py`, registered with `@_srv.mcp.tool()`
and imported in `src/finops/server.py`. The suite holds every tool to a few
rules, and a new one fails CI until it follows these:

- It belongs to exactly one family in `src/finops/tool_surface.py`, which
  decides when it is listed (`tests/test_tool_surface.py`).
- It never takes a secret as an argument (`tests/test_audit_security.py`).
- `CAPABILITIES.md` and `docs/tool-inventory.txt` state the new count and name
  (`tests/test_capabilities_doc_counts.py`).

One more that review, not a test, has to catch: a tool that writes or deletes
anything goes in `WRITE_TOOLS` or `DESTRUCTIVE_TOOLS` in the same file, so its
`readOnlyHint` and `destructiveHint` annotations tell the client the truth.

## Adding a connector

### SaaS provider (Datadog, Snowflake, etc.)

1. Create `src/finops/connectors/saas/<name>.py`. Follow an existing connector
   (e.g. `datadog.py`): subclass `BaseConnector` (`connectors/base.py`) and
   implement `is_configured()`, `get_costs()` and `list_accounts()`.
2. Add the provider's setup to `setup_wizard.py` so `finops setup` can prompt
   for its keys, and its credential to `connector_scopes.py`.
3. Register it in `SAAS_CONNECTORS` in `server.py` if it is a cost source.
4. List it in the Connectors table in `README.md` and in `CAPABILITIES.md`,
   saying exactly what it reads. A connector that can only check a key is not
   a source of spend, and the docs say so.

### AWS/Azure/GCP service

For most new cloud services, you do not need a new connector file. The universal connector in `src/finops/connectors/universal.py` already handles any service via the provider's native cost API:

- AWS: Cost Explorer SERVICE dimension (200+ services)
- Azure: Cost Management ServiceName dimension
- GCP: Cloud Billing BigQuery export

If the service has a short name or abbreviation users will type, add it to `_AWS_ALIASES` in `universal.py`.

For services that need deeper analysis beyond cost data (e.g., CloudWatch metrics, rightsizing, utilization), create a dedicated file under `src/finops/connectors/aws_services/<name>.py` and wire up a tool as above.

## Docs

A number, a "never" or an "always" in the docs is a claim a reader will check
against the code. Back it with a test where that is cheap: the tool and command
counts, the version carriers, the prices and the margin tables already are.
No em dashes in the plugin's docs and skill (`tests/test_guard_plugin.py`).

## PR guidelines

- Keep PRs focused: one feature or fix per PR.
- Add a test for any new connector or behaviour.
- Run the full suite and lint your changed files before opening a PR.
- Follow the existing code style (no em dashes in comments or docstrings).
- Update `README.md` if you add or change user-facing behaviour.

Releases are cut by the maintainers: [docs/RELEASING.md](../docs/RELEASING.md).

## Security issues

Do not open a public issue for security vulnerabilities. See [SECURITY.md](SECURITY.md) for the responsible disclosure process.
