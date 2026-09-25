"""recommend_spot_adoption says when CPU history could not be read, instead of
reporting that no candidates exist."""
import asyncio
import inspect
from unittest.mock import patch

from finops.recommendations.spot_adoption import SpotResults


def _call():
    from finops import server
    fn = getattr(server.recommend_spot_adoption, "fn", server.recommend_spot_adoption)
    out = fn(regions=["us-east-1"])
    if inspect.iscoroutine(out):
        out = asyncio.run(out)
    return out


def test_all_unread_is_not_reported_as_no_candidates():
    empty = SpotResults()
    empty.cpu_unread_instances = ["i-aaa", "i-bbb"]
    with patch("finops.recommendations.spot_adoption.recommend_spot_adoption", return_value=empty):
        out = _call()
    assert "could not be read for 2 instance(s)" in out
    assert "already on spot" not in out
