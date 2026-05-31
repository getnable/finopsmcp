"""Tests for named profile support (Feature 2)."""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Tests: storage/db.py profile-aware path selection
# ---------------------------------------------------------------------------

class TestDbProfilePath:
    def test_default_profile_uses_home_finops(self, tmp_path):
        """Without FINOPS_PROFILE, db lives in ~/.finops/finops.db."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FINOPS_PROFILE", None)
            os.environ.pop("FINOPS_DB_PATH", None)
            os.environ.pop("FINOPS_DATA_DIR", None)
            os.environ.pop("DATABASE_URL", None)

            from src.finops.storage import db as db_module
            # Reset cached state
            db_module._DATA_DIR = None
            db_module._ENGINE = None

            data_dir = db_module.data_dir()
            assert data_dir == Path.home() / ".finops"

    def test_profile_env_var_changes_data_dir(self, tmp_path):
        """FINOPS_PROFILE redirects data_dir to ~/.finops/profiles/{name}/."""
        with patch.dict(os.environ, {"FINOPS_PROFILE": "clientA"}, clear=False):
            os.environ.pop("FINOPS_DATA_DIR", None)

            from src.finops.storage import db as db_module
            db_module._DATA_DIR = None
            db_module._ENGINE = None

            with patch("src.finops.storage.db.Path") as MockPath:
                # Use real Path logic but intercept the final .mkdir
                real_path = Path.home() / ".finops" / "profiles" / "clientA"
                MockPath.home.return_value = Path.home()
                # Let it fall through to real behavior
                pass

            # Re-import fresh to get real behavior
            import importlib
            import src.finops.storage.db
            importlib.reload(src.finops.storage.db)
            src.finops.storage.db._DATA_DIR = None
            src.finops.storage.db._ENGINE = None

            data_dir = src.finops.storage.db.data_dir()
            expected = Path.home() / ".finops" / "profiles" / "clientA"
            assert data_dir == expected

    def test_profile_dir_created_on_access(self, tmp_path):
        """Profile directory is created automatically when data_dir() is called."""
        profile_name = "test-auto-create"
        profile_root = Path.home() / ".finops" / "profiles" / profile_name

        # Clean up if it already exists
        if profile_root.exists():
            import shutil
            shutil.rmtree(profile_root)

        try:
            with patch.dict(os.environ, {"FINOPS_PROFILE": profile_name}, clear=False):
                os.environ.pop("FINOPS_DATA_DIR", None)

                from src.finops.storage import db as db_module
                db_module._DATA_DIR = None
                db_module._ENGINE = None

                data_dir = db_module.data_dir()
                assert data_dir.exists()
                assert data_dir == profile_root
        finally:
            if profile_root.exists():
                import shutil
                shutil.rmtree(profile_root)

    def test_finops_db_path_takes_priority_over_profile(self, tmp_path):
        """FINOPS_DB_PATH overrides profile-based path selection."""
        custom_db = tmp_path / "custom.db"
        with patch.dict(
            os.environ,
            {"FINOPS_PROFILE": "someprofile", "FINOPS_DB_PATH": str(custom_db)},
            clear=False,
        ):
            os.environ.pop("DATABASE_URL", None)

            from src.finops.storage import db as db_module
            db_module._DATA_DIR = None
            db_module._ENGINE = None

            # get_engine uses FINOPS_DB_PATH before profile
            # We can verify the path logic by checking storage_mode
            mode = db_module.storage_mode()
            assert mode["mode"] == "sqlite"
            assert str(custom_db) in mode["path"]

        # Reset engine for other tests
        db_module._ENGINE = None


# ---------------------------------------------------------------------------
# Tests: security/vault.py keyring service prefix
# ---------------------------------------------------------------------------

class TestVaultKeyringPrefix:
    def test_default_keyring_service(self):
        """Without a profile, keyring service is the default 'finops-mcp'."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FINOPS_PROFILE", None)

            from src.finops.security import vault as vault_module
            service = vault_module._keyring_service()
            assert service == "finops-mcp"

    def test_profile_keyring_service_prefix(self):
        """With FINOPS_PROFILE=clientA, service becomes 'nable-clientA-mcp'."""
        with patch.dict(os.environ, {"FINOPS_PROFILE": "clientA"}, clear=False):
            from src.finops.security import vault as vault_module
            service = vault_module._keyring_service()
            assert service == "nable-clientA-mcp"

    def test_profile_keyring_prefix_different_names(self):
        """Different profile names produce different keyring service strings."""
        with patch.dict(os.environ, {"FINOPS_PROFILE": "acme-corp"}, clear=False):
            from src.finops.security import vault as vault_module
            service_a = vault_module._keyring_service()

        with patch.dict(os.environ, {"FINOPS_PROFILE": "beta-client"}, clear=False):
            service_b = vault_module._keyring_service()

        assert service_a != service_b
        assert "acme-corp" in service_a
        assert "beta-client" in service_b


