"""Phone-keyed fixture; different key type from the email plugins (stacks on merge)."""

from instamail.base import BasePlugin


class PhoneAPlugin(BasePlugin):
    name = "phone_a"
    key = "phone"
    fields = ["carrier"]

    async def search(self, terms, opts):
        return [{"phone": "+15550001", "carrier": "AT&T"}]
