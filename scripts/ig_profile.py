"""Instagram public-profile fetch + metrics, ported from the old plugin.

The free public path uses curl_cffi with Chrome TLS impersonation (Instagram fingerprints plain
requests), an async min-interval limiter, and 401/429 backoff. An optional INSTAGRAM_SESSIONID
cookie reduces 401s but is not required — without it more requests are blocked, which surfaces as
`HarvestError` and a `blocked` stats status upstream. Network deps are injected so the logic is
unit-testable without a live session.

`compute_metrics` is side-effect free and derives content metrics from the embedded recent posts of
a web_profile_info `user` object. `project_profile` flattens a raw `user` object into the stat shape
the stats CLI emits (raw counts + every derived metric + is_private).
"""

from __future__ import annotations

import asyncio
import os
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

WEB_PROFILE_INFO = "https://i.instagram.com/api/v1/users/web_profile_info/"
APP_ID = "936619743392459"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

ENV_SESSIONID = "INSTAGRAM_SESSIONID"


class ProfileNotFound(Exception):
    """The username does not resolve to an Instagram account (404 / null user)."""


class HarvestError(Exception):
    """Transient/blocked response (401/429/network) that survived all retries."""


def default_session():
    from curl_cffi.requests import AsyncSession

    return AsyncSession(impersonate="chrome")


class AsyncRateLimiter:
    """Min-interval limiter shared across concurrent fetches."""

    def __init__(self, min_interval: float, now: Callable[[], float] | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 jitter: Callable[[], float] = lambda: 1.0):
        self.min_interval = min_interval
        self._now = now or (lambda: asyncio.get_event_loop().time())
        self._sleep = sleep
        self._jitter = jitter
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._next_allowed - self._now()
            if delay > 0:
                await self._sleep(delay)
            self._next_allowed = self._now() + self.min_interval * self._jitter()


class Harvester:
    """Fetches and returns the raw web_profile_info `user` object for a username."""

    def __init__(self, session: Any = None, sessionid: str | None = None,
                 rate_limiter: AsyncRateLimiter | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 max_retries: int = 4, base_backoff: float = 2.0):
        self._session = session if session is not None else default_session()
        self._sessionid = sessionid if sessionid is not None else os.environ.get(ENV_SESSIONID)
        self._rate_limiter = rate_limiter
        self._sleep = sleep
        self._max_retries = max_retries
        self._base_backoff = base_backoff

    def _headers(self) -> dict[str, str]:
        return {
            "x-ig-app-id": APP_ID,
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip, deflate, br",
            "Accept": "*/*",
            "Referer": "https://www.instagram.com/",
        }

    def _cookies(self) -> dict[str, str] | None:
        return {"sessionid": self._sessionid} if self._sessionid else None

    async def fetch_profile(self, username: str) -> dict[str, Any]:
        for attempt in range(self._max_retries):
            if self._rate_limiter is not None:
                await self._rate_limiter.wait()
            resp = await self._session.get(
                WEB_PROFILE_INFO,
                params={"username": username},
                headers=self._headers(),
                cookies=self._cookies(),
            )
            status = resp.status_code
            if status == 200:
                try:
                    payload = resp.json()
                except Exception:
                    payload = None
                if payload is None:
                    await self._sleep(self._retry_delay(attempt, resp))
                    continue
                user = (payload.get("data") or {}).get("user")
                if not user:
                    raise ProfileNotFound(username)
                return user
            if status == 404:
                raise ProfileNotFound(username)
            if status in (401, 429):
                await self._sleep(self._retry_delay(attempt, resp))
                continue
            raise HarvestError(f"{username}: unexpected status {status}")
        raise HarvestError(f"{username}: throttled after {self._max_retries} attempts")

    def _retry_delay(self, attempt: int, resp: Any) -> float:
        retry_after = (getattr(resp, "headers", {}) or {}).get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except (TypeError, ValueError):
                pass
        return self._base_backoff * (2 ** attempt)


# --- metrics ----------------------------------------------------------------

_HASHTAG_RE = re.compile(r"#(\w+)")
_TOP_HASHTAGS = 10


def _post_nodes(user: dict) -> list[dict]:
    edges = user.get("edge_owner_to_timeline_media", {}).get("edges", []) or []
    return [e["node"] for e in edges if isinstance(e, dict) and "node" in e]


