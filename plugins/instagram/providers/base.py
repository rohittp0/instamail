"""Provider base + layer interfaces.

A provider belongs to a layer (discovery / enrichment / email / verification) and a tier
(FREE < OFFICIAL < PAID). It is *eligible* for a run only when every env var it requires is
present (presence == consent). The pipeline tries a layer's eligible providers in ascending
tier and takes the first success, so a PAID provider runs only when cheaper ones came up empty.
"""

from __future__ import annotations

from typing import Mapping

from ..config import Tier


class Provider:
    name: str = ""
    tier: Tier = Tier.FREE
    requires_env: tuple[str, ...] = ()

    def available(self, env: Mapping[str, str]) -> bool:
        """Eligible only if all required env vars are set (and non-empty)."""
        return all(env.get(k) for k in self.requires_env)


class DiscoveryProvider(Provider):
    async def discover(self, terms: str, limit: int) -> list[str]:
        """Return candidate usernames for the search terms (best-effort, may be empty)."""
        raise NotImplementedError


class EnrichmentProvider(Provider):
    async def enrich(self, username: str) -> dict | None:
        """Return a normalized profile dict for the username, or None if not found."""
        raise NotImplementedError


class EmailProvider(Provider):
    async def find(self, profile: dict) -> str | None:
        """Return an email for the (already enriched) profile, or None."""
        raise NotImplementedError


class Verifier(Provider):
    async def verify(self, email: str) -> str:
        """Return a confidence label, e.g. 'valid' / 'invalid' / 'unknown'."""
        raise NotImplementedError
