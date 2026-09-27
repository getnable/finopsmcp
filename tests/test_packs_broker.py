# SPDX-License-Identifier: Apache-2.0
"""The broker: code packs run out of process with only what they declare.

Every pack here is built at test time and signed with a key generated at test
time. Nothing reaches the network: the only sockets are listeners on
127.0.0.1 opened by the test itself.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import textwrap
import threading
import time
from datetime import date
from pathlib import Path

import pytest

from finops.packs import broker, sdk, store
from finops.packs import install as inst
from finops.packs.errors import BrokerError, PackError, PolicyRefusal
from finops.setup_wizard import main
from tests import packs_support
from tests.packs_support import EXAMPLE_CODE_PACK, copy_pack, new_key, sign_pack

packs_env = packs_support.packs_env

PROBE = '''\
import json
import os
import socket
import subprocess
import sys
import time


def env(ctx, payload):
    print("this goes to the log, not the protocol")
    print("the token is", os.environ.get("DECLARED_TOKEN"), file=sys.stderr)
    return {"env": dict(os.environ), "cwd": os.getcwd(),
            "declared": ctx.secret("DECLARED_TOKEN")}


def sleep(ctx, payload):
    time.sleep(60)
    return {}


def big(ctx, start, end):
    return [{"pad": "x" * 1000}] * 5000


def rows(ctx, start, end):
    ok = {"BilledCost": 10, "EffectiveCost": 9.5, "ListCost": 12, "ResourceId": "i-1",
          "ResourceName": None, "ResourceType": "Virtual Machine", "ServiceName": "Compute",
          "ServiceCategory": "Compute", "ProviderName": "Acme", "PublisherName": "Acme",
          "RegionId": "r1", "RegionName": None, "BillingPeriodStart": "2026-09-01",
          "BillingPeriodEnd": "2026-10-01", "ChargePeriodStart": start,
          "ChargePeriodEnd": end, "ChargeCategory": "Usage", "ChargeDescription": None,
          "CommitmentDiscountId": None, "CommitmentDiscountType": None,
          "Tags": {"team": "pay"}, "x_Extra": "ignored"}
    return [ok, dict(ok, ServiceName="Storage", ServiceCategory="Storage"),
            dict(ok, BilledCost="ten"), dict(ok, ChargeCategory="Refund"),
            dict(ok, ChargePeriodEnd="2020-01-01"), "not a row"]


def facts(ctx, context):
    return [{"fact": "owner", "subject": {"kind": "service", "id": "billing"},
             "value": {"team": "payments"}, "source": "catalog", "status": "confirmed",
             "confirmed_by": "mallory", "confidence": 0.9},
            {"fact": "environment", "subject": "aws_account:123456789012",
             "value": {"env": "prod"}},
            {"fact": "owner", "subject": {"kind": "service", "id": "x"}, "value": {}},
            {"fact": "nonsense"}]


def data(ctx, payload):
    out = {}
    for scope in payload["scopes"]:
        try:
            out[scope] = ctx._request("data.read", {"scope": scope,
                                                    "query": payload.get("query", {})})
        except Exception as e:
            out[scope] = f"{type(e).__name__}: {e}"
    try:
        ctx.read_data("org.teams")
    except Exception as e:
        out["client_side"] = f"{type(e).__name__}: {e}"
    return out


def net(ctx, payload):
    out = {}
    for target in payload["targets"]:
        host, port = target.rsplit(":", 1)
        try:
            with socket.create_connection((host, int(port)), timeout=5) as s:
                s.sendall(b"hi")
            out[target] = "connected"
        except Exception as e:
            out[target] = f"{type(e).__name__}: {e}"
    try:
        subprocess.run(["curl", "http://127.0.0.1/"], check=False)
        out["spawn"] = "ran"
    except Exception as e:
        out["spawn"] = f"{type(e).__name__}: {e}"
    if os.path.exists("/proc/self/ns/net"):
        out["netns"] = os.readlink("/proc/self/ns/net")
    return out


def deliver(ctx, payload):
    return {"id": "T-1", "kind": payload["kind"]}


def flood(ctx, start, end):
    # review2 hang.py: requests the core answers, with the answers never read
    # back, so the core's writes to this process's stdin block once the pipe
    # fills; then sleep far past the call's timeout.
    chan = ctx._request.__self__          # the host's Channel
    for i in range(5000):
        chan.send({"jsonrpc": "2.0", "id": f"x{i}", "method": "data.read",
                   "params": {"scope": "nope", "query": {"pad": "x" * 2000}}})
    time.sleep(3600)
    return []


def stall(ctx, start, end):
    # Stops reading before the core answers anything, and sleeps. Each
    # refusal quotes the scope back, so the answers overfill the pipe.
    chan = ctx._request.__self__
    for i in range(40):
        chan.send({"jsonrpc": "2.0", "id": f"y{i}", "method": "data.read",
                   "params": {"scope": "s" * 200000, "query": {}}})
    time.sleep(3600)
    return []


def deep(ctx, start, end):
    chan = ctx._request.__self__
    chan._out.write("[" * 200000 + "]" * 200000 + "\\n")
    chan._out.flush()
    time.sleep(3600)
    return []
'''

SINK_CAPS = 'act = ["ticket"]\nmax_autonomy = "L2"\n'


def build_pack(root: Path, *, name: str = "probe", caps: str = SINK_CAPS,
               provides: str | None = None) -> Path:
    provides = provides if provides is not None else textwrap.dedent('''\
        sinks = [{id = "env", entry = "probe_code:env"}, {id = "sleep", entry = "probe_code:sleep"},
                 {id = "data", entry = "probe_code:data"}, {id = "net", entry = "probe_code:net"},
                 {id = "deliver", entry = "probe_code:deliver"}]
        connectors = [{id = "rows", entry = "probe_code:rows", output = "focus-1.3"},
                      {id = "big", entry = "probe_code:big", output = "focus-1.3"},
                      {id = "flood", entry = "probe_code:flood", output = "focus-1.3"},
                      {id = "stall", entry = "probe_code:stall", output = "focus-1.3"},
                      {id = "deep", entry = "probe_code:deep", output = "focus-1.3"}]
        adapters = [{id = "facts", entry = "probe_code:facts"}]
        ''')
    (root / "probe_code").mkdir(parents=True)
    (root / "probe_code" / "__init__.py").write_text(PROBE)
    (root / "nable-pack.toml").write_text(
        f'[pack]\nname = "{name}"\nnamespace = "io.github.example"\nversion = "1.0.0"\n'
        'description = "A probe"\nnable_api = ">=1.0,<2.0"\nmaintainers = ["@t"]\n'
        f'support = "community"\n\n[capabilities]\n{caps}\n[provides]\n{provides}')
    return root


@pytest.fixture
def code_env(packs_env, tmp_path, monkeypatch):
    """A trusted org key, the vault out of the way, and a helper that builds,
    signs and installs a probe pack."""
    monkeypatch.setattr(broker, "_vault_get", lambda name: None)
    key = new_key(tmp_path, "acme")
    base = "  trusted_keys:\n" + key.trusted

    def policy(extra: str = "") -> None:
        packs_env.policy("packs:\n" + base + extra)

    def install(name: str = "probe", *, sign: bool = True, **kw) -> str:
        src = build_pack(tmp_path / f"src-{name}", name=name, **kw)
        if sign:
            sign_pack(src, key)
        inst.install(str(src), yes=True)
        return f"io.github.example/{name}"

    policy()
    packs_env.key, packs_env.install, packs_env.trust = key, install, policy
    return packs_env


def _deliver(pid: str, entry: str, **kw):
    payload = {"kind": "ticket", **kw.pop("payload", {})}
    return broker.deliver(pid, entry, payload, **kw)


# ── environment and secrets ──────────────────────────────────────────────────

def test_the_pack_sees_a_scrubbed_environment_and_only_its_declared_secret(
        code_env, monkeypatch):
    monkeypatch.setenv("FINOPS_LICENSE_KEY", "finops-secret")
    monkeypatch.setenv("NABLE_PACK_KEY_PASSPHRASE", "nable-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("UNDECLARED_TOKEN", "undeclared")
    monkeypatch.setenv("DECLARED_TOKEN", "from-the-environment")
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
    monkeypatch.setattr(broker, "_vault_get", lambda key: "declared-value-123"
                        if key == "pack:io.github.example/probe:DECLARED_TOKEN" else None)
    pid = code_env.install(caps=SINK_CAPS + 'secrets = ["DECLARED_TOKEN"]\n')
    r = _deliver(pid, "env")
    env = r.output["env"]
    assert env["DECLARED_TOKEN"] == "declared-value-123"
    assert r.output["declared"] == "declared-value-123"
    assert set(env) <= {"PATH", "HOME", "LANG", "LC_CTYPE", "DECLARED_TOKEN"}, sorted(env)
    assert not [k for k in env if k.startswith(("FINOPS_", "NABLE_", "AWS_", "PYTHON"))]
    assert "UNDECLARED_TOKEN" not in env
    assert env["HOME"] != str(Path.home()) and "nable-pack-" in env["HOME"]
    assert r.output["cwd"] == env["HOME"]
    assert not Path(env["HOME"]).exists()                 # thrown away afterwards
    log = Path(r.log).read_text()
    assert "this goes to the log, not the protocol" in log
    assert "declared-value-123" not in log and "[redacted DECLARED_TOKEN]" in log


def test_a_secret_comes_only_from_the_packs_own_vault_entry(code_env, monkeypatch):
    # review2 sec.py: a declared secret used to fall back to nable's shared
    # vault keys and its own environment, which is where cloud keys live.
    vault = {"DECLARED_TOKEN": "nables-own-key",
             "pack:io.github.example/probe:DECLARED_TOKEN": "the-packs-own"}
    monkeypatch.setattr(broker, "_vault_get", vault.get)
    monkeypatch.setenv("DECLARED_TOKEN", "from-env")
    pid = code_env.install(caps=SINK_CAPS + 'secrets = ["DECLARED_TOKEN"]\n')
    assert _deliver(pid, "env").output["declared"] == "the-packs-own"
    del vault["pack:io.github.example/probe:DECLARED_TOKEN"]
    out = _deliver(pid, "env").output
    assert out["declared"] is None and "DECLARED_TOKEN" not in out["env"]


def test_cloud_credential_names_are_refused_for_packs_that_are_not_first_party(monkeypatch):
    from finops.packs import capabilities as caps
    names = ["AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN", "AWS_PROFILE",
             "GOOGLE_APPLICATION_CREDENTIALS", "AZURE_CLIENT_SECRET", "ARM_CLIENT_SECRET",
             "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE", "KUBECONFIG", "R2_SECRET_ACCESS_KEY",
             "STS_SESSION_TOKEN"]
    _, probs = caps.validate({"secrets": names}, first_party=False)
    assert len(probs) == len(names)
    assert all("cloud credential" in p.reason for p in probs)
    _, probs = caps.validate({"secrets": names}, first_party=True)
    assert probs == []
    _, probs = caps.validate({"secrets": ["EXAMPLE_API_TOKEN"]}, first_party=False)
    assert probs == []
    # and the environment never supplies one, even to a pack that declares it
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-a-real-key")
    monkeypatch.setattr(broker, "_vault_get", lambda key: None)
    caps_fp, _ = caps.validate({"secrets": ["AWS_SECRET_ACCESS_KEY"]}, first_party=True)
    prep = broker.Prepared("io.github.getnable/x", "io.github.getnable", "x", "1.0.0",
                           "connectors", "c", "m:f", Path("/nonexistent"), caps_fp,
                           broker.signing.UNSIGNED, False, "first-party")
    env, secrets = broker.child_env(prep, "/tmp/h")
    assert "AWS_SECRET_ACCESS_KEY" not in env and secrets == {}


def test_nable_pack_secret_set_stores_under_the_packs_namespace(code_env, monkeypatch,
                                                                capsys):
    import io

    from finops.security import vault as vault_mod
    stored: dict[str, str] = {}

    class FakeVault:
        def store(self, k, v):
            stored[k] = v

        def delete(self, k):
            return stored.pop(k, None) is not None

    monkeypatch.setattr(vault_mod.Vault, "default", classmethod(lambda cls: FakeVault()))
    (code_env.tmp / "vault").mkdir()
    (code_env.tmp / "vault" / "vault.db").write_bytes(b"")
    monkeypatch.setattr(vault_mod, "_vault_dir", lambda: code_env.tmp / "vault")
    monkeypatch.setattr(broker, "_vault_get", stored.get)
    pid = code_env.install(caps=SINK_CAPS + 'secrets = ["DECLARED_TOKEN"]\n')
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret-value\n"))
    with pytest.raises(SystemExit) as ei:
        main(["pack", "secret", "set", pid, "DECLARED_TOKEN"])
    out = capsys.readouterr()
    assert ei.value.code == 0 and "s3cret-value" not in out.out + out.err
    assert stored == {f"pack:{pid}:DECLARED_TOKEN": "s3cret-value"}
    assert _deliver(pid, "env").output["declared"] == "s3cret-value"
    # the value never comes from argv, and a cloud credential name is refused
    monkeypatch.setattr("sys.stdin", io.StringIO("AKIA..."))
    with pytest.raises(SystemExit) as ei:
        main(["pack", "secret", "set", pid, "AWS_ACCESS_KEY_ID"])
    assert ei.value.code == 1 and "cloud credential" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["pack", "secret", "set", pid, "DECLARED_TOKEN", "inline-value"])
    capsys.readouterr()
    with pytest.raises(SystemExit) as ei:
        main(["pack", "secret", "remove", pid, "DECLARED_TOKEN"])
    assert ei.value.code == 0 and stored == {}


def test_an_undeclared_secret_is_refused_inside_the_pack_too():
    ctx = sdk.Context.for_testing(capabilities={"secrets": ["A_TOKEN"]},
                                  secrets={"A_TOKEN": "a"})
    assert ctx.secret("A_TOKEN") == "a"
    with pytest.raises(PermissionError):
        ctx.secret("B_TOKEN")


# ── limits ───────────────────────────────────────────────────────────────────

def test_a_call_past_its_timeout_is_stopped(code_env):
    pid = code_env.install()
    t0 = time.monotonic()
    with pytest.raises(BrokerError) as ei:
        _deliver(pid, "sleep", timeout=1.5)
    assert "took longer than 1.5s" in str(ei.value)
    assert time.monotonic() - t0 < 15
    assert "FAILED" in broker.log_path(broker.prepare(pid, "sleep")).read_text()


def test_a_pack_that_floods_the_core_with_requests_is_stopped(code_env):
    # review2 hang.py: 5000 requests whose answers the pack never reads. The
    # core's writes used to block on the full pipe, past any timeout.
    pid = code_env.install()
    t0 = time.monotonic()
    with pytest.raises(BrokerError) as ei:
        broker.fetch_costs(pid, "flood", "2026-09-01", "2026-09-02", timeout=3)
    assert time.monotonic() - t0 < 15
    assert f"more than {broker.MAX_INFLIGHT} requests of the core in flight" in str(ei.value)


def test_a_pack_that_stops_reading_cannot_outlast_its_timeout(code_env):
    # Too few requests for the in-flight cap, but the answers overfill the
    # pipe: the write is bound by the call's deadline and the process killed.
    pid = code_env.install()
    t0 = time.monotonic()
    with pytest.raises(BrokerError) as ei:
        broker.fetch_costs(pid, "stall", "2026-09-01", "2026-09-02", timeout=2)
    assert time.monotonic() - t0 < 15
    assert "took longer than 2s" in str(ei.value) or "stopped reading" in str(ei.value)


def test_a_line_nested_too_deeply_is_a_protocol_error(code_env):
    pid = code_env.install()
    with pytest.raises(BrokerError, match="broke the protocol"):
        broker.fetch_costs(pid, "deep", "2026-09-01", "2026-09-02", timeout=20)


def test_output_past_the_cap_is_stopped(code_env):
    pid = code_env.install()
    with pytest.raises(BrokerError) as ei:
        broker.fetch_costs(pid, "big", "2026-09-01", "2026-09-02", max_output=50_000)
    assert "more than 50000 bytes" in str(ei.value)
    assert "FAILED" in broker.log_path(broker.prepare(pid, "big")).read_text()


# ── what comes back ──────────────────────────────────────────────────────────

def test_invalid_focus_rows_are_dropped_and_reported(code_env):
    pid = code_env.install()
    r = broker.fetch_costs(pid, "rows", "2026-09-01", "2026-09-02")
    assert [row["ServiceName"] for row in r.output] == ["Compute", "Storage"]
    assert r.output[0]["ChargePeriodStart"] == "2026-09-01T00:00:00"
    assert "x_Extra" not in r.output[0] and r.output[0]["Tags"] == {"team": "pay"}
    assert r.dropped == 4
    text = " | ".join(r.problems)
    assert "BilledCost must be a finite number" in text
    assert "ChargeCategory 'Refund'" in text
    assert "ChargePeriodEnd is before ChargePeriodStart" in text
    assert "a row must be an object" in text


def test_adapter_facts_are_forced_to_proposals_with_the_pack_as_source(code_env):
    pid = code_env.install(caps='write_org = ["proposals"]\n')
    r = broker.propose_facts(pid, "facts")
    assert [f.status for f in r.output] == ["proposed", "proposed"]
    assert all(f.confirmed_by is None for f in r.output)
    assert r.output[0].source == f"pack:{pid}:catalog"
    assert r.output[1].source == f"pack:{pid}"
    assert r.dropped == 2
    assert any("a pack can only propose" in p for p in r.problems)


def test_an_adapter_without_write_org_proposals_does_not_run(code_env):
    pid = code_env.install(caps="")
    with pytest.raises(PolicyRefusal, match="write_org"):
        broker.propose_facts(pid, "facts")


# ── data scopes ──────────────────────────────────────────────────────────────

@pytest.fixture
def cost_db(tmp_path, monkeypatch):
    from finops.storage import db as db_mod
    prev_engine, prev_dir = db_mod._ENGINE, db_mod._DATA_DIR
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "finops.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    db_mod._ENGINE, db_mod._DATA_DIR = None, None
    from finops.storage.snapshots import store_snapshot
    store_snapshot("aws", "Amazon EC2", "111122223333", "us-east-1", date(2026, 9, 2), 42.5)
    store_snapshot("aws", "Amazon S3", "111122223333", "us-east-1", date(2026, 8, 1), 1.0)
    yield
    if db_mod._ENGINE is not None and db_mod._ENGINE is not prev_engine:
        db_mod._ENGINE.dispose()
    db_mod._ENGINE, db_mod._DATA_DIR = prev_engine, prev_dir


def test_data_read_answers_only_declared_scopes(code_env, cost_db, tmp_path, monkeypatch):
    from finops import org
    monkeypatch.setenv("FINOPS_ORG_DIR", str(tmp_path / "org"))
    org.propose(org.make_fact("owner", "aws_account:111122223333", {"team": "platform"},
                              source="test"))
    pid = code_env.install(caps=SINK_CAPS + 'read_data = ["focus.cost", "org.owners", '
                                            '"org.environments", "budgets"]\n')
    r = _deliver(pid, "data", payload={
        "scopes": ["focus.cost", "org.owners", "org.environments", "budgets", "org.teams",
                   "ledger.guard"],
        "query": {"start": "2026-09-01", "end": "2026-10-01"}})
    out = r.output
    assert [row["ServiceName"] for row in out["focus.cost"]["rows"]] == ["Amazon EC2"]
    assert out["focus.cost"]["rows"][0]["BilledCost"] == 42.5
    assert out["org.owners"]["owners"][0]["value"] == {"team": "platform"}
    assert out["org.owners"]["owners"][0]["confirmed"] is False
    assert out["org.environments"] == {"environments": []}
    assert "does not serve it to packs yet" in out["budgets"]
    assert "is not in io.github.example/probe's declared read_data" in out["org.teams"]
    assert "not in io.github.example/probe's declared read_data" in out["ledger.guard"]
    assert "DataError" in out["client_side"]


# ── network ──────────────────────────────────────────────────────────────────

class _Listener:
    """A TCP listener on 127.0.0.1 that accepts and closes connections."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                continue
            self.accepted += 1
            c.close()

    def close(self):
        self._stop = True
        self._t.join(timeout=2)
        self.sock.close()


