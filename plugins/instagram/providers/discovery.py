"""Discovery providers: search terms -> candidate Instagram usernames.

Free discovery is the pipeline's weakest link (no sanctioned anonymous search). DuckDuckGo
dorking needs no IG auth but is rate-limited and relevance-noisy; IG topsearch returns
structured users but is block-prone without a session. OFFICIAL Hashtag Search and PAID vendor
adapters give real coverage when credentials exist (real request shape; untested live).
"""

from __future__ import annotations

import logging
import os
from typing import Mapping

from ..config import ENV_BRIGHTDATA, ENV_META_IG_USER, ENV_META_TOKEN, Tier
from ..http import default_session, extract_handles
from .base import DiscoveryProvider

log = logging.getLogger("instamail.instagram")


class DdgDiscovery(DiscoveryProvider):
    """FREE: scrape DuckDuckGo HTML for `{terms} site:instagram.com` profile links."""

    name = "ddg"
    tier = Tier.FREE

    def __init__(self, session=None):
        self._session = session

    async def discover(self, terms: str, limit: int) -> list[str]:
        session = self._session or default_session()
        try:
            resp = await session.get(
                "https://html.duckduckgo.com/html/",
                params={"q": f"{terms} site:instagram.com"},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            handles = extract_handles(getattr(resp, "text", "") or "")
        except Exception as exc:
            log.debug("ddg discovery failed: %s", exc)
            return []
        return handles[:limit]


class IgSearchDiscovery(DiscoveryProvider):
    """FREE-ish: Instagram topsearch endpoint (more reliable with INSTAGRAM_SESSIONID)."""

    name = "ig_topsearch"
    tier = Tier.FREE

    def __init__(self, session=None):
        self._session = session

    async def discover(self, terms: str, limit: int) -> list[str]:
        session = self._session or default_session()
        try:
            resp = await session.get(
                "https://www.instagram.com/web/search/topsearch/",
                params={"context": "blended", "query": terms},
                headers={"User-Agent": "Mozilla/5.0", "x-ig-app-id": "936619743392459"},
            )
            users = (resp.json() or {}).get("users") or []
        except Exception as exc:
            log.debug("ig topsearch discovery failed: %s", exc)
            return []
        out: list[str] = []
        for entry in users:
            uname = (entry.get("user") or {}).get("username")
            if uname and uname not in out:
                out.append(uname)
        return out[:limit]


class MetaDiscovery(DiscoveryProvider):
    """OFFICIAL: Hashtag Search -> recent media -> usernames. Untested live."""

    name = "hashtag_search"
    tier = Tier.OFFICIAL
    requires_env = (ENV_META_TOKEN, ENV_META_IG_USER)

    def __init__(self, session=None, env: Mapping[str, str] | None = None):
        self._session = session
        self._env = env if env is not None else os.environ

    async def discover(self, terms: str, limit: int) -> list[str]:
        session = self._session or default_session()
        ig_user = self._env.get(ENV_META_IG_USER)
        token = self._env.get(ENV_META_TOKEN)
        tag = terms.split()[0].lstrip("#") if terms.split() else terms
        try:
            r1 = await session.get(
                "https://graph.facebook.com/v21.0/ig_hashtag_search",
                params={"user_id": ig_user, "q": tag, "access_token": token},
            )
            hashtag_id = ((r1.json() or {}).get("data") or [{}])[0].get("id")
            if not hashtag_id:
                return []
            r2 = await session.get(
                f"https://graph.facebook.com/v21.0/{hashtag_id}/recent_media",
                params={"user_id": ig_user, "fields": "username", "access_token": token},
            )
            media = (r2.json() or {}).get("data") or []
        except Exception as exc:
            log.debug("hashtag_search discovery failed: %s", exc)
            return []
        out: list[str] = []
        for m in media:
            uname = m.get("username")
            if uname and uname not in out:
                out.append(uname)
        return out[:limit]


class VendorDiscovery(DiscoveryProvider):
    """PAID: vendor keyword/hashtag discovery (Bright Data). Untested live."""

    name = "vendor"
    tier = Tier.PAID
    requires_env = (ENV_BRIGHTDATA,)

    def __init__(self, session=None, env: Mapping[str, str] | None = None):
        self._session = session
        self._env = env if env is not None else os.environ

    async def discover(self, terms: str, limit: int) -> list[str]:
        session = self._session or default_session()
        key = self._env.get(ENV_BRIGHTDATA)
        try:
            resp = await session.post(
                "https://api.brightdata.com/datasets/v3/scrape",
                params={"dataset_id": "instagram_search"},
                headers={"Authorization": f"Bearer {key}"},
                json={"query": terms, "limit": limit},
            )
            data = resp.json()
        except Exception as exc:
            log.debug("vendor discovery failed: %s", exc)
            return []
        rows = data if isinstance(data, list) else (data or {}).get("results") or []
        out: list[str] = []
        for rec in rows:
            uname = rec.get("username") if isinstance(rec, dict) else None
            if uname and uname not in out:
                out.append(uname)
        return out[:limit]
