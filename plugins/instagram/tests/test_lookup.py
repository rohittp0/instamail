import pytest

from instagram.lookup import (
    InstagramLookup,
    LookupBlocked,
    obfuscated_email_matches,
)


# --- obfuscation matching (the verification primitive) ----------------------

def test_obfuscation_matches_first_last_and_domain():
    # Instagram masks the local part keeping first + last char, domain intact.
    assert obfuscated_email_matches("t******9@gmail.com", "tprohit9@gmail.com")
    assert obfuscated_email_matches("j*****e@gmail.com", "johndoe@gmail.com")


def test_obfuscation_rejects_wrong_first_char():
    # @tprohit's recovery email would start with a different letter -> refuted.
    assert not obfuscated_email_matches("p*****t@gmail.com", "tprohit9@gmail.com")


def test_obfuscation_rejects_wrong_last_char():
    assert not obfuscated_email_matches("t******x@gmail.com", "tprohit9@gmail.com")


def test_obfuscation_rejects_wrong_domain():
    assert not obfuscated_email_matches("t******9@yahoo.com", "tprohit9@gmail.com")


def test_obfuscation_rejects_masked_last_char_and_empty():
    assert not obfuscated_email_matches("t*******@gmail.com", "tprohit9@gmail.com")
    assert not obfuscated_email_matches(None, "x@gmail.com")
    assert not obfuscated_email_matches("", "x@gmail.com")
    assert not obfuscated_email_matches("nodomain", "x@gmail.com")


# --- InstagramLookup client -------------------------------------------------

class FakeResp:
    def __init__(self, status_code, payload=None, headers=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def post(self, url, data=None, headers=None, cookies=None, **kw):
        self.calls.append({"url": url, "data": data, "headers": headers, "cookies": cookies})
        return self._responses.pop(0)


async def _noop_sleep(_s):
    return None


def _lookup(session, **kw):
    kw.setdefault("sleep", _noop_sleep)
    return InstagramLookup(session=session, **kw)


async def test_hit_returns_obfuscation_and_signs_body():
    payload = {"obfuscated_email": "t******9@gmail.com", "obfuscated_phone": "+91 *** **09"}
    session = FakeSession([FakeResp(200, payload)])
    lk = _lookup(session)
    out = await lk.lookup("tprohit")
    assert out["obfuscated_email"] == "t******9@gmail.com"
    assert session.calls[0]["data"].startswith("signed_body=SIGNATURE.")
    assert "tprohit" in session.calls[0]["data"]


async def test_clean_miss_returns_none():
    session = FakeSession([FakeResp(200, {"message": "No users found", "status": "fail"})])
    assert await _lookup(session).lookup("ghost") is None


async def test_404_returns_none():
    assert await _lookup(FakeSession([FakeResp(404)])).lookup("ghost") is None


async def test_throttle_then_success_retries():
    slept = []

    async def rec(s):
        slept.append(s)

    session = FakeSession([
        FakeResp(429, headers={"Retry-After": "2"}),
        FakeResp(200, {"obfuscated_email": "a****z@x.com"}),
    ])
    out = await _lookup(session, sleep=rec).lookup("a")
    assert out["obfuscated_email"] == "a****z@x.com"
    assert slept == [2.0]


async def test_persistent_throttle_raises_blocked():
    session = FakeSession([FakeResp(200, {"status": "fail", "message": ""}) for _ in range(5)])
    with pytest.raises(LookupBlocked):
        await _lookup(session, max_retries=3).lookup("x")


async def test_obfuscation_for_swallows_block():
    session = FakeSession([FakeResp(429) for _ in range(5)])
    # obfuscation_for must degrade a throttle to None (never propagate to the chain)
    assert await _lookup(session, max_retries=2).obfuscation_for("x") is None


async def test_sessionid_cookie_attached():
    session = FakeSession([FakeResp(200, {"obfuscated_email": "a****z@x.com"})])
    await _lookup(session, sessionid="SECRET").lookup("a")
    assert session.calls[0]["cookies"] == {"sessionid": "SECRET"}
