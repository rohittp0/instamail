"""InstagramPlugin: resolves an email to an Instagram username (best-effort) then harvests
public-profile metrics. Lives in the plugins/instagram package and is re-exported from the
package __init__ so the loader registers it."""

import os
import random
from pathlib import Path
from typing import Any

from instamail.base import AccountNotFound, BasePlugin

from . import metrics
from .cache import JsonCache
from .harvester import AsyncRateLimiter, Harvester, ProfileNotFound
from .resolver import Resolution, Resolver

_CACHE_DIR = Path(".cache/instagram")
_PROFILE_TTL = 24 * 3600
_RESOLUTION_TTL = 7 * 24 * 3600


def _count(user: dict, edge: str) -> int | None:
    return (user.get(edge) or {}).get("count")


class InstagramPlugin(BasePlugin):
    name = "instagram"
    max_concurrency = 2
    timeout = 60.0
    fields = [
        "username", "id", "followers", "following", "posts", "avg_views", "max_views",
        "full_name", "biography", "external_url", "is_verified", "is_private", "profile_pic_url",
        "is_business", "business_category", "public_email", "public_phone",
        "engagement_rate", "avg_likes", "avg_comments", "posts_analyzed",
        "top_hashtags", "posting_cadence_per_week", "reels_ratio", "last_post_date",
        "resolution_method", "resolution_confidence",
    ]

    def __init__(self, resolver=None, harvester=None):
        if harvester is None:
            sessionid = os.environ.get("INSTAGRAM_SESSIONID")
            limiter = AsyncRateLimiter(
                min_interval=5.0 if sessionid else 18.0,
                jitter=lambda: 1.0 + random.random() * 0.5,
            )
            harvester = Harvester(
                cache=JsonCache(_CACHE_DIR / "profiles", ttl=_PROFILE_TTL),
                rate_limiter=limiter,
            )
        if resolver is None:
            resolver = Resolver(
                harvester=harvester,
                cache=JsonCache(_CACHE_DIR / "resolutions", ttl=_RESOLUTION_TTL),
            )
        self._resolver = resolver
        self._harvester = harvester

    async def fetch(self, email: str) -> dict[str, Any]:
        resolution = await self._resolver.resolve(email)
        if resolution is None:
            raise AccountNotFound(email)
        try:
            user = await self._harvester.fetch_profile(resolution.username)
        except ProfileNotFound as e:
            raise AccountNotFound(email) from e
        return self._build_row(resolution, user)

    def _build_row(self, resolution: Resolution, user: dict) -> dict[str, Any]:
        m = metrics.compute_metrics(user)
        row = {
            "username": user.get("username") or resolution.username,
            "id": user.get("id"),
            "followers": _count(user, "edge_followed_by"),
            "following": _count(user, "edge_follow"),
            "posts": _count(user, "edge_owner_to_timeline_media"),
            "full_name": user.get("full_name"),
            "biography": user.get("biography"),
            "external_url": user.get("external_url"),
            "is_verified": user.get("is_verified"),
            "is_private": user.get("is_private"),
            "profile_pic_url": user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
            "is_business": user.get("is_business_account"),
            "business_category": user.get("business_category_name") or user.get("category_name"),
            "public_email": user.get("business_email"),
            "public_phone": user.get("business_phone_number"),
            "resolution_method": resolution.method,
            "resolution_confidence": resolution.confidence,
        }
        row.update(m)  # avg_views, max_views, avg_likes, avg_comments, engagement_rate,
                       # posts_analyzed, reels_ratio, posting_cadence_per_week,
                       # last_post_date, top_hashtags
        return row
