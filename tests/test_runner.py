import asyncio

import pytest

from instamail.base import AccountNotFound, BasePlugin
from instamail.runner import ContractViolation, FetchResult, iter_results


class Ok(BasePlugin):
    name = "ok"
    fields = ["x"]
    async def fetch(self, email):
        return {"x": email}


class Missing(BasePlugin):
    name = "missing"
    fields = ["x"]
    async def fetch(self, email):
        raise AccountNotFound("nope")


class Boom(BasePlugin):
    name = "boom"
    fields = ["x"]
    async def fetch(self, email):
        raise ValueError("kaboom")


class Slow(BasePlugin):
    name = "slow"
    fields = ["x"]
    timeout = 0.05
    async def fetch(self, email):
        await asyncio.sleep(1)
        return {"x": 1}


class Bad(BasePlugin):
    name = "bad"
    fields = ["x"]
    async def fetch(self, email):
        return {"y": 1}  # wrong key


async def _collect(emails, plugins):
    return [pair async for pair in iter_results(emails, plugins)]


async def test_ok_results_in_input_order():
    rows = await _collect(["b@x.com", "a@x.com"], [Ok()])
    assert [e for e, _ in rows] == ["b@x.com", "a@x.com"]
    assert rows[0][1]["ok"].status == "ok"
    assert rows[0][1]["ok"].data == {"x": "b@x.com"}


async def test_not_found_and_error_classified():
    rows = await _collect(["a@x.com"], [Missing(), Boom()])
    res = rows[0][1]
    assert res["missing"].status == "not_found"
    assert res["boom"].status == "error" and "kaboom" in res["boom"].message


async def test_timeout_is_error():
    rows = await _collect(["a@x.com"], [Slow()])
    assert rows[0][1]["slow"].status == "error"
    assert "timeout" in rows[0][1]["slow"].message


async def test_contract_violation_is_fatal():
    with pytest.raises(ContractViolation, match="bad"):
        await _collect(["a@x.com"], [Bad()])


async def test_per_plugin_concurrency_respected():
    class Counter(BasePlugin):
        name = "counter"
        fields = ["x"]
        max_concurrency = 2
        live = 0
        peak = 0
        async def fetch(self, email):
            type(self).live += 1
            type(self).peak = max(type(self).peak, type(self).live)
            await asyncio.sleep(0.01)
            type(self).live -= 1
            return {"x": 1}
    c = Counter()
    await _collect([f"{i}@x.com" for i in range(10)], [c])
    assert Counter.peak <= 2