@pytest.fixture
def listeners():
    a, b = _Listener(), _Listener()
    yield a, b
    a.close()
    b.close()


def test_the_audit_hook_allows_declared_hosts_and_refuses_the_rest(code_env, listeners):
    declared, undeclared = listeners
    pid = code_env.install(caps=SINK_CAPS + f'network = ["127.0.0.1:{declared.port}"]\n')
    r = _deliver(pid, "net", payload={"targets": [
        f"127.0.0.1:{declared.port}", f"127.0.0.1:{undeclared.port}",
        "unreachable.invalid:443"]})
    out = r.output
    assert out[f"127.0.0.1:{declared.port}"] == "connected"
    assert "PermissionError" in out[f"127.0.0.1:{undeclared.port}"]
    # The lookup itself is refused, so no name leaves the machine.
    assert "PermissionError" in out["unreachable.invalid:443"]
    assert "PermissionError" in out["spawn"]
    deadline = time.monotonic() + 5
    while declared.accepted < 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert declared.accepted == 1 and undeclared.accepted == 0
    assert r.network["mode"] == "audit"
    assert r.network["declared"] == [f"127.0.0.1:{declared.port}"]
    seen = {(o["host"], o["port"], o["allowed"]) for o in r.network["observed"]}
    assert ("127.0.0.1", declared.port, True) in seen
    assert ("127.0.0.1", undeclared.port, False) in seen
    assert "REFUSED" in Path(r.log).read_text()


