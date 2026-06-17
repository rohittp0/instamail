import pytest

from instagram.cache import JsonCache
from instagram.harvester import HarvestError, ProfileNotFound
from instagram.resolver import (
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


def test_username_permutations_strip_trailing_digits_and_plus_tag():
    # the case the old resolver could never reach: 'tprohit9' -> handle 'tprohit'
    assert "tprohit" in username_permutations("tprohit9")
    assert "tprohit9" in username_permutations("tprohit9")
    # +tag sub-addressing is dropped
    assert "john" in username_permutations("john+osint")


# --- fakes ------------------------------------------------------------------

class FakeHarvester:
    def __init__(self, profiles):  # {username: full_name or None(=ProfileNotFound)}
        self.profiles = profiles
        self.looked_up = []

    async def fetch_profile(self, username):
        self.looked_up.append(username)
        val = self.profiles.get(username)
        if val is None:
            raise ProfileNotFound(username)
        return {"username": username, "full_name": val}


class FakeLookup:
    def __init__(self, obfuscations=None, boom=False):  # {username: {"obfuscated_email":...}}
        self.obfuscations = obfuscations or {}
        self.boom = boom
        self.queried = []

    async def obfuscation_for(self, q):
        self.queried.append(q)
        if self.boom:
            raise RuntimeError("throttled")
        return self.obfuscations.get(q)


class FakeContacts:
    def __init__(self, matches=None):
        self.matches = matches or {}

    async def find_by_email(self, email):
        return self.matches.get(email, [])


async def _miss(_email):
    return None


async def _hit(handle):
    async def _dork(_email):
        return handle
    return _dork


# --- tier 1: contact-import -------------------------------------------------

async def test_contact_import_short_circuits_high():
    h = FakeHarvester({})
    contacts = FakeContacts({"e@x.com": [{"username": "Random_Handle", "full_name": "R", "pk": 1}]})
    lk = FakeLookup({"random_handle": {"obfuscated_email": "x", "obfuscated_phone": "+1 **9"}})
    r = Resolver(harvester=h, dork_lookup=_miss, lookup=lk, contacts=contacts)
    res = await r.resolve("e@x.com")
    assert res.username == "random_handle"
    assert res.method == "contact_import"
    assert res.confidence == "high"
    assert res.obfuscated_phone == "+1 **9"   # hints captured opportunistically
    assert h.looked_up == []                   # never fell through to guessing


# --- tier 2: lookup verification (confirm) ----------------------------------

async def test_lookup_verifies_permutation_high():
    h = FakeHarvester({"johnsmith": "Unrelated Name", "john_smith": None, "john.smith": None})
    lk = FakeLookup({"johnsmith": {"obfuscated_email": "j*******h@x.com"}})
    r = Resolver(harvester=h, dork_lookup=_miss, lookup=lk)
    res = await r.resolve("johnsmith@x.com")
    assert res.username == "johnsmith"
    assert res.method == "lookup_verified"
    assert res.confidence == "high"
    assert res.obfuscated_email == "j*******h@x.com"


async def test_digit_stripped_candidate_is_verified():
    # mirrors tprohit9@gmail.com -> @tprohit, confirmed by obfuscated recovery email
    h = FakeHarvester({"tprohit9": None, "tprohit": "Some One"})
    lk = FakeLookup({"tprohit": {"obfuscated_email": "t******9@gmail.com"}})
    r = Resolver(harvester=h, dork_lookup=_miss, lookup=lk)
    res = await r.resolve("tprohit9@gmail.com")
    assert res.username == "tprohit"
    assert res.method == "lookup_verified"
    assert res.confidence == "high"


# --- tier 2: lookup verification (REFUTE a false positive) ------------------

async def test_lookup_refutes_same_named_stranger():
    # The exact old failure: a name match would have accepted this account, but
    # Instagram's obfuscated recovery email says it is NOT the target email.
    h = FakeHarvester({"tprohit": "Prohit Thakur", "tprohit9": None})
    lk = FakeLookup({"tprohit": {"obfuscated_email": "p*****t@gmail.com"}})  # different person
    r = Resolver(harvester=h, dork_lookup=_miss, lookup=lk)
    assert await r.resolve("tprohit9@gmail.com") is None


# --- tier 3: degraded fallbacks (lookup unavailable) ------------------------

async def test_dork_existing_falls_back_to_medium_without_lookup():
    h = FakeHarvester({"johnny": "Whoever"})
    r = Resolver(harvester=h, dork_lookup=await _hit("johnny"), lookup=None)
    res = await r.resolve("john@x.com")
    assert res.username == "johnny"
    assert res.method == "dork"
    assert res.confidence == "medium"


async def test_dork_hit_that_404s_is_skipped():
    h = FakeHarvester({})  # the dorked handle does not actually exist
    r = Resolver(harvester=h, dork_lookup=await _hit("ghosthandle"), lookup=None,
                 enable_permutation=False)
    assert await r.resolve("john@x.com") is None


async def test_permutation_name_match_low_without_lookup():
    h = FakeHarvester({"johnsmith": "John Smith", "john_smith": None, "john.smith": None})
    r = Resolver(harvester=h, dork_lookup=_miss, lookup=None)
    res = await r.resolve("john.smith@x.com")
    assert res.username == "johnsmith"
    assert res.method == "permutation"
    assert res.confidence == "low"


async def test_permutation_name_mismatch_rejected_without_lookup():
    h = FakeHarvester({"johnsmith": "Totally Different", "john_smith": None, "john.smith": None})
    r = Resolver(harvester=h, dork_lookup=_miss, lookup=None)
    assert await r.resolve("john.smith@x.com") is None


async def test_medium_dork_beats_low_permutation_fallback():
    # dork handle exists (medium) and a permutation also exists with a name match (low):
    # the stronger signal wins.
    h = FakeHarvester({"dorkhandle": "No Match", "john": "John Doe"})
    r = Resolver(harvester=h, dork_lookup=await _hit("dorkhandle"), lookup=None)
    res = await r.resolve("john@x.com")
    assert res.method == "dork"
    assert res.confidence == "medium"


# --- robustness -------------------------------------------------------------

async def test_lookup_throttle_degrades_to_fallback():
    h = FakeHarvester({"johnny": "Whoever"})
    lk = FakeLookup(boom=True)  # every lookup raises -> treated as "no info"
    r = Resolver(harvester=h, dork_lookup=await _hit("johnny"), lookup=lk)
    res = await r.resolve("john@x.com")
    assert res.method == "dork" and res.confidence == "medium"


async def test_dork_exception_falls_through_to_permutation():
    h = FakeHarvester({"nasa": "NASA"})

    async def dork_boom(_e):
        raise RuntimeError("ddg blocked")

    r = Resolver(harvester=h, dork_lookup=dork_boom, lookup=None)
    res = await r.resolve("nasa@x.com")
    assert res.username == "nasa"
    assert res.method == "permutation"


async def test_harvest_error_on_candidate_is_skipped():
    class Boom(FakeHarvester):
        async def fetch_profile(self, username):
            raise HarvestError("blocked")

    r = Resolver(harvester=Boom({}), dork_lookup=_miss, lookup=None)
    assert await r.resolve("john.smith@x.com") is None


async def test_all_miss_returns_none():
    h = FakeHarvester({})
    r = Resolver(harvester=h, dork_lookup=_miss, lookup=None, contacts=FakeContacts())
    assert await r.resolve("nobody@x.com") is None


async def test_resolution_is_cached(tmp_path):
    cache = JsonCache(tmp_path, ttl=1000, now=lambda: 1.0)
    h = FakeHarvester({"johnny": "Whoever"})
    calls = []

    async def dork(e):
        calls.append(e)
        return "johnny"

    r = Resolver(harvester=h, dork_lookup=dork, lookup=None, cache=cache)
    first = await r.resolve("john@x.com")
    second = await r.resolve("john@x.com")
    assert first.username == second.username == "johnny"
    assert first.method == second.method == "dork"
    assert calls == ["john@x.com"]  # second call served from cache
