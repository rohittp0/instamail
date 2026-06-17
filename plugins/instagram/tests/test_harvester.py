import pytest

from instagram.cache import JsonCache
from instagram.harvester import (
    APP_ID,
    HarvestError,
    Harvester,
    ProfileNotFound,
)


class FakeResp:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def get(self, url, params=None, headers=None, cookies=None, **kw):
        self.calls.append({"url": url, "params": params, "headers": headers, "cookies": cookies})
        return self._responses.pop(0)


def _user_payload(username="bob"):
    return {"data": {"user": {"id": "42", "username": username, "edge_followed_by": {"count": 7}}}}


async def _noop_sleep(_seconds):
    return None


def _harvester(session, **kw):
    kw.setdefault("sleep", _noop_sleep)
    kw.setdefault("rate_limiter", None)
    return Harvester(session=session, **kw)


async def test_parses_user_and_sends_app_id_header():
    session = FakeSession([FakeResp(200, _user_payload())])
    h = _harvester(session)
    user = await h.fetch_profile("bob")
    assert user["id"] == "42"
    assert session.calls[0]["headers"]["x-ig-app-id"] == APP_ID
    assert session.calls[0]["params"] == {"username": "bob"}


async def test_404_raises_profile_not_found():
    h = _harvester(FakeSession([FakeResp(404)]))
    with pytest.raises(ProfileNotFound):
        await h.fetch_profile("ghost")


async def test_null_user_raises_profile_not_found():
    h = _harvester(FakeSession([FakeResp(200, {"data": {"user": None}})]))
    with pytest.raises(ProfileNotFound):
        await h.fetch_profile("ghost")


async def test_429_then_success_retries_with_backoff():
    slept = []

    async def rec_sleep(s):
        slept.append(s)

    session = FakeSession([FakeResp(429, headers={"Retry-After": "3"}), FakeResp(200, _user_payload())])
    h = _harvester(session, sleep=rec_sleep)
    user = await h.fetch_profile("bob")
    assert user["id"] == "42"
    assert slept == [3.0]  # honored Retry-After


async def test_persistent_429_raises_harvest_error():
    session = FakeSession([FakeResp(429) for _ in range(10)])
    h = _harvester(session, max_retries=3)
    with pytest.raises(HarvestError):
        await h.fetch_profile("bob")


async def test_sessionid_cookie_is_attached():
    session = FakeSession([FakeResp(200, _user_payload())])
    h = _harvester(session, sessionid="SECRET")
    await h.fetch_profile("bob")
    assert session.calls[0]["cookies"] == {"sessionid": "SECRET"}


async def test_cache_hit_skips_network(tmp_path):
    cache = JsonCache(tmp_path, ttl=1000, now=lambda: 1.0)
    cache.set("bob", _user_payload()["data"]["user"])
    session = FakeSession([])  # would IndexError if called
    h = _harvester(session, cache=cache)
    user = await h.fetch_profile("bob")
    assert user["id"] == "42"
    assert session.calls == []
