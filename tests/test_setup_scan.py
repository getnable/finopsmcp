"""The ambient credential scan behind `finops connect` and the welcome-flow
cross-sell. Getting every connector wired should take one keystroke when the
credentials are already on the machine; these tests guard the detection, the
already-connected suppression, the file probes, and the vault write path."""
import os

import pytest

import finops.setup_scan as scan


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Fresh vault in tmp, and an environment scrubbed of every env var the
    scanner looks for, so a developer's real keys never leak into assertions."""
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("FINOPS_VAULT_KEY", raising=False)
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    for _slug, (_name, required, optional) in scan.PROVIDER_ENV.items():
        for k in required + optional:
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    return tmp_path


def test_env_scan_finds_provider(isolated, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-1234567890")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-1234567890")
    found = scan.scan_ambient_credentials(home=tmp_path)
    names = {f["slug"] for f in found}
    assert "openai" in names
    f = next(f for f in found if f["slug"] == "openai")
    assert f["source"] == "environment"
    assert f["env"]["OPENAI_API_KEY"] == "sk-test-1234567890"
    assert f["env"]["OPENAI_ADMIN_KEY"] == "sk-admin-1234567890"  # optional captured


def test_partial_required_keys_not_found(isolated, monkeypatch, tmp_path):
    # Datadog needs API + APP key; only one present -> not a find.
    monkeypatch.setenv("DATADOG_API_KEY", "dd-api-key-value")
    found = scan.scan_ambient_credentials(home=tmp_path)
    assert not any(f["slug"] == "datadog" for f in found)


def test_already_connected_is_skipped(isolated, monkeypatch, tmp_path):
    from finops.security.vault import Vault
    Vault.default().store("ANTHROPIC_API_KEY", "sk-ant-stored")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-1234")
    found = scan.scan_ambient_credentials(home=tmp_path)
    assert not any(f["slug"] == "anthropic" for f in found)


def test_modal_toml_probe(isolated, tmp_path):
    (tmp_path / ".modal.toml").write_text(
        '[default]\ntoken_id = "ak-abc123"\ntoken_secret = "as-def456"\nactive = true\n'
    )
    found = scan.scan_ambient_credentials(home=tmp_path)
    f = next((f for f in found if f["slug"] == "modal"), None)
    assert f is not None
    assert f["env"] == {"MODAL_TOKEN_ID": "ak-abc123", "MODAL_TOKEN_SECRET": "as-def456"}
    assert "modal.toml" in f["source"]


def test_databrickscfg_probe(isolated, tmp_path):
    (tmp_path / ".databrickscfg").write_text(
        "[DEFAULT]\nhost = https://adb-123.4.azuredatabricks.net\ntoken = dapiabc123def\n"
    )
    found = scan.scan_ambient_credentials(home=tmp_path)
    f = next((f for f in found if f["slug"] == "databricks"), None)
    assert f is not None
    assert f["env"]["DATABRICKS_HOST"].startswith("https://adb-123")
    assert f["env"]["DATABRICKS_TOKEN"] == "dapiabc123def"


def test_gh_hosts_probe(isolated, tmp_path):
    ghdir = tmp_path / ".config" / "gh"
    ghdir.mkdir(parents=True)
    (ghdir / "hosts.yml").write_text(
        "github.com:\n    oauth_token: gho_testtoken123\n    user: someone\n"
    )
    found = scan.scan_ambient_credentials(home=tmp_path)
    f = next((f for f in found if f["slug"] == "github"), None)
    assert f is not None
    assert f["env"]["GITHUB_TOKEN"] == "gho_testtoken123"
    assert "gh CLI" in f["source"]


def test_env_beats_file_probe(isolated, monkeypatch, tmp_path):
    # GITHUB_TOKEN in the environment wins over the gh hosts file.
    ghdir = tmp_path / ".config" / "gh"
    ghdir.mkdir(parents=True)
    (ghdir / "hosts.yml").write_text("github.com:\n    oauth_token: gho_filetoken\n")
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_envtoken")
    found = scan.scan_ambient_credentials(home=tmp_path)
    f = next(f for f in found if f["slug"] == "github")
    assert f["source"] == "environment"
    assert f["env"]["GITHUB_TOKEN"] == "github_pat_envtoken"


def test_connect_finding_stores_in_vault(isolated, tmp_path):
    ok = scan.connect_finding({
        "slug": "together", "name": "Together AI", "source": "environment",
        "env": {"TOGETHER_API_KEY": "tok-xyz-12345"},
    })
    assert ok
    from finops.security.vault import Vault
    assert Vault.default().get("TOGETHER_API_KEY") == "tok-xyz-12345"


def test_gcloud_adc_path(isolated, tmp_path):
    assert scan.gcloud_adc_path(home=tmp_path) is None
    adc = tmp_path / ".config" / "gcloud"
    adc.mkdir(parents=True)
    (adc / "application_default_credentials.json").write_text("{}")
    p = scan.gcloud_adc_path(home=tmp_path)
    assert p is not None and p.name == "application_default_credentials.json"


def test_offer_ambient_connections_connects_on_yes(isolated, monkeypatch, tmp_path):
    monkeypatch.setenv("MISTRAL_API_KEY", "mk-1234567890")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    n = scan.offer_ambient_connections()
    assert n == 1
    from finops.security.vault import Vault
    assert Vault.default().get("MISTRAL_API_KEY") == "mk-1234567890"


def test_offer_ambient_connections_silent_when_nothing(isolated, monkeypatch, tmp_path):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("prompted with nothing to offer")
    monkeypatch.setattr("builtins.input", _boom)
    assert scan.offer_ambient_connections() == 0


def test_key_help_covers_every_saas_setup_provider():
    """Every provider wired through setup_saas_api_key must have a KEY_HELP
    entry (a url or an explicit no-url hint), so no setup flow ever strands
    the user without directions to their key."""
    import re as _re
    from pathlib import Path
    src = (Path(__file__).parent.parent / "src" / "finops" / "setup_wizard.py").read_text()
    names = set(_re.findall(r'setup_saas_api_key\("([^"]+)"', src))
    names.discard("provider_name")  # the def itself
    missing = sorted(n for n in names if n not in scan.KEY_HELP)
    assert not missing, f"KEY_HELP missing entries for: {missing}"


def test_scan_names_map_to_real_setup_slugs():
    """`finops setup <slug>` must exist for every slug the scanner reports, so
    the fallback instruction in the report is always runnable."""
    import re as _re
    from pathlib import Path
    src = (Path(__file__).parent.parent / "src" / "finops" / "setup_wizard.py").read_text()
    dispatch = set(_re.findall(r'^\s+"([a-z0-9-]+)": (?:lambda|setup_)', src, flags=_re.M))
    missing = sorted(s for s in scan.PROVIDER_ENV if s not in dispatch)
    assert not missing, f"scanner slugs with no setup dispatch: {missing}"
