import pytest
from instamail.base import BasePlugin, AccountNotFound


class Demo(BasePlugin):
    name = "demo"
    fields = ["a", "b"]

    async def fetch(self, email):
        return {"a": 1, "b": None}


def test_defaults_and_attrs():
    p = Demo()
    assert p.name == "demo"
    assert p.fields == ["a", "b"]
    assert p.max_concurrency == 5
    assert p.timeout == 10.0


async def test_fetch_runs():
    assert await Demo().fetch("x@y.com") == {"a": 1, "b": None}


async def test_base_fetch_not_implemented():
    with pytest.raises(NotImplementedError):
        await BasePlugin().fetch("x@y.com")


def test_account_not_found_is_exception():
    assert issubclass(AccountNotFound, Exception)
