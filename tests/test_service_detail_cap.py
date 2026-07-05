"""The per-provider service detail is the token-bloat driver in multi-provider
cost answers. _cap_provider_service_detail keeps the top N services and rolls the
tail into scalars, so the response stays flat as providers scale, without changing
any total.
"""
from finops.server import _cap_provider_service_detail


def _provider(n_services, total=None):
    svc = {f"svc-{i:02d}": float(100 - i) for i in range(n_services)}
    return {"provider": "p", "total_usd": total if total is not None else sum(svc.values()),
            "by_service": dict(svc)}


def test_caps_to_top_n_and_rolls_tail():
    bp = {"aws": _provider(25)}
    _cap_provider_service_detail(bp, top_n=8)
    p = bp["aws"]
    assert len(p["by_service"]) == 8
    # top 8 are the largest (svc-00..svc-07 = 100..93)
    assert set(p["by_service"]) == {f"svc-{i:02d}" for i in range(8)}
    assert p["by_service_omitted"] == 17
    # rolled tail = sum of svc-08..svc-24 (92..76)
    assert round(p["by_service_others_usd"], 2) == round(sum(100 - i for i in range(8, 25)), 2)


def test_total_is_untouched():
    bp = {"aws": _provider(25, total=1234.56)}
    _cap_provider_service_detail(bp, top_n=8)
    assert bp["aws"]["total_usd"] == 1234.56
    # kept services + rolled tail reconstruct the full service sum
    kept = sum(bp["aws"]["by_service"].values())
    assert round(kept + bp["aws"]["by_service_others_usd"], 2) == round(sum(100 - i for i in range(25)), 2)


def test_small_provider_untouched():
    bp = {"vercel": _provider(3)}
    _cap_provider_service_detail(bp, top_n=8)
    assert len(bp["vercel"]["by_service"]) == 3
    assert "by_service_omitted" not in bp["vercel"]


def test_error_entries_are_skipped():
    bp = {"aws": _provider(25), "gcp": {"error": "throttled"}}
    _cap_provider_service_detail(bp, top_n=8)  # must not raise
    assert bp["gcp"] == {"error": "throttled"}
