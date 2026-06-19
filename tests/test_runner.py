from argparse import Namespace
from pathlib import Path

import pytest

from instamail.base import ContractViolation
from instamail.loader import discover_plugins
from instamail.runner import run

FIXTURES = Path(__file__).parent / "fixtures"


def _plugins(*names):
    found = discover_plugins(FIXTURES)
    return [found[n] for n in names]


def _opts(plugins):
    # email_a is the only fixture that reads an opt.
    return {p.name: Namespace(min_followers=None) for p in plugins}


async def test_gather_returns_each_plugins_rows():
    plugins = _plugins("email_a", "email_b")
    out = await run(plugins, "x", _opts(plugins))
    assert {r["email"] for r in out["email_a"]} == {"a@x.com", "b@x.com"}
    assert {r["email"] for r in out["email_b"]} == {"a@x.com", "c@x.com"}


async def test_raising_plugin_is_isolated():
    plugins = _plugins("email_a", "raiser")
    out = await run(plugins, "x", _opts(plugins))
    assert out["raiser"] == []          # failed -> no rows
    assert len(out["email_a"]) == 2     # others unaffected


async def test_contract_violation_is_fatal():
    plugins = _plugins("violator")
    with pytest.raises(ContractViolation):
        await run(plugins, "x", _opts(plugins))
