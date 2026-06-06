"""Edge coverage for the detect-then-confirm AWS onboarding.

Guards the credential-detection and auto-naming logic that drives
`finops setup aws`. The interactive prompts are exercised manually; these tests
pin the pure logic that decides what to connect and what to call it.
"""
from finops import setup_wizard


class _FakeClient:
    def __init__(self, account=None, aliases=None, fail=False):
        self._account = account
        self._aliases = aliases or []
        self._fail = fail

    def get_caller_identity(self):
        if self._fail:
            raise RuntimeError("Unable to locate credentials")
        return {"Account": self._account}

    def list_account_aliases(self):
        return {"AccountAliases": self._aliases}


class _FakeSession:
    # Overridden per-test via a subclass.
    _profiles: list = []
    _map: dict = {}          # profile_name ("" = default chain) -> (account, aliases) | absent = fail
    _region = "us-east-1"

    def __init__(self, profile_name=None, region_name=None):
        self._profile = profile_name or ""
        self.region_name = region_name or type(self)._region

    @property
    def available_profiles(self):
        return list(type(self)._profiles)

    def client(self, name, region_name=None, **kwargs):
        spec = type(self)._map.get(self._profile)
        if spec is None:
            return _FakeClient(fail=True)
        account, aliases = spec
        return _FakeClient(account=account, aliases=aliases)


def _install(monkeypatch, profiles, mapping, region="us-east-1"):
    import boto3
    cls = type("FS", (_FakeSession,), {"_profiles": profiles, "_map": mapping, "_region": region})
    monkeypatch.setattr(boto3, "Session", cls)


def test_detect_finds_named_profiles(monkeypatch):
    _install(monkeypatch, ["default", "prod"], {
        "default": ("111111111111", ["acme-dev"]),
        "prod": ("222222222222", []),
    })
    candidates = setup_wizard._detect_aws_candidates()
    by_id = {c["account_id"]: c for c in candidates}
    assert set(by_id) == {"111111111111", "222222222222"}
    assert by_id["111111111111"]["alias"] == "acme-dev"
    assert by_id["111111111111"]["profile"] == "default"
    assert by_id["111111111111"]["label"] == "profile 'default'"


def test_detect_dedupes_same_account_across_profiles(monkeypatch):
    _install(monkeypatch, ["p1", "p2"], {
        "p1": ("333333333333", []),
        "p2": ("333333333333", []),
    })
    assert len(setup_wizard._detect_aws_candidates()) == 1


def test_detect_falls_back_to_default_chain(monkeypatch):
    _install(monkeypatch, [], {"": ("444444444444", [])})
    candidates = setup_wizard._detect_aws_candidates()
    assert len(candidates) == 1
    assert candidates[0]["label"] == "default credentials"
    assert candidates[0]["profile"] == ""


def test_detect_skips_default_chain_when_a_profile_works(monkeypatch):
    # A working profile means we do NOT also surface the (likely duplicate) chain.
    _install(monkeypatch, ["good"], {"good": ("555555555555", []), "": ("555555555555", [])})
    candidates = setup_wizard._detect_aws_candidates()
    assert len(candidates) == 1
    assert candidates[0]["profile"] == "good"


def test_detect_empty_when_nothing_authenticates(monkeypatch):
    _install(monkeypatch, ["bad"], {})  # profile fails, no default chain
    assert setup_wizard._detect_aws_candidates() == []


def test_auto_name_prefers_alias():
    assert setup_wizard._auto_aws_name({"alias": "acme-prod", "account_id": "1"}, set()) == "acme-prod"


def test_auto_name_falls_back_to_account_id():
    assert setup_wizard._auto_aws_name({"alias": "", "account_id": "999"}, set()) == "aws-999"


def test_auto_name_dedupes_against_existing():
    taken = {"acme", "acme-2"}
    assert setup_wizard._auto_aws_name({"alias": "acme", "account_id": "1"}, taken) == "acme-3"
