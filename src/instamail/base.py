from typing import Any


class AccountNotFound(Exception):
    """Raised by a plugin when an email cleanly has no account on its platform."""


class BasePlugin:
    """Base class for all enrichment plugins. Subclass and drop into ./plugins/."""

    name: str
    fields: list[str]
    max_concurrency: int = 5
    timeout: float = 10.0

    async def fetch(self, email: str) -> dict[str, Any]:
        """Resolve one email into {field: value}. Keys must exactly match `fields`;
        a field with no value must be None. Raise AccountNotFound on a clean miss."""
        raise NotImplementedError
