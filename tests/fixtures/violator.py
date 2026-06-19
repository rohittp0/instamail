"""Fixture that returns a row violating its contract (wrong keys) — fatal."""

from instamail.base import BasePlugin


class ViolatorPlugin(BasePlugin):
    name = "violator"
    key = "email"
    fields = ["x"]

    async def search(self, terms, opts):
        return [{"email": "z@x.com", "y": "wrong-key"}]
