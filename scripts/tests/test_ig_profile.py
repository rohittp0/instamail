"""project_profile + compute_metrics + Harvester (network-free, injected session)."""

import pytest

from ig_profile import (
    HarvestError,
    Harvester,
    ProfileNotFound,
    compute_metrics,
    project_profile,
)


def test_project_profile_raw_counts_and_flags(public_user):
    p = project_profile(public_user)
    assert p["username"] == "natgeo"
    assert p["full_name"] == "National Geographic"
    assert p["followers"] == 1000
    assert p["following"] == 50
    assert p["posts"] == 500
    assert p["is_verified"] is True
    assert p["is_business"] is True
    assert p["is_private"] is False
    assert p["external_url"] == "http://example.com/"


def test_project_profile_derived_metrics(public_user):
    p = project_profile(public_user)
    assert p["avg_views"] == 150.0           # mean(100, 200)
    assert p["max_views"] == 200
    assert p["avg_likes"] == 20              # mean(10, 20, 30)
    assert p["avg_comments"] == 4            # mean(2, 4, 6)
    assert p["engagement_rate"] == 2.4       # (20 + 4) / 1000 * 100
    assert p["posts_analyzed"] == 3
    assert p["reels_ratio"] == round(2 / 3, 3)
    assert p["top_hashtags"][0] == "travel"  # appears twice -> most common
    assert "nature" in p["top_hashtags"]
    assert isinstance(p["last_post_date"], str)


def test_compute_metrics_empty_profile():
    m = compute_metrics({})
    assert m["avg_views"] is None
    assert m["posts_analyzed"] == 0
    assert m["top_hashtags"] is None


# --- Harvester with an injected fake session (no network, no real sleeps) ---

class _Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Session:
    """Returns queued responses in order; the last one repeats."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def get(self, *a, **k):
        self.calls += 1
        idx = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[idx]


async def _noop_sleep(_):
    return None


def _harvester(session):
    return Harvester(session=session, sessionid=None, rate_limiter=None,
                     sleep=_noop_sleep, max_retries=3, base_backoff=0)


async def test_harvester_returns_user_on_200(public_user):
    sess = _Session([_Resp(200, {"data": {"user": public_user}})])
    user = await _harvester(sess).fetch_profile("natgeo")
    assert user["username"] == "natgeo"


async def test_harvester_404_raises_not_found():
    sess = _Session([_Resp(404)])
    with pytest.raises(ProfileNotFound):
        await _harvester(sess).fetch_profile("ghost")


async def test_harvester_persistent_401_raises_harvest_error():
    sess = _Session([_Resp(401)])
    with pytest.raises(HarvestError):
        await _harvester(sess).fetch_profile("blocked")
    assert sess.calls == 3   # exhausted all retries


async def test_harvester_recovers_after_one_401(public_user):
    sess = _Session([_Resp(401), _Resp(200, {"data": {"user": public_user}})])
    user = await _harvester(sess).fetch_profile("natgeo")
    assert user["username"] == "natgeo"


async def test_harvester_null_user_raises_not_found():
    sess = _Session([_Resp(200, {"data": {"user": None}})])
    with pytest.raises(ProfileNotFound):
        await _harvester(sess).fetch_profile("nobody")