@pytest.mark.parametrize("mode", ["off", "auto"])
def test_a_pack_that_declares_no_network_reaches_nothing(code_env, listeners, monkeypatch,
                                                         mode):
    target, _ = listeners
    monkeypatch.setattr(broker, "NETNS", mode)
    monkeypatch.setattr(broker, "_NETNS_OK", {})
    if mode == "auto" and not broker.netns_available():
        pytest.skip("network namespaces are not available here")
    pid = code_env.install()
    r = _deliver(pid, "net", payload={"targets": [f"127.0.0.1:{target.port}"]})
    assert r.network["mode"] == ("namespace" if mode == "auto" else "audit")
    assert r.output[f"127.0.0.1:{target.port}"] != "connected"
    assert target.accepted == 0
    if mode == "auto":   # really another network namespace, not only the audit hook
        assert r.output["netns"] != os.readlink("/proc/self/ns/net")


# ── what may run ─────────────────────────────────────────────────────────────

def _digest(pid: str) -> str:
    return store.content_digest(store.read_index()["packs"][pid]["files"])


def test_unsigned_code_does_not_run_unless_the_org_allowlists_it(code_env):
    pid = code_env.install(sign=False)
    with pytest.raises(PolicyRefusal) as ei:
        _deliver(pid, "deliver")
    msg = str(ei.value)
    digest = _digest(pid)
    assert "Unsigned code does not run" in msg
    assert f"allow_unsigned_code: [{pid}@{digest}]" in msg
    code_env.trust(f"  allow_unsigned_code: [{pid}@{digest}]\n")
    assert _deliver(pid, "deliver").output == {"id": "T-1", "kind": "ticket"}


