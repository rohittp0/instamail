"""Builds each layer's eligible, tier-sorted provider chain from the environment.

A provider is included only if its tier is within ``max_tier`` and all its required env vars are
present; chains are sorted FREE→OFFICIAL→PAID so the pipeline reaches paid providers last.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from ..cache import JsonCache
from ..config import CACHE_DIR, ENV_SESSIONID, PROFILE_TTL, Tier
from ..http import AsyncRateLimiter, Harvester, default_session
from .discovery import (DdgDiscovery, GoogleDiscovery, IgSearchDiscovery, MetaDiscovery,
                        VendorDiscovery)
from .email import HunterEmail, IgEmail, WebsiteEmail
from .enrichment import MetaEnrichment, PublicEnrichment, VendorEnrichment
from .verify import SyntaxVerifier


@dataclass
class Chains:
    discovery: list
    enrichment: list
    email: list
    verify: list


def _pick(providers, env: Mapping[str, str], max_tier: Tier) -> list:
    eligible = [p for p in providers if p.tier <= max_tier and p.available(env)]
    return sorted(eligible, key=lambda p: p.tier)


def build_chains(*, window: int | None = None, max_tier: Tier = Tier.PAID,
                 env: Mapping[str, str] | None = None, session=None,
                 use_cache: bool = True) -> Chains:
    env = os.environ if env is None else env
    session = session or default_session()

    sessionid = env.get(ENV_SESSIONID)
    interval = 5.0 if sessionid else 18.0  # anonymous access is throttled hard
    cache = JsonCache(CACHE_DIR, PROFILE_TTL) if use_cache else None
    harvester = Harvester(session=session, sessionid=sessionid, cache=cache,
                          rate_limiter=AsyncRateLimiter(interval))

    # Discovery preference among free providers: Google Custom Search (full-text, reliable) when
    # configured; else topsearch when a session cookie is present; DDG (block-prone) last.
    ddg = DdgDiscovery(session)
    igsearch = IgSearchDiscovery(session, sessionid)
    name_search = [igsearch, ddg] if sessionid else [ddg, igsearch]
    free_discovery = [GoogleDiscovery(session, env), *name_search]

    return Chains(
        discovery=_pick(
            [*free_discovery, MetaDiscovery(session, env), VendorDiscovery(session, env)],
            env, max_tier),
        enrichment=_pick(
            [PublicEnrichment(harvester, window),
             MetaEnrichment(session, env), VendorEnrichment(session, env)], env, max_tier),
        email=_pick(
            [IgEmail(), WebsiteEmail(session), HunterEmail(session, env)], env, max_tier),
        verify=_pick([SyntaxVerifier()], env, max_tier),
    )
