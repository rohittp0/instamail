"""Shared HTTP engine for the free public path.

Uses curl_cffi with Chrome TLS impersonation (Instagram fingerprints plain requests). Paces
requests with an async min-interval limiter and backs off on 401/429. An optional
INSTAGRAM_SESSIONID cookie markedly reduces 401s. Network deps are injected so logic is testable.
"""

import asyncio
import os
import re
from typing import Any, Awaitable, Callable

WEB_PROFILE_INFO = "https://i.instagram.com/api/v1/users/web_profile_info/"
APP_ID = "936619743392459"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_HANDLE_RE = re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9._]+)")
_AT_RE = re.compile(r"@([A-Za-z0-9._]{1,30})")
_RESERVED = {
    "p", "reel", "reels", "explore", "stories", "tv", "accounts", "about", "developer",
    "legal", "directory", "web", "api", "graphql", "oauth", "emails", "session",
    "challenge", "privacy", "terms",
}


class ProfileNotFound(Exception):
    """The username does not resolve to an Instagram account (404 / null user)."""


class HarvestError(Exception):
    """Transient/blocked response (401/429/network) that survived all retries."""


def extract_handles(text: str) -> list[str]:
    """Return all distinct non-reserved instagram.com/<handle> values in text, lowercased,
    preserving first-seen order."""
    out: list[str] = []
    for match in _HANDLE_RE.finditer(text or ""):
        handle = match.group(1).strip("/").lower()
        if handle and handle not in _RESERVED and handle not in out:
            out.append(handle)
    return out


def extract_at_mentions(text: str) -> list[str]:
    """Return distinct @handles in text, lowercased (e.g. from a 'Name (@handle)' title).
    Apply to titles, not snippets — snippet @mentions are usually tagged *other* accounts."""
    out: list[str] = []
    for match in _AT_RE.finditer(text or ""):
        handle = match.group(1).strip(".").lower()
        if handle and handle not in _RESERVED and handle not in out:
            out.append(handle)
    return out


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

    def __init__(self, session: Any = None, sessionid: str | None = None, cache=None,
                 rate_limiter: AsyncRateLimiter | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 max_retries: int = 4, base_backoff: float = 2.0):
        self._session = session if session is not None else default_session()
        self._sessionid = sessionid if sessionid is not None else os.environ.get("INSTAGRAM_SESSIONID")
        self._cache = cache
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
        if self._cache is not None:
            cached = self._cache.get(username, default=None)
            if cached is not None:
                return cached

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
                if self._cache is not None:
                    self._cache.set(username, user)
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