def test_a_bare_id_allowlists_nothing_without_allowed_sources(code_env, tmp_path):
    # review2: the allowlist was keyed on a namespace nothing verifies for an
    # unsigned pack, so any pack claiming the id ran.
    pid = code_env.install(sign=False)
    code_env.trust(f"  allow_unsigned_code: [{pid}]\n")
    with pytest.raises(PolicyRefusal, match="honoured only when packs.allowed_sources"):
        _deliver(pid, "deliver")
    # a digest pins files: other files under the same id do not run
    code_env.trust(f"  allow_unsigned_code: [{pid}@{'0' * 64}]\n")
    with pytest.raises(PolicyRefusal, match="Unsigned code does not run"):
        _deliver(pid, "deliver")
    # with allowed_sources pinning where packs come from, the bare id holds
    src = str(tmp_path / "src-probe")
    code_env.trust(f"  allow_unsigned_code: [{pid}]\n  allowed_sources: ['{src}']\n")
    assert _deliver(pid, "deliver").output["kind"] == "ticket"


def test_code_signed_by_a_key_nobody_trusts_does_not_run(code_env, tmp_path):
    pid = code_env.install()
    code_env.policy("packs:\n  trusted_keys: []\n")
    with pytest.raises(PolicyRefusal, match="signature no longer holds"):
        _deliver(pid, "deliver")
    # signed by a key nobody here trusted to begin with
    other = new_key(tmp_path, "stranger")
    src = build_pack(tmp_path / "src-stranger", name="stranger")
    sign_pack(src, other)
    code_env.trust()
    inst.install(str(src), yes=True)
    with pytest.raises(PolicyRefusal, match="not signed by a key this org trusts"):
        _deliver("io.github.example/stranger", "deliver")


