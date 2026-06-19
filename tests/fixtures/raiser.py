"""Fixture whose search() raises a normal runtime error (isolated, non-fatal)."""

from instamail.base import BasePlugin


class RaiserPlugin(BasePlugin):
    name = "raiser"
    key = "email"
    fields = ["x"]

    async def search(self, terms, opts):
        raise RuntimeError("boom")