# ---------------------------------------------------------------------------
# Tests: profile directory structure
# ---------------------------------------------------------------------------

class TestProfileDirectory:
    def test_profile_dir_path_structure(self, tmp_path):
        """Profile dirs live at ~/.finops/profiles/{name}/."""
        profile_name = "myprofile"
        expected = Path.home() / ".finops" / "profiles" / profile_name
        from src.finops.security.vault import _active_profile
        with patch.dict(os.environ, {"FINOPS_PROFILE": profile_name}):
            active = _active_profile()
        assert active == profile_name

    def test_empty_profile_env_returns_default(self):
        """Empty FINOPS_PROFILE is treated as no profile (default)."""
        with patch.dict(os.environ, {"FINOPS_PROFILE": ""}, clear=False):
            from src.finops.security.vault import _active_profile
            assert _active_profile() == ""

    def test_whitespace_profile_env_returns_default(self):
        """Whitespace-only FINOPS_PROFILE is treated as no profile."""
        with patch.dict(os.environ, {"FINOPS_PROFILE": "  "}, clear=False):
            from src.finops.security.vault import _active_profile
            assert _active_profile() == ""


# ---------------------------------------------------------------------------
# Tests: setup_wizard profile subcommand
# ---------------------------------------------------------------------------

class TestProfileCommand:
    def test_profile_create_makes_directory(self, tmp_path, capsys):
        """finops profile create <name> creates the profile directory."""
        profile_name = "wizard-test-profile"
        profiles_dir = Path.home() / ".finops" / "profiles" / profile_name

        if profiles_dir.exists():
            import shutil
            shutil.rmtree(profiles_dir)

        try:
            from src.finops.setup_wizard import _handle_profile_cmd
            parsed = type("P", (), {
                "profile_action": "create",
                "profile_name": profile_name,
            })()
            _handle_profile_cmd(parsed)
            assert profiles_dir.exists()

            captured = capsys.readouterr()
            assert "created" in captured.out.lower()
        finally:
            if profiles_dir.exists():
                import shutil
                shutil.rmtree(profiles_dir)

    def test_profile_create_duplicate_warns(self, tmp_path, capsys):
        """finops profile create warns if profile already exists."""
        profile_name = "already-exists"
        profiles_dir = Path.home() / ".finops" / "profiles" / profile_name
        profiles_dir.mkdir(parents=True, exist_ok=True)

        try:
            from src.finops.setup_wizard import _handle_profile_cmd
            parsed = type("P", (), {
                "profile_action": "create",
                "profile_name": profile_name,
            })()
            _handle_profile_cmd(parsed)
            captured = capsys.readouterr()
            assert "already exists" in captured.out.lower()
        finally:
            import shutil
            shutil.rmtree(profiles_dir)

    def test_profile_current_default(self, capsys):
        """finops profile current shows 'default' when FINOPS_PROFILE not set."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FINOPS_PROFILE", None)

            from src.finops.setup_wizard import _handle_profile_cmd
            parsed = type("P", (), {
                "profile_action": "current",
                "profile_name": "",
            })()
            _handle_profile_cmd(parsed)
            captured = capsys.readouterr()
            assert "default" in captured.out.lower()

    def test_profile_current_with_active(self, capsys):
        """finops profile current shows active profile name when set."""
        with patch.dict(os.environ, {"FINOPS_PROFILE": "myjob"}, clear=False):
            from src.finops.setup_wizard import _handle_profile_cmd
            parsed = type("P", (), {
                "profile_action": "current",
                "profile_name": "",
            })()
            _handle_profile_cmd(parsed)
            captured = capsys.readouterr()
            assert "myjob" in captured.out

    def test_profile_use_prints_export(self, capsys):
        """finops profile use <name> prints an export command."""
        from src.finops.setup_wizard import _handle_profile_cmd
        parsed = type("P", (), {
            "profile_action": "use",
            "profile_name": "production",
        })()
        _handle_profile_cmd(parsed)
        captured = capsys.readouterr()
        assert "FINOPS_PROFILE=production" in captured.out
        assert "export" in captured.out.lower()

    def test_profile_list_no_profiles_dir(self, capsys):
        """finops profile list guidance when profiles directory does not exist."""
        with tempfile.TemporaryDirectory() as td:
            # profiles_dir does not exist under this temp home
            fake_profiles_dir = Path(td) / "profiles"
            assert not fake_profiles_dir.exists()

            # Patch the function-level profiles_dir variable by patching Path.home
            import src.finops.setup_wizard as wizard_mod

            original_home = Path.home

            def fake_home():
                return Path(td)

            with patch.object(Path, "home", staticmethod(fake_home)):
                parsed = type("P", (), {
                    "profile_action": "list",
                    "profile_name": "",
                })()
                wizard_mod._handle_profile_cmd(parsed)

            captured = capsys.readouterr()
            # Should say no profiles or mention create command
            combined = captured.out.lower()
            assert "no profiles" in combined or "finops profile create" in combined