def test_code_edited_after_install_does_not_run(code_env):
    pid = code_env.install()
    root = store.install_dir("io.github.example", "probe", "1.0.0")
    p = root / "probe_code" / "__init__.py"
    p.write_text(p.read_text() + "\nBACKDOOR = 1\n")
    with pytest.raises(PolicyRefusal, match="files changed since it was approved"):
        _deliver(pid, "deliver")


def test_code_outside_the_pack_is_not_covered_by_its_signature(code_env):
    pid = code_env.install(provides='sinks = [{id = "stdlib", entry = "json:dumps"}]\n')
    with pytest.raises(BrokerError, match="resolves outside the pack"):
        _deliver(pid, "stdlib")


def test_a_sink_delivers_only_its_declared_act_kinds(code_env):
    pid = code_env.install()
    assert _deliver(pid, "deliver").output["kind"] == "ticket"
    with pytest.raises(PolicyRefusal, match="cannot deliver a 'pr'"):
        broker.deliver(pid, "deliver", {"kind": "pr"})
    with pytest.raises(PolicyRefusal, match="above the pack's max_autonomy"):
        broker.deliver(pid, "deliver", {"kind": "ticket", "autonomy": "L3"})
    with pytest.raises(PackError, match="has no sink 'rows'"):
        broker.deliver(pid, "rows", {"kind": "ticket"})


