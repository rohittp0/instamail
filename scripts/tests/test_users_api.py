"""users_api: name join, cursor param, pagination, and transient-error retry (no network)."""

import pytest

import users_api


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv(users_api.ENV_API_KEY, "test-key")
    monkeypatch.delenv(users_api.ENV_API_BASE, raising=False)


def _noop_sleep(_):
    return None


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _ApiSession:
    """Honors ?email=<cursor>&limit=N against a fixed ordered users list."""

    def __init__(self, users):
        self.users = users
        self.calls = []

    def get(self, base, params=None, headers=None, timeout=None):
        self.calls.append(params)
        limit = params["limit"]
        after = params.get("email")
        if after is None:
            start = 0
        else:
            start = next((i + 1 for i, u in enumerate(self.users) if u["email"] == after), len(self.users))
        return _Resp(200, {"users": self.users[start:start + limit]})

    def close(self):
        pass


def _users(n):
    return [{"email": f"u{i}@x.com", "first_name": f"F{i}", "last_name": f"L{i}"} for i in range(n)]


def test_name_join_and_cursor_param():
    sess = _ApiSession(_users(3))
    out = users_api.fetch_users(after=None, limit=2, session=sess, sleep=_noop_sleep)
    assert out == [{"email": "u0@x.com", "name": "F0 L0"}, {"email": "u1@x.com", "name": "F1 L1"}]
    # second page uses the cursor param `email`
    out2 = users_api.fetch_users(after="u1@x.com", limit=2, session=sess, sleep=_noop_sleep)
    assert [u["email"] for u in out2] == ["u2@x.com"]
    assert sess.calls[-1].get("email") == "u1@x.com"


def test_pagination_follows_pages(monkeypatch):
    monkeypatch.setattr(users_api, "PAGE_SIZE", 2)   # force multi-page for limit=5
    sess = _ApiSession(_users(10))
    out = users_api.fetch_users(after=None, limit=5, session=sess, sleep=_noop_sleep)
    assert [u["email"] for u in out] == [f"u{i}@x.com" for i in range(5)]
    assert len(sess.calls) == 3                       # 2 + 2 + 1
    assert all(c["limit"] <= 2 for c in sess.calls)


def test_short_page_means_exhausted(monkeypatch):
    monkeypatch.setattr(users_api, "PAGE_SIZE", 2)
    sess = _ApiSession(_users(3))                     # fewer than the requested 5
    out = users_api.fetch_users(after=None, limit=5, session=sess, sleep=_noop_sleep)
    assert len(out) == 3


def test_empty_name_when_no_first_last():
    sess = _ApiSession([{"email": "admin@x.com", "first_name": "", "last_name": ""}])
    out = users_api.fetch_users(after=None, limit=1, session=sess, sleep=_noop_sleep)
    assert out == [{"email": "admin@x.com", "name": ""}]


class _FlakySession(_ApiSession):
    def __init__(self, users, fail_times):
        super().__init__(users)
        self._fail = fail_times

    def get(self, base, params=None, headers=None, timeout=None):
        if self._fail > 0:
            self._fail -= 1
            self.calls.append(params)
            return _Resp(503, text="deploying")
        return super().get(base, params=params, headers=headers, timeout=timeout)


def test_retries_transient_5xx_then_succeeds():
    sess = _FlakySession(_users(2), fail_times=2)     # two 503s, then 200
    out = users_api.fetch_users(after=None, limit=2, session=sess, sleep=_noop_sleep)
    assert [u["email"] for u in out] == ["u0@x.com", "u1@x.com"]


def test_4xx_is_not_retried_and_aborts():
    class _AuthFail(_ApiSession):
        def get(self, *a, **k):
            self.calls.append(k.get("params"))
            return _Resp(403, text="forbidden")

    sess = _AuthFail(_users(2))
    with pytest.raises(SystemExit):
        users_api.fetch_users(after=None, limit=2, session=sess, sleep=_noop_sleep)
    assert len(sess.calls) == 1                        # no retry on 4xx
