"""Email-keyed fixture plugin that also exercises a filter arg."""

from instamail.base import BasePlugin


class EmailAPlugin(BasePlugin):
    name = "email_a"
    key = "email"
    fields = ["name", "followers"]

    @classmethod
    def add_arguments(cls, group):
        group.add_argument("--min-followers", type=int, default=None)

    async def search(self, terms, opts):
        rows = [
            {"email": "a@x.com", "name": "Al", "followers": 100},
            {"email": "b@x.com", "name": "Bea", "followers": 50},
        ]
        if opts.min_followers is not None:
            rows = [r for r in rows if r["followers"] >= opts.min_followers]
        return rows