# ── the example pack, end to end ─────────────────────────────────────────────

@pytest.fixture
def example_pack(code_env, tmp_path, monkeypatch):
    src = copy_pack(EXAMPLE_CODE_PACK, tmp_path / "example")
    sign_pack(src, code_env.key)
    inst.install(str(src), yes=True)
    pid = "com.example/example-csv-connector"
    # As `nable pack secret set` stores them: in the pack's own vault entries.
    vault = {broker.secret_key(pid, "EXAMPLE_COSTS_CSV"):
             str(EXAMPLE_CODE_PACK / "samples" / "costs.csv"),
             broker.secret_key(pid, "EXAMPLE_OWNERS_CSV"):
             str(EXAMPLE_CODE_PACK / "samples" / "owners.csv")}
    monkeypatch.setattr(broker, "_vault_get", vault.get)
    monkeypatch.setenv("FINOPS_ORG_DIR", str(tmp_path / "org"))
    return pid


def test_the_example_connector_runs_from_the_cli(example_pack, capsys):
    with pytest.raises(SystemExit) as ei:
        main(["pack", "run", example_pack, "csv-costs", "--start", "2026-09-01",
              "--end", "2026-09-03", "--json"])
    body = json.loads(capsys.readouterr().out)
    assert ei.value.code == 0
    assert body["summary"]["rows"] == 4
    assert body["summary"]["billed_total"] == pytest.approx(412.5 + 96 + 398.25 + 96)
    assert body["dropped"] == 0 and body["loaded_from"] == "pack"
    assert "seatbelt" in body["sandbox"]
    with pytest.raises(SystemExit):
        main(["pack", "run", example_pack, "csv-costs", "--start", "2026-09-01",
              "--end", "2026-10-01"])
    out = capsys.readouterr().out
    assert "5 FOCUS rows" in out and "Snowflake Warehouse" in out and "log:" in out


