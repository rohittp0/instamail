"""instagram_stats status mapping + dedup (network-free)."""

import asyncio

from ig_profile import HarvestError, ProfileNotFound
from instagram_stats import _fetch_one, fetch_stats


class _FakeHarvester:
    def __init__(self, behavior):
        self._behavior = behavior   # username -> user dict | Exception instance

    async def fetch_profile(self, username):
        result = self._behavior[username]
        if isinstance(result, Exception):
            raise result
        return result


def _run_one(behavior, username):
    async def go():
        sem = asyncio.Semaphore(3)
        return await _fetch_one(_FakeHarvester(behavior), sem, username)
    return asyncio.run(go())


def test_status_ok(public_user):
    row = _run_one({"natgeo": public_user}, "natgeo")
    assert row["stats_status"] == "ok"
    assert row["followers"] == 1000


def test_status_private():
    private_user = {
        "username": "secret", "is_private": True,
        "edge_followed_by": {"count": 5}, "edge_follow": {"count": 3},
        "edge_owner_to_timeline_media": {"count": 0, "edges": []},
    }
    row = _run_one({"secret": private_user}, "secret")
    assert row["stats_status"] == "private"
    assert row["followers"] == 5          # counts still surfaced
    assert row["avg_views"] is None       # no posts -> no view metrics


def test_status_not_found():
    row = _run_one({"ghost": ProfileNotFound("ghost")}, "ghost")
    assert row["stats_status"] == "not_found"
    assert row["followers"] is None


def test_status_blocked():
    row = _run_one({"blk": HarvestError("429")}, "blk")
    assert row["stats_status"] == "blocked"


def test_status_error():
    row = _run_one({"boom": RuntimeError("kaboom")}, "boom")
    assert row["stats_status"] == "error"


# --- fetch_stats end-to-end with a fake session ----------------------------

class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.headers = {}

    def json(self):
        return self._payload


class _Session:
    def __init__(self, user):
        self._user = user
        self.calls = 0

    async def get(self, *a, **k):
        self.calls += 1
        return _Resp({"data": {"user": self._user}})


def test_fetch_stats_dedups_and_keys_by_username(public_user):
    sess = _Session(public_user)
    result = asyncio.run(fetch_stats(["natgeo", "@NatGeo", "natgeo"], session=sess, sessionid="x"))
    assert sess.calls == 1                 # three inputs collapse to one fetch
    assert set(result) == {"natgeo"}
    assert result["natgeo"]["stats_status"] == "ok"


def test_fetch_stats_empty():
    assert asyncio.run(fetch_stats([], session=object(), sessionid="x")) == {}
