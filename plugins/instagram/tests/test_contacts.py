import json

from instagram.contacts import (
    ContactImporter,
    account_id_from_sessionid,
    extract_matches,
)


# --- pure helpers -----------------------------------------------------------

def test_account_id_parsed_from_sessionid():
    assert account_id_from_sessionid("7635741101%3AH2Gz%3A23%3Axyz") == "7635741101"
    assert account_id_from_sessionid("7635741101:raw:23") == "7635741101"
    assert account_id_from_sessionid("notanid%3Axyz") is None
    assert account_id_from_sessionid(None) is None
    assert account_id_from_sessionid("") is None


def test_extract_matches_direct_users():
    payload = {"users": [{"username": "RealHandle", "full_name": "Real", "pk": 42}], "status": "ok"}
    assert extract_matches(payload) == [{"username": "realhandle", "full_name": "Real", "pk": 42}]


def test_extract_matches_nested_user_object():
    payload = {"users": [{"user": {"username": "h", "full_name": "H", "id": 7}}]}
    assert extract_matches(payload) == [{"username": "h", "full_name": "H", "pk": 7}]


def test_extract_matches_ignores_non_user_keys_and_empty():
    assert extract_matches({"suggested_users": {"suggestions": [{"username": "pymk"}]}}) == []
    assert extract_matches({"status": "ok"}) == []
    assert extract_matches({"users": []}) == []
    assert extract_matches("nope") == []


# --- ContactImporter --------------------------------------------------------

class FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def post(self, url, data=None, params=None, headers=None, cookies=None, **kw):
        self.calls.append({"url": url, "data": data, "params": params,
                           "headers": headers, "cookies": cookies})
        return self._response


def test_disabled_without_session():
    ci = ContactImporter(session=FakeSession(FakeResp(200)), sessionid=None)
    assert ci.enabled is False


def test_enabled_with_valid_session():
    ci = ContactImporter(session=FakeSession(FakeResp(200)), sessionid="7635741101%3Ax%3A23")
    assert ci.enabled is True


async def test_find_by_email_returns_empty_when_disabled():
    session = FakeSession(FakeResp(200, {"users": [{"username": "x"}]}))
    ci = ContactImporter(session=session, sessionid=None)
    assert await ci.find_by_email("e@x.com") == []
    assert session.calls == []  # never hits the network without a session


async def test_find_by_email_parses_matches_and_uploads_email():
    payload = {"users": [{"username": "RealHandle", "full_name": "R", "pk": 1}]}
    session = FakeSession(FakeResp(200, payload))
    ci = ContactImporter(session=session, sessionid="7635741101%3Ax%3A23")
    out = await ci.find_by_email("target@gmail.com")
    assert out == [{"username": "realhandle", "full_name": "R", "pk": 1}]
    body = session.calls[0]["data"]
    assert body.startswith("signed_body=SIGNATURE.")
    assert "target%40gmail.com" in body or "target@gmail.com" in json.dumps(body)
    assert session.calls[0]["cookies"]["sessionid"].startswith("7635741101")


async def test_find_by_email_empty_on_non_200():
    session = FakeSession(FakeResp(429, {"message": "throttled"}))
    ci = ContactImporter(session=session, sessionid="7635741101%3Ax%3A23")
    assert await ci.find_by_email("e@x.com") == []
