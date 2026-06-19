"""The `instagram` plugin: discovers travel creators for the search terms and returns
creator records keyed on email.

Free providers run by default; OFFICIAL/PAID providers activate only when their credentials are
set in the environment, and paid ones run only as a last resort (see providers/registry.py).
The default free path scrapes public Instagram / search-engine endpoints, which violates
Instagram's ToS — surfaced here and logged at runtime.
"""

from __future__ import annotations

import inspect
import logging

from instamail.base import BasePlugin

from .config import TRAVEL_KEYWORDS, Tier
from .http import default_session
from .pipeline import ROW_FIELDS, Pipeline, is_travel
from .providers.registry import build_chains

log = logging.getLogger("instamail.instagram")

_TIER = {"free": Tier.FREE, "official": Tier.OFFICIAL, "paid": Tier.PAID}


class InstagramPlugin(BasePlugin):
    name = "instagram"
    key = "email"
    fields = list(ROW_FIELDS)

    def __init__(self, pipeline: Pipeline | None = None):
        # An injected pipeline (tests) bypasses env/credential wiring.
        self._pipeline = pipeline

    @classmethod
    def add_arguments(cls, group) -> None:
        group.add_argument("--limit", type=int, default=100,
                           help="max creators to emit (top N by --sort)")
        group.add_argument("--min-followers", type=int, default=None,
                           help="drop creators below this follower count")
        group.add_argument("--sort", choices=["avg_views", "max_views", "followers"],
                           default="avg_views", help="ranking metric (reel-reach proxy)")
        group.add_argument("--window", type=int, default=12,
                           help="recent reels used for the view metrics")
        group.add_argument("--max-tier", choices=["free", "official", "paid"], default="paid",
                           help="cap provider tier; 'free' = zero-cost dry run")
        group.add_argument("--travel-only", action="store_true",
                           help="keep only creators classified travel-relevant")
        group.add_argument("--require-email", action="store_true",
                           help="drop creators with no resolvable email")

    async def search(self, terms: str, opts) -> list[dict]:
        if self._pipeline is not None:
            return await self._pipeline.run(terms, opts)

        log.warning("instagram: free path scrapes public endpoints (Instagram ToS); "
                    "set provider credentials to use sanctioned/vendor sources")
        session = default_session()
        try:
            chains = build_chains(window=opts.window, max_tier=_TIER[opts.max_tier], session=session)
            pipeline = Pipeline(chains, classifier=lambda p: is_travel(p, TRAVEL_KEYWORDS))
            return await pipeline.run(terms, opts)
        finally:
            closer = getattr(session, "aclose", None) or getattr(session, "close", None)
            if closer:
                result = closer()
                if inspect.isawaitable(result):
                    await result
