#!/usr/bin/env python3
"""Direct (non-agentic) Instagram public-stats fetcher.

Reads ``{"usernames": [...]}`` on stdin and prints a JSON map
``{username: {<full stats>, "stats_status": ...}}`` on stdout. Fetches the public
``web_profile_info`` endpoint via the ported ``Harvester`` with Chrome TLS impersonation, a small
concurrency pool behind a shared min-interval rate limiter, and 401/429 backoff.

INSTAGRAM_SESSIONID is an optional booster: without it (or with an expired one) more requests are
blocked, surfacing as ``stats_status="blocked"`` and a single logged warning — the script still
runs. Status values:

  ok        - profile fetched, public, metrics computed
  private   - is_private account; follower/following/post counts kept, view metrics blank
  not_found - username does not resolve (404 / null user)
  blocked   - throttled/blocked after retries (common when no sessionid)
  error     - any other failure

Usage:
    echo '{"usernames":["natgeo"]}' | .venv/bin/python scripts/instagram_stats.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from dotenv import find_dotenv, load_dotenv

from ig_profile import (
    ENV_SESSIONID,
    AsyncRateLimiter,
    HarvestError,
    Harvester,
    ProfileNotFound,
    default_session,
    project_profile,
)

load_dotenv(find_dotenv(usecwd=True))

log = logging.getLogger("instamail.stats")

CONCURRENCY = 3
MIN_INTERVAL = 1.5  # seconds between fetches, shared across the pool

# Every key project_profile emits, so error/dead rows are shape-complete (all None).
_STAT_KEYS = (
    "username", "full_name", "followers", "following", "posts", "is_verified", "is_business",
    "is_private", "external_url", "biography", "avg_views", "max_views", "avg_likes",
    "avg_comments", "engagement_rate", "posts_analyzed", "reels_ratio",
    "posting_cadence_per_week", "last_post_date", "top_hashtags",
)


def _blank(status: str, username: str) -> dict:
    row = {k: None for k in _STAT_KEYS}
    row["username"] = username
    row["stats_status"] = status
    return row


async def _fetch_one(harvester: Harvester, sem: asyncio.Semaphore, username: str) -> dict:
    async with sem:
        try:
            user = await harvester.fetch_profile(username)
        except ProfileNotFound:
            return _blank("not_found", username)
        except HarvestError as exc:
            log.warning("stats blocked for %s: %s", username, exc)
            return _blank("blocked", username)
        except Exception as exc:  # noqa: BLE001 - any other failure is a soft miss
            log.warning("stats error for %s: %s", username, exc)
            return _blank("error", username)

    stats = project_profile(user)
    stats["stats_status"] = "private" if stats.get("is_private") else "ok"
    return stats


async def fetch_stats(usernames, session=None, sessionid=None) -> dict[str, dict]:
    """Fetch stats for an iterable of usernames; returns a {username: stats} map.

    Deduplicates while preserving order. ``session``/``sessionid`` are injectable for tests."""
    seen: list[str] = []
    for u in usernames:
        u = (u or "").strip().lstrip("@").lower()
        if u and u not in seen:
            seen.append(u)
    if not seen:
        return {}

    if sessionid is None:
        sessionid = os.environ.get(ENV_SESSIONID)
    if not sessionid:
        log.warning("%s not set; fetching without a session cookie (more 401s expected).",
                    ENV_SESSIONID)

    own_session = session is None
    session = session if session is not None else default_session()
    limiter = AsyncRateLimiter(MIN_INTERVAL)
    sem = asyncio.Semaphore(CONCURRENCY)
    harvester = Harvester(session=session, sessionid=sessionid, rate_limiter=limiter)
    try:
        rows = await asyncio.gather(*(_fetch_one(harvester, sem, u) for u in seen))
    finally:
        if own_session:
            close = getattr(session, "close", None)
            if close is not None:
                res = close()
                if asyncio.iscoroutine(res):
                    await res
    return {row["username"] or u: row for u, row in zip(seen, rows)}


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:  # noqa: BLE001
        print(f"instagram_stats: invalid JSON on stdin: {exc}", file=sys.stderr)
        return 2
    usernames = payload.get("usernames") if isinstance(payload, dict) else None
    if not isinstance(usernames, list):
        print('instagram_stats: expected {"usernames": [...]} on stdin', file=sys.stderr)
        return 2

    result = asyncio.run(fetch_stats(usernames))
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
