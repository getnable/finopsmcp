"""Suite-wide guards.

The OS keychain is per-macOS-user, not per-$HOME or per-FINOPS_DATA_DIR, so any
test that reaches the real `keyring` module reads (and on first-run generation,
rewrites) the developer's actual keychain. On macOS that is a password dialog
per suite run, and a rewrite resets the item ACL so "Always Allow" never sticks.
This is exactly how `test_setup_scan.py`'s Vault.default() calls turned every
pytest run into a keychain prompt.

Every test gets an in-memory keyring stub. Tests that assert keyring behavior
(test_keychain_prompts.py, test_keyring_cache.py) install their own counting
stubs on top via monkeypatch, which override this one for their duration.
"""
import os
import sys
import types
from pathlib import Path

import pytest

# Set this to run the suite against a finops installed somewhere else on
# purpose, e.g. testing a built wheel rather than the working tree.
_ALLOW_FOREIGN = "FINOPS_TESTS_ALLOW_FOREIGN_SOURCE"


def pytest_configure(config):
    """Refuse to run this tree's tests against a different tree's source.

    finops is installed editable, pointing at ONE checkout. A git worktree gets
    its own tests/ and its own src/, but `import finops` still resolves to
    whichever checkout the install points at, so running pytest from a worktree
    silently pairs that worktree's test files with the main checkout's code.

    Nothing errors. You get a normal-looking run whose result means nothing, and
    the failures it invents look exactly like real ones. On 2026-08-22 that cost
    two wrong conclusions in a row about the same test: first "this fails on
    main" (it did not; the source was a feature branch), then "this branch broke
    it" (it had not; the test had been renamed on that branch, so the old name
    was being run against new code). Both readings were confidently reported
    before anyone noticed the trees were crossed.

    The fix a person needs is one environment variable, so the message says it
    rather than describing the problem and leaving them to it. Failing here in
    pytest_configure stops the run with a single line, instead of thousands of
    confusing test failures further down.
    """
    if os.environ.get(_ALLOW_FOREIGN, "").strip().lower() in ("1", "true", "yes"):
        return
    try:
        import finops
    except Exception:                      # pragma: no cover - collection handles it
        return

    tests_tree = Path(__file__).resolve().parent.parent
    expected = (tests_tree / "src" / "finops").resolve()
    actual = Path(finops.__file__).resolve().parent
    if actual == expected:
        return

    pytest.exit(
        "\n"
        "  These tests and the finops they import are from different trees.\n\n"
        f"    tests come from : {tests_tree}\n"
        f"    finops imports  : {actual}\n"
        f"    expected        : {expected}\n\n"
        "  finops is installed editable against one checkout, so a worktree's\n"
        "  tests run against the main checkout's source unless you say otherwise.\n"
        "  Whatever this run reported would have been about neither tree.\n\n"
        f"    PYTHONPATH={tests_tree / 'src'} pytest ...\n\n"
        f"  Deliberately testing an installed build? Set {_ALLOW_FOREIGN}=1.\n",
        returncode=4,
    )


@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch):
    store: dict = {}
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service, user: store.get((service, user))
    fake.set_password = lambda service, user, value: store.__setitem__((service, user), value)
    fake.delete_password = lambda service, user: store.pop((service, user), None)
    monkeypatch.setitem(sys.modules, "keyring", fake)

    # The vault and trial caches are module-global; clear them so a key cached
    # by one test never satisfies (or poisons) another.
    from finops.security.vault import Vault
    import finops.license as license_mod
    Vault._key_cache.clear()
    monkeypatch.setattr(license_mod, "_kr_cached_date", None)
    yield
    Vault._key_cache.clear()


@pytest.fixture(autouse=True)
def _guard_ledger_sandbox(monkeypatch, tmp_path_factory):
    """Every guard verdict is appended to a decision ledger under the user's
    data dir. A suite full of `terraform destroy` fixtures must not land in the
    developer's real audit log, so each test writes to a throwaway one (tests
    that read it back take its path from guard_ledger.ledger_path())."""
    import finops.guard_ledger as ledger
    monkeypatch.setattr(ledger, "_path_override",
                        tmp_path_factory.mktemp("guard-ledger") / ledger.LEDGER_NAME)


@pytest.fixture(autouse=True)
def _guard_switch_sandbox(monkeypatch, tmp_path_factory):
    """The guard's off switch (`nable guard off`, FINOPS_GUARD=off) and the
    Claude Code plugin's enabled state are read from the machine: nable's data
    directory and Claude Code's user settings. A developer who paused the
    guard, or has the nable plugin installed, must not change what the suite
    sees, so both point at empty throwaway directories. Tests of those lookups
    reset the overrides themselves."""
    import finops.guard_plugin as gp
    monkeypatch.delenv("FINOPS_GUARD", raising=False)
    monkeypatch.setattr(gp, "_data_root_override", tmp_path_factory.mktemp("finops-data"))
    monkeypatch.setattr(gp, "_user_dir_override", tmp_path_factory.mktemp("claude-user"))


