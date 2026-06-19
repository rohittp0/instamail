"""Discovery providers: search terms -> candidate Instagram usernames.

Free discovery is the pipeline's weakest link (no sanctioned anonymous search). DuckDuckGo
dorking needs no IG auth but is rate-limited and relevance-noisy; IG topsearch returns
structured users but is block-prone without a session. OFFICIAL Hashtag Search and PAID vendor
adapters give real coverage when credentials exist (real request shape; untested live).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Mapping

from ..config import (ENV_BRIGHTDATA, ENV_GOOGLE_CSE, ENV_GOOGLE_KEY, ENV_META_IG_USER,
                      ENV_META_TOKEN, Tier)
from ..http import default_session, extract_at_mentions, extract_handles
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
            text = getattr(resp, "text", "") or ""
            handles = extract_handles(text)
        except Exception as exc:
            log.debug("ddg discovery failed: %s", exc)
            return []
        if not handles and (getattr(resp, "status_code", 200) != 200 or "anomaly" in text.lower()):
            log.warning("instagram: DuckDuckGo discovery blocked (challenge page); "
                        "relying on other discovery providers")
        return handles[:limit]


class IgSearchDiscovery(DiscoveryProvider):
    """FREE-ish: Instagram topsearch endpoint. Anonymous access is blocked; a valid
    INSTAGRAM_SESSIONID cookie is what makes it return keyword-matched users."""

    name = "ig_topsearch"
    tier = Tier.FREE

    #: Cap topsearch calls per run so a long phrase can't fan out into many requests.
    MAX_QUERIES = 6

    def __init__(self, session=None, sessionid: str | None = None):
        self._session = session
        self._sessionid = sessionid

    async def discover(self, terms: str, limit: int) -> list[str]:
        session = self._session or default_session()
        cookies = {"sessionid": self._sessionid} if self._sessionid else None

        # topsearch matches account names/keywords, not long phrases, and caps ~5 hits/query.
        # So query the full phrase plus each distinctive token (longest first) and union the
        # results — broadening recall. Generic single-word noise is meant to be trimmed
        # downstream by --instagram-travel-only and view-based ranking.
        out: list[str] = []
        for query in self._queries(terms):
            for uname in await self._query(session, query, cookies):
                if uname not in out:
                    out.append(uname)
            if len(out) >= limit:
                break
        return out[:limit]

    def _queries(self, terms: str) -> list[str]:
        full = terms.strip()
        tokens = sorted({t for t in re.findall(r"[A-Za-z0-9]+", terms) if len(t) >= 4},
                        key=len, reverse=True)
        queries = ([full] if full else []) + [t for t in tokens if t.lower() != full.lower()]
        return queries[:self.MAX_QUERIES]

    async def _query(self, session, query: str, cookies) -> list[str]:
        try:
            resp = await session.get(
                "https://www.instagram.com/web/search/topsearch/",
                params={"context": "blended", "query": query},
                headers={"User-Agent": "Mozilla/5.0", "x-ig-app-id": "936619743392459"},
                cookies=cookies,
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
        return out


class GoogleDiscovery(DiscoveryProvider):
    """Google Programmable Search (Custom Search JSON API), restricted to instagram.com.

    Full-text web search (indexes captions/bios), so it finds creators who *describe* the
    content even when their username doesn't match — broader than topsearch. Free 100
    queries/day; needs GOOGLE_API_KEY + GOOGLE_CSE_ID. Handles are pulled from each result's
    link, title, and snippet (so profile handles surface even from /p/ post URLs)."""

    name = "google_cse"
    tier = Tier.FREE  # free quota; gated on its two credentials below
    requires_env = (ENV_GOOGLE_KEY, ENV_GOOGLE_CSE)

    def __init__(self, session=None, env: Mapping[str, str] | None = None):
        self._session = session
        self._env = env if env is not None else os.environ

    async def discover(self, terms: str, limit: int) -> list[str]:
        session = self._session or default_session()
        params_base = {
            "key": self._env.get(ENV_GOOGLE_KEY), "cx": self._env.get(ENV_GOOGLE_CSE),
            "q": f"{terms} site:instagram.com", "num": 10,
        }
        out: list[str] = []
        start = 1
        while len(out) < limit and start <= 91:   # CSE allows start up to 91 (100 results)
            try:
                resp = await session.get(
                    "https://www.googleapis.com/customsearch/v1",
                    params={**params_base, "start": start},
                )
                items = (resp.json() or {}).get("items") or []
            except Exception as exc:
                log.debug("google_cse discovery failed: %s", exc)
                break
            if not items:
                break
            for it in items:
                # profile handle from the result URL, plus the (@handle) in the result title
                handles = extract_handles(it.get("link") or "") + extract_at_mentions(it.get("title") or "")
                for handle in handles:
                    if handle not in out:
                        out.append(handle)
            start += 10
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
