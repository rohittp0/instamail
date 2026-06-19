from instamail.base import BasePlugin


class PkgPlugin(BasePlugin):
    name = "pkg"
    key = "email"
    fields = ["z"]

    async def search(self, terms, opts):
        return [{"email": "p@x.com", "z": "zed"}]