@pytest.fixture(autouse=True)
def _org_model_sandbox(monkeypatch, tmp_path_factory):
    """The guard, tickets, findings, attribution and the workload classifier
    read the org model (finops.org), which lives in the developer's data dir
    or a repo's nable.org/. A developer with one must not change what the
    suite sees, so FINOPS_ORG_DIR points at an empty directory. Tests of the
    org model set or clear it themselves."""
    monkeypatch.setenv("FINOPS_ORG_DIR", str(tmp_path_factory.mktemp("org-model")))


@pytest.fixture(autouse=True)
def _packs_sandbox(monkeypatch, tmp_path_factory):
    """The guard reads installed packs (guard rules, price books) on every
    verdict. A developer with packs installed must not change what the suite
    sees, so the packs root is an empty directory; the packs tests point it
    elsewhere themselves (tests/packs_support.py)."""
    from finops import guard_packs
    from finops.packs import runtime, store
    monkeypatch.setattr(store, "_root_override", tmp_path_factory.mktemp("packs") / "packs")
    runtime.invalidate()
    guard_packs.invalidate()
    yield
    runtime.invalidate()
    guard_packs.invalidate()


@pytest.fixture(autouse=True)
def _no_ambient_service_creds(monkeypatch):
    """Unit tests must not inherit the developer's live service credentials.

    A dev box with GITHUB_TOKEN + GITHUB_ORGS set made an unpatched code path do a
    real GitHub org sweep inside a unit test (4-5s per run, network-flaky). Strip
    the vars suite-wide; tests that need them set their own via monkeypatch, which
    layers on top of this fixture for their duration.
    """
    for var in ("GITHUB_TOKEN", "GITHUB_ORGS"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_agent_usage(monkeypatch, tmp_path_factory):
    """The AI budget reads every agent harness on the machine: Claude Code's
    transcripts (tests point CLAUDE_CONFIG_DIR at a sandbox), Codex CLI's
    rollouts under CODEX_HOME, and Cursor's Admin API when a key is set. A dev
    box with a real ~/.codex, a Codex session id in its environment, or a
    Cursor key must not leak its usage (or a network call) into a unit test.
    Tests that exercise those readers set their own values on top. The guard's
    background refresh (background_refresh) is off under pytest unless a test
    turns it on, so a developer's own setting is cleared too."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path_factory.mktemp("codex-home")))
    for var in ("CODEX_SESSION_ID", "CURSOR_ADMIN_API_KEY", "CURSOR_ADMIN_USER_EMAIL",
                "FINOPS_CURSOR_API_URL", "FINOPS_GUARD_BACKGROUND_REFRESH",
                "FINOPS_GUARD_AUTO_REFRESH_BUDGET"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True, scope="session")
def _no_test_may_spend_money():
    """Hard block on every billed AWS API call for the whole test session.

    This exists because a test called `test_explain_cost_drivers_demo_answers`
    was making two real ce:GetCostAndUsage requests against the developer's own
    AWS account on every suite run. It patched demo_data.DEMO_MODE but not
    FINOPS_DEMO_FORCE, and is_demo() yields to real data whenever a provider is
    connected, so on any machine with credentials the "demo" test quietly
    exercised the live path and billed the owner per request.

    Nothing in a test suite should be able to do that, and no reviewer should
    have to notice it. Cost Explorer bills per request; Athena bills per byte
    scanned. Both raise here with the name of the test that reached them.

    A test that genuinely needs to exercise these must stub the client itself.
    """
    import botocore.client

    billed = {
        "ce": "Cost Explorer (billed per request)",
        "athena": "Athena (billed per byte scanned)",
        "cost-optimization-hub": "Cost Optimization Hub (billed per request)",
    }
    original = botocore.client.BaseClient._make_api_call

    def guarded(self, operation_name, api_params):
        service = self.meta.service_model.service_name
        if service in billed and not getattr(self, "_nable_billed_stub", False):
            raise AssertionError(
                f"A test reached {service}.{operation_name} — {billed[service]}. "
                f"This spends real money on whoever runs the suite. Stub the "
                f"client, or use demo data with FINOPS_DEMO_FORCE=1."
            )
        return original(self, operation_name, api_params)

    botocore.client.BaseClient._make_api_call = guarded
    try:
        yield
    finally:
        botocore.client.BaseClient._make_api_call = original


@pytest.fixture
def billed_stub():
    """The one way a test reaches a billed client: through botocore's Stubber.

    `with billed_stub(ce_client) as stub:` activates a Stubber on the client
    and lets it past the block above for the duration. The Stubber answers
    every call from its queue and raises on anything unqueued, so nothing
    reaches AWS; the request parameters and the queued responses are still
    checked against the real service model."""
    import contextlib

    from botocore.stub import Stubber

    @contextlib.contextmanager
    def _stub(client):
        with Stubber(client) as stub:
            client._nable_billed_stub = True
            try:
                yield stub
            finally:
                client._nable_billed_stub = False

    return _stub