def _video_views(node: dict) -> int | None:
    if not node.get("is_video"):
        return None
    views = node.get("video_view_count")
    if views is None:
        views = node.get("video_play_count")
    return views


def _count(node: dict, edge: str) -> int | None:
    val = node.get(edge)
    return val.get("count") if isinstance(val, dict) else None


def _caption(node: dict) -> str:
    edges = node.get("edge_media_to_caption", {}).get("edges", []) or []
    if edges:
        return edges[0].get("node", {}).get("text", "") or ""
    return ""


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def compute_metrics(user: dict, window: int | None = None) -> dict[str, Any]:
    """Return derived metrics from the embedded recent posts of a web_profile_info user.

    ``window`` caps how many of the most-recent posts feed the view metrics (the ranking
    proxy); ``None`` uses all embedded posts (~12). Photos carry no view count, so view metrics
    cover video/reel posts only. Missing values are None."""
    posts = _post_nodes(user)
    followers = (user.get("edge_followed_by") or {}).get("count") or 0

    view_posts = posts[:window] if window else posts
    views = [v for v in (_video_views(n) for n in view_posts) if v is not None]
    likes = [c for c in (_count(n, "edge_media_preview_like") for n in posts) if c is not None]
    comments = [c for c in (_count(n, "edge_media_to_comment") for n in posts) if c is not None]
    timestamps = [n["taken_at_timestamp"] for n in posts if n.get("taken_at_timestamp")]
    num_videos = sum(1 for n in posts if n.get("is_video"))

    avg_likes = _mean(likes)
    avg_comments = _mean(comments)

    engagement_rate = None
    if followers and avg_likes is not None and avg_comments is not None:
        engagement_rate = round((avg_likes + avg_comments) / followers * 100, 3)

    cadence = None
    if len(timestamps) >= 2:
        span_days = (max(timestamps) - min(timestamps)) / 86400
        if span_days > 0:
            cadence = round(len(timestamps) / (span_days / 7), 2)

    last_post_date = None
    if timestamps:
        last_post_date = datetime.fromtimestamp(max(timestamps), tz=timezone.utc).date().isoformat()

    hashtags: Counter = Counter()
    for n in posts:
        for tag in _HASHTAG_RE.findall(_caption(n)):
            hashtags[tag.lower()] += 1
    top_hashtags = [t for t, _ in hashtags.most_common(_TOP_HASHTAGS)] or None

    return {
        "avg_views": round(_mean(views), 1) if views else None,
        "max_views": max(views) if views else None,
        "avg_likes": round(avg_likes) if avg_likes is not None else None,
        "avg_comments": round(avg_comments) if avg_comments is not None else None,
        "engagement_rate": engagement_rate,
        "posts_analyzed": len(posts),
        "reels_ratio": round(num_videos / len(posts), 3) if posts else None,
        "posting_cadence_per_week": cadence,
        "last_post_date": last_post_date,
        "top_hashtags": top_hashtags,
    }


def project_profile(user: dict, window: int | None = None) -> dict[str, Any]:
    """Flatten a raw web_profile_info `user` object into the full stat shape.

    Raw counts/flags straight off the user object, plus every derived `compute_metrics` field
    and `is_private`. `top_hashtags` stays a list here; the stats CLI joins it for the sheet cell."""
    m = compute_metrics(user, window)
    return {
        "username": user.get("username"),
        "full_name": user.get("full_name"),
        "followers": (user.get("edge_followed_by") or {}).get("count"),
        "following": (user.get("edge_follow") or {}).get("count"),
        "posts": (user.get("edge_owner_to_timeline_media") or {}).get("count"),
        "is_verified": bool(user.get("is_verified")),
        "is_business": bool(user.get("is_business_account")),
        "is_private": bool(user.get("is_private")),
        "external_url": user.get("external_url"),
        "biography": user.get("biography"),
        "avg_views": m["avg_views"],
        "max_views": m["max_views"],
        "avg_likes": m["avg_likes"],
        "avg_comments": m["avg_comments"],
        "engagement_rate": m["engagement_rate"],
        "posts_analyzed": m["posts_analyzed"],
        "reels_ratio": m["reels_ratio"],
        "posting_cadence_per_week": m["posting_cadence_per_week"],
        "last_post_date": m["last_post_date"],
        "top_hashtags": m["top_hashtags"],
    }
