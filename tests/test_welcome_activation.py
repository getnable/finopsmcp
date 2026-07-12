"""
Activation onboarding tests.

PostHog showed the cliff: of people who install, ~84% never connect a provider
and never see a number, because the old `finops welcome` only printed a dollar
figure when the user handed over cloud credentials. If they skipped, they saw
nothing and left. These tests lock in the fix:

  1. The value moment tells the truth in demo mode (sample data, not "your account").
  2. A user who skips the credential step is never dead-ended and never shown fake
     numbers: they get an honest empty state plus the fastest real connect path.
  3. Ambient AWS credentials trigger a one-keystroke real scan, no menu.

Scans are stubbed so these stay fast and never touch a network or a real cloud.
"""
from __future__ import annotations

import os

import pytest

import finops.welcome as w


@pytest.fixture(autouse=True)
def _isolate_welcome_env(monkeypatch):
    """Keep these tests hermetic regardless of suite order.

    Two leak vectors: (1) the value moment sets FINOPS_DEMO on os.environ; (2)
    run_welcome_flow probes for an ambient model provider via the LLM env keys and
    the vault (_any_llm_configured / _llm_ambient_provider), so an earlier test that
    leaves an OpenAI/Anthropic credential in the vault would steer the flow down the
    LLM branch and break the AWS/demo-path assertions here. These tests cover the
    cloud/demo path only, so clear the demo + provider env keys and neutralize the
    LLM probe; tests that want the LLM branch can override.
    """
    for k in ("FINOPS_DEMO", "FINOPS_DEMO_MODE",
              "OPENAI_API_KEY", "OPENAI_ADMIN_KEY",
              "ANTHROPIC_API_KEY", "ANTHROPIC_ADMIN_KEY",
              "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)

    async def _no_ambient_llm():
        return False

    monkeypatch.setattr(w, "_any_llm_configured", _no_ambient_llm, raising=False)
    monkeypatch.setattr(w, "_llm_ambient_provider", lambda: None, raising=False)


@pytest.fixture()
def stub_scans(monkeypatch):
    """Stub the three server scans the value moment runs, so no network is hit."""
    import finops.server as server

    async def _summary():
        return {"total_usd": 12000.0,
                "by_service": {"Amazon EC2": 7000.0, "Amazon Bedrock": 3000.0, "Amazon S3": 2000.0}}

    async def _idle():
        return {"total_monthly_waste_usd": 400.0}

    async def _ai():
        return {"addressable_savings_monthly_usd": 950.0}

    monkeypatch.setattr(server, "get_cost_summary", _summary)
    monkeypatch.setattr(server, "list_idle_resources", _idle)
    monkeypatch.setattr(server, "optimize_ai_spend", _ai)


def test_value_moment_demo_is_truthful(stub_scans, capsys):
    ok = w._show_value_moment(demo=True)
    out = capsys.readouterr().out
    assert ok is True
    assert "sample data" in out
    assert "scanned your account" not in out  # must not claim it read a real account
    assert "$12,000" in out


def test_value_moment_returns_false_on_empty(monkeypatch):
    import finops.server as server

    async def _empty():
        return {"total_usd": 0.0, "by_service": {}}

    async def _none():
        return None

    monkeypatch.setattr(server, "get_cost_summary", _empty)
    monkeypatch.setattr(server, "list_idle_resources", _none)
    monkeypatch.setattr(server, "optimize_ai_spend", _none)
    assert w._show_value_moment(demo=True) is False


def test_skip_offers_real_connect_never_fake_numbers(monkeypatch, capsys):
    """Real data or nothing: declining the credential step must not dead-end, but
    it must never invent a sample number either. It offers the fastest real path."""
    monkeypatch.setattr("finops.setup_wizard._configure_claude_desktop", lambda *a, **k: None)

    async def _no_ambient(self):
        return False

    monkeypatch.setattr("finops.connectors.aws.AWSConnector.is_configured", _no_ambient)
    monkeypatch.setattr(w, "_llm_ambient_provider", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "5")  # explicitly skip the menu

    calls = []
    monkeypatch.setattr(w, "_show_value_moment", lambda demo=False: calls.append(demo) or (demo is True))

    w.run_welcome_flow(demo=False)
    out = capsys.readouterr().out

    assert calls == []  # no value moment at all: no real scan, and no fake demo bill
    assert "Here's nable on a sample bill" not in out  # the old auto-demo is gone
    assert "No numbers yet, on purpose." in out  # honest empty state
    assert "finops setup aws" in out  # clear real next step offered
    assert "welcome --demo" in out  # demo stays available as an explicit opt-in


def test_menu_default_enter_connects_not_skips(monkeypatch):
    """The no-ambient-creds menu now defaults to AWS connect on Enter, not skip, so
    the path of least resistance is to connect. Skip is a deliberate '5'."""
    monkeypatch.setattr("finops.setup_wizard._configure_claude_desktop", lambda *a, **k: None)

    async def _no_ambient(self):
        return False

    monkeypatch.setattr("finops.connectors.aws.AWSConnector.is_configured", _no_ambient)
    monkeypatch.setattr(w, "_llm_ambient_provider", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")  # Enter, take the default

    connected = []
    monkeypatch.setattr("finops.setup_wizard.setup_aws_account", lambda *a, **k: connected.append(True))
    monkeypatch.setattr(w, "_show_value_moment", lambda demo=False: False)

    w.run_welcome_flow(demo=False)
    assert connected == [True]  # Enter routed into AWS connect, not the skip nudge


def test_ambient_aws_offers_one_keystroke_real_scan(monkeypatch):
    """With ambient AWS creds, accept the prompt -> real scan, no menu, no demo."""
    monkeypatch.setattr("finops.setup_wizard._configure_claude_desktop", lambda *a, **k: None)

    async def _ambient(self):
        return True

    monkeypatch.setattr("finops.connectors.aws.AWSConnector.is_configured", _ambient)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")

    calls = []
    monkeypatch.setattr(w, "_show_value_moment", lambda demo=False: calls.append(demo) or True)

    w.run_welcome_flow(demo=False)

    assert calls == [False]  # one real scan, no menu, no demo fallback


def test_slow_ambient_probe_does_not_hang_onboarding(monkeypatch):
    """A hanging credential probe (firewalled IMDS / stale SSO) must time out and
    be treated as no ambient creds, not freeze the first run."""
    monkeypatch.setattr("finops.setup_wizard._configure_claude_desktop", lambda *a, **k: None)
    monkeypatch.setattr(w, "_AMBIENT_AWS_TIMEOUT", 0.2)

    import asyncio

    async def _hang(self):
        await asyncio.sleep(5)  # far longer than the timeout
        return True

    monkeypatch.setattr("finops.connectors.aws.AWSConnector.is_configured", _hang)
    monkeypatch.setattr(w, "_llm_ambient_provider", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "5")  # skip the menu

    calls = []
    monkeypatch.setattr(w, "_show_value_moment", lambda demo=False: calls.append(demo) or (demo is True))

    w.run_welcome_flow(demo=False)

    # The probe timed out: ambient was treated as absent, so onboarding fell through
    # to the connect nudge rather than hanging or invoking a real scan.
    assert calls == []  # no value moment ran; the timeout did not trigger a scan


def test_demo_env_restored_after_value_moment(monkeypatch):
    """A demo scan must not leak FINOPS_DEMO into the process env afterward."""
    import os as _os

    import finops.server as server

    async def _summary():
        return {"total_usd": 100.0, "by_service": {"S3": 100.0}}

    async def _none():
        return None

    monkeypatch.setattr(server, "get_cost_summary", _summary)
    monkeypatch.setattr(server, "list_idle_resources", _none)
    monkeypatch.setattr(server, "optimize_ai_spend", _none)
    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    monkeypatch.delenv("FINOPS_DEMO_MODE", raising=False)

    w._show_value_moment(demo=True)

    assert "FINOPS_DEMO" not in _os.environ
    assert "FINOPS_DEMO_MODE" not in _os.environ


def test_ambient_aws_declined_falls_through_to_menu_then_connect_nudge(monkeypatch, capsys):
    """Ambient creds present but user says no -> menu, skip, connect nudge (no fake bill)."""
    monkeypatch.setattr("finops.setup_wizard._configure_claude_desktop", lambda *a, **k: None)

    async def _ambient(self):
        return True

    monkeypatch.setattr("finops.connectors.aws.AWSConnector.is_configured", _ambient)
    monkeypatch.setattr(w, "_llm_ambient_provider", lambda: None)
    # decline Y/n, explicitly skip the menu, decline the inline sample tour
    answers = iter(["n", "5", "n"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))

    calls = []
    monkeypatch.setattr(w, "_show_value_moment", lambda demo=False: calls.append(demo) or (demo is True))

    w.run_welcome_flow(demo=False)
    out = capsys.readouterr().out

    assert calls == []  # declined real scan never ran, and no demo fallback either
    assert "No numbers yet, on purpose." in out  # honest empty state, real next step


def test_no_creds_close_offers_inline_sample_tour(monkeypatch, capsys):
    """Skipping everything offers the clearly-labeled sample tour inline; accepting
    runs it (opt-in, so 'real data or nothing' holds: sample never sets shown)."""
    monkeypatch.setattr("finops.setup_wizard._configure_claude_desktop", lambda *a, **k: None)

    async def _no_ambient(self):
        return False

    monkeypatch.setattr("finops.connectors.aws.AWSConnector.is_configured", _no_ambient)
    monkeypatch.setattr(w, "_llm_ambient_provider", lambda: None)
    # skip the menu, then accept the inline sample tour (Enter defaults to yes)
    answers = iter(["5", ""])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))

    calls = []
    monkeypatch.setattr(w, "_show_value_moment", lambda demo=False: calls.append(demo) or False)

    w.run_welcome_flow(demo=False)
    out = capsys.readouterr().out

    assert calls == [True]  # the sample tour ran: demo-labeled, opt-in
    assert "No numbers yet, on purpose." in out  # the honest close still printed first
