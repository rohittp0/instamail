"""Enrichment providers: username -> normalized profile + reel-view metrics.

FREE public path (web_profile_info) is fully implemented and tested. The OFFICIAL (Business
Discovery) and PAID (vendor) adapters carry real request code but are exercised only via mocks
(no live credentials to verify against). All adapters degrade to None on any failure.
"""

from __future__ import annotations

import logging
import os
from typing import Mapping

from ..config import ENV_BRIGHTDATA, ENV_META_IG_USER, ENV_META_TOKEN, Tier
from ..http import HarvestError, ProfileNotFound, default_session
from ..metrics import compute_metrics
from .base import EnrichmentProvider

log = logging.getLogger("instamail.instagram")


def normalize_web_profile(user: dict, window: int | None = None) -> dict:
    """Project a raw web_profile_info `user` object into the pipeline's profile shape."""
    m = compute_metrics(user, window)
    return {
        "instagram_id": user.get("id"),
        "username": user.get("username"),
        "full_name": user.get("full_name"),
        "followers": (user.get("edge_followed_by") or {}).get("count"),
        "following": (user.get("edge_follow") or {}).get("count"),
        "posts": (user.get("edge_owner_to_timeline_media") or {}).get("count"),
        "is_business": bool(user.get("is_business_account")),
        "is_verified": bool(user.get("is_verified")),
        "external_url": user.get("external_url"),
        "biography": user.get("biography"),
        "avg_views": m["avg_views"],
        "max_views": m["max_views"],
        "public_email": user.get("business_email") or user.get("public_email"),
        "top_hashtags": m["top_hashtags"],
    }


def _empty_metrics_profile(**base) -> dict:
    """A normalized profile with view metrics unavailable (used by non-public adapters)."""
    return {
        "instagram_id": None, "username": None, "full_name": None, "followers": None,
        "following": None, "posts": None, "is_business": None, "is_verified": None,
        "external_url": None, "biography": None, "avg_views": None, "max_views": None,
        "public_email": None, "top_hashtags": None, **base,
    }


class PublicEnrichment(EnrichmentProvider):
    name = "public"
    tier = Tier.FREE

    def __init__(self, harvester, window: int | None = None):
        self._harvester = harvester
        self._window = window

    async def enrich(self, username: str) -> dict | None:
        try:
            user = await self._harvester.fetch_profile(username)
        except ProfileNotFound:
            return None
        except HarvestError as exc:
            log.debug("public enrich blocked for %s: %s", username, exc)
            return None
        return normalize_web_profile(user, self._window)


class MetaEnrichment(EnrichmentProvider):
    """Business Discovery — public metadata/metrics for another professional account.

    Note: Business Discovery does not expose per-reel reach, so avg/max views stay None."""

    name = "business_discovery"
    tier = Tier.OFFICIAL
    requires_env = (ENV_META_TOKEN, ENV_META_IG_USER)

    def __init__(self, session=None, env: Mapping[str, str] | None = None):
        self._session = session
        self._env = env if env is not None else os.environ

    async def enrich(self, username: str) -> dict | None:
        session = self._session or default_session()
        ig_user = self._env.get(ENV_META_IG_USER)
        token = self._env.get(ENV_META_TOKEN)
        fields = (
            f"business_discovery.username({username})"
            "{id,username,name,followers_count,media_count,website,biography}"
        )
        try:
            resp = await session.get(
                f"https://graph.facebook.com/v21.0/{ig_user}",
                params={"fields": fields, "access_token": token},
            )
            bd = (resp.json() or {}).get("business_discovery")
        except Exception as exc:  # any auth/parse/network failure -> miss
            log.debug("business_discovery failed for %s: %s", username, exc)
            return None
        if not bd:
            return None
        return _empty_metrics_profile(
            instagram_id=bd.get("id"), username=bd.get("username"),
            full_name=bd.get("name"), followers=bd.get("followers_count"),
            posts=bd.get("media_count"), external_url=bd.get("website"),
            biography=bd.get("biography"), is_business=True,
        )


class VendorEnrichment(EnrichmentProvider):
    """Paid vendor enrichment (Bright Data). Real request shape; untested live."""

    name = "vendor"
    tier = Tier.PAID
    requires_env = (ENV_BRIGHTDATA,)

    def __init__(self, session=None, env: Mapping[str, str] | None = None):
        self._session = session
        self._env = env if env is not None else os.environ

    async def enrich(self, username: str) -> dict | None:
        session = self._session or default_session()
        key = self._env.get(ENV_BRIGHTDATA)
        try:
            resp = await session.post(
                "https://api.brightdata.com/datasets/v3/scrape",
                params={"dataset_id": "instagram_profiles"},
                headers={"Authorization": f"Bearer {key}"},
                json={"url": f"https://www.instagram.com/{username}/"},
            )
            data = resp.json()
        except Exception as exc:
            log.debug("vendor enrich failed for %s: %s", username, exc)
            return None
        rec = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else None
        if not rec:
            return None
        return _empty_metrics_profile(
            instagram_id=rec.get("id") or rec.get("user_id"),
            username=rec.get("username") or username,
            full_name=rec.get("full_name") or rec.get("name"),
            followers=rec.get("followers") or rec.get("followers_count"),
            posts=rec.get("posts_count"), external_url=rec.get("external_url"),
            biography=rec.get("biography"),
            avg_views=rec.get("avg_views"), max_views=rec.get("max_views"),
            public_email=rec.get("email") or rec.get("business_email"),
        )
