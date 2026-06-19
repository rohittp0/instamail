"""Email providers (provenance ladder): profile -> best email candidate.

Tried free-first: the creator's published IG business email (highest confidence), then a crawl
of their linked website, then PAID Hunter as a last resort. The first provider to return an
address wins, and the pipeline records which one as ``email_source``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Mapping
from urllib.parse import urlparse

from ..config import ENV_HUNTER, Tier
from ..http import default_session
from .base import EmailProvider

log = logging.getLogger("instamail.instagram")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Role mailboxes that signal a real outreach contact, best-first.
_PREFERRED_LOCALPARTS = ("partnerships", "press", "media", "collab", "booking", "contact", "hello", "info")


def _rank_emails(emails: list[str]) -> list[str]:
    """De-dup (case-insensitive) and sort so preferred role mailboxes come first."""
    seen: dict[str, str] = {}
    for e in emails:
        seen.setdefault(e.lower(), e)
    uniq = list(seen.values())

    def score(e: str) -> int:
        local = e.split("@", 1)[0].lower()
        for i, pref in enumerate(_PREFERRED_LOCALPARTS):
            if local.startswith(pref):
                return i
        return len(_PREFERRED_LOCALPARTS)

    return sorted(uniq, key=score)


class IgEmail(EmailProvider):
    """FREE: the public/business email the creator published on their IG profile."""

    name = "ig_public"
    tier = Tier.FREE

    async def find(self, profile: dict) -> str | None:
        return profile.get("public_email") or None


class WebsiteEmail(EmailProvider):
    """FREE: crawl the profile's linked website and extract the best contact email."""

    name = "website"
    tier = Tier.FREE

    def __init__(self, session=None):
        self._session = session

    async def find(self, profile: dict) -> str | None:
        url = profile.get("external_url")
        if not url:
            return None
        session = self._session or default_session()
        try:
            resp = await session.get(url, headers={"User-Agent": "Mozilla/5.0"})
            found = _EMAIL_RE.findall(getattr(resp, "text", "") or "")
        except Exception as exc:
            log.debug("website crawl failed for %s: %s", url, exc)
            return None
        ranked = _rank_emails(found)
        return ranked[0] if ranked else None


class HunterEmail(EmailProvider):
    """PAID: Hunter domain search for the linked website's domain. Untested live."""

    name = "hunter"
    tier = Tier.PAID
    requires_env = (ENV_HUNTER,)

    def __init__(self, session=None, env: Mapping[str, str] | None = None):
        self._session = session
        self._env = env if env is not None else os.environ

    async def find(self, profile: dict) -> str | None:
        url = profile.get("external_url")
        domain = urlparse(url).netloc.lower().lstrip("www.") if url else ""
        if not domain:
            return None
        session = self._session or default_session()
        try:
            resp = await session.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": domain, "api_key": self._env.get(ENV_HUNTER), "limit": 1},
            )
            emails = ((resp.json() or {}).get("data") or {}).get("emails") or []
        except Exception as exc:
            log.debug("hunter lookup failed for %s: %s", domain, exc)
            return None
        return emails[0].get("value") if emails else None
