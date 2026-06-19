"""Second email-keyed fixture; overlaps email_a on a@x.com to exercise same-key merge."""

from instamail.base import BasePlugin


class EmailBPlugin(BasePlugin):
    name = "email_b"
    key = "email"
    fields = ["handle"]

    async def search(self, terms, opts):
        return [
            {"email": "a@x.com", "handle": "@al"},
            {"email": "c@x.com", "handle": "@cy"},
        ]
