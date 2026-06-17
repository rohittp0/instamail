import pytest

from instamail.instagram.cache import JsonCache
from instamail.instagram.harvester import ProfileNotFound
from instamail.instagram.resolver import (
    Resolver,
    extract_instagram_handle,
    username_permutations,
)


# --- pure helpers -----------------------------------------------------------

def test_extract_handle_skips_reserved_paths():
    assert extract_instagram_handle("see https://instagram.com/p/ABC123/ here") is None
    assert extract_instagram_handle("https://www.instagram.com/reel/XYZ/") is None
    assert extract_instagram_handle("profile: instagram.com/john.doe/") == "john.doe"


def test_extract_handle_none_when_absent():
    assert extract_instagram_handle("no socials here") is None


def test_username_permutations_from_dotted_local():
    perms = username_permutations("john.smith")
    assert "john.smith" in perms
    assert "johnsmith" in perms
    assert "john_smith" in perms
    assert all(p == p.lower() for p in perms)


# --- resolver orchestration -------------------------------------------------

class FakeHarvester:
    def __init__(self, profiles):  # {username: full_name or ProfileNotFound}
        self.profiles = profiles
        self.looked_up = []

    async def fetch_profile(self, username):
        self.looked_up.append(username)
        val = self.profiles.get(username)
        if val is None:
            raise ProfileNotFound(username)
        return {"username": username, "full_name": val}


async def _hit(_email):
    return "johnny"


async def _miss(_email):
    return None


async def test_breach_hit_short_circuits(tmp_path):
    h = FakeHarvester({})
    r = Resolver(harvester=h, intelx_api_key="key", breach_lookup=_hit, dork_lookup=_miss)
    res = await r.resolve("john.smith@x.com")
    assert res.username == "johnny"
    assert res.method == "breach"
    assert res.confidence == "high"
    assert h.looked_up == []  # permutation never ran


async def test_breach_skipped_without_api_key_falls_to_dork():
    h = FakeHarvester({})

    async def dork(_e):
        return "janedoe"

    called = []

    async def breach(_e):
        called.append(True)
        return "should_not_be_used"

    r = Resolver(harvester=h, intelx_api_key=None, breach_lookup=breach, dork_lookup=dork)
    res = await r.resolve("jane@x.com")
    assert res.username == "janedoe"
    assert res.method == "dork"
    assert called == []  # breach skipped entirely without a key


async def test_permutation_requires_name_match():
    # breach+dork miss; candidate "johnsmith" exists with a matching name
    h = FakeHarvester({"johnsmith": "John Smith", "john_smith": None, "john.smith": None})
    r = Resolver(harvester=h, breach_lookup=_miss, dork_lookup=_miss)
    res = await r.resolve("john.smith@x.com")
    assert res.username == "johnsmith"
    assert res.method == "permutation"
    assert res.confidence == "low"


async def test_permutation_rejects_name_mismatch():
    # candidate exists but the name is unrelated -> not accepted
    h = FakeHarvester({"johnsmith": "Totally Different", "john_smith": None, "john.smith": None})
    r = Resolver(harvester=h, breach_lookup=_miss, dork_lookup=_miss)
    assert await r.resolve("john.smith@x.com") is None


async def test_all_miss_returns_none():
    h = FakeHarvester({})  # every permutation -> ProfileNotFound
    r = Resolver(harvester=h, breach_lookup=_miss, dork_lookup=_miss)
    assert await r.resolve("nobody@x.com") is None


async def test_resolution_is_cached(tmp_path):
    cache = JsonCache(tmp_path, ttl=1000, now=lambda: 1.0)
    h = FakeHarvester({})
    calls = []

    async def breach(e):
        calls.append(e)
        return "johnny"

    r = Resolver(harvester=h, intelx_api_key="k", breach_lookup=breach, dork_lookup=_miss, cache=cache)
    first = await r.resolve("john@x.com")
    second = await r.resolve("john@x.com")
    assert first.username == second.username == "johnny"
    assert calls == ["john@x.com"]  # second call served from cache