def test_the_example_adapter_feeds_org_init_as_proposals(example_pack, monkeypatch):
    from finops import org
    from finops.org import store as org_store
    org.propose(org.make_fact("owner", "aws_account:123456789012", {"team": "payments"},
                              source="someone"))
    adapters = broker.org_adapters()
    assert [a.name for a in adapters] == [f"pack:{example_pack}/csv-owners"]
    # org init runs installed pack adapters after the built-in ones; only the
    # pack's are wanted here.
    monkeypatch.setattr(org_store, "ADAPTERS", [])
    runs = org_store.run_adapters()
    assert [r.id for r in runs] == [f"pack:{example_pack}/csv-owners"]
    assert runs[0].error is None and runs[0].results.count("added") == 2
    props = [f for f in org.load().proposals("owner") if f.source.startswith("pack:")]
    assert sorted(str(f.subject) for f in props) == ["repo_path:infra/analytics",
                                                     "service:snowflake"]
    assert all(f.source.startswith(f"pack:{example_pack}:owners.csv:") for f in props)
    assert all(not f.confirmed for f in props)


def test_org_adapters_skips_packs_that_may_not_run(code_env):
    code_env.install(sign=False, caps='write_org = ["proposals"]\n')
    assert broker.org_adapters() == []
    pid = "io.github.example/probe"
    code_env.trust(f"  allow_unsigned_code: [{pid}@{_digest(pid)}]\n")
    assert [a.pack_id for a in broker.org_adapters()] == ["io.github.example/probe"]


def test_a_pack_tests_its_own_entry_points_without_the_broker(monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)   # keep the example tree clean
    monkeypatch.syspath_prepend(str(EXAMPLE_CODE_PACK))
    try:
        from example_csv_connector import owners
        ctx = sdk.Context.for_testing(
            kind="adapters", capabilities={"secrets": ["EXAMPLE_OWNERS_CSV"],
                                           "read_data": ["org.owners"]},
            secrets={"EXAMPLE_OWNERS_CSV": str(EXAMPLE_CODE_PACK / "samples" / "owners.csv")},
            data={"org.owners": {"owners": [{"subject": "service:snowflake"}]}})
        facts = owners.propose(ctx, {})
        assert [f["subject"]["id"] for f in facts] == ["123456789012", "infra/analytics"]
    finally:
        for mod in [m for m in sys.modules if m.startswith("example_csv_connector")]:
            sys.modules.pop(mod, None)


def test_the_host_runs_under_the_core_interpreter_isolated():
    cmd = broker._command(False)
    assert cmd[:4] == [sys.executable, "-I", "-B", "-c"]
    import finops
    assert cmd[5] == str(Path(finops.__file__).resolve().parent.parent)
