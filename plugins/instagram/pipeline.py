"""The layered orchestration: discover → enrich → classify → email → verify → dedup → rank.

Each layer walks its provider chain and takes the FIRST success (chains are FREE→OFFICIAL→PAID,
so paid providers run only as a last resort). Always returns rows whose keys are exactly the
plugin's ``key`` + ``fields``, filling None wherever a layer produced nothing.
"""

from __future__ import annotations

import asyncio

ROW_FIELDS = (
    "instagram_id", "username", "full_name", "followers", "following", "posts",
    "avg_views", "max_views", "is_business", "is_verified", "external_url", "biography",
    "discovery_source", "email_source", "email_confidence",
)


def is_travel(profile: dict, keywords) -> bool:
    hay = " ".join(filter(None, [
        profile.get("biography") or "",
        profile.get("full_name") or "",
        " ".join(profile.get("top_hashtags") or []),
    ])).lower()
    return any(k in hay for k in keywords)


class Pipeline:
    def __init__(self, chains, *, concurrency: int = 3, overfetch: int = 3, classifier=None):
        self.chains = chains
        self.concurrency = concurrency
        self.overfetch = overfetch
        self.classifier = classifier

    async def run(self, terms: str, opts) -> list[dict]:
        limit = opts.limit
        handles, source = await self._discover(terms, max(limit * self.overfetch, limit))
        handles = _dedup_lower(handles)

        profiles = await self._enrich_all(handles, source)
        profiles = _dedup_by_id(profiles)

        if getattr(opts, "travel_only", False) and self.classifier:
            profiles = [p for p in profiles if self.classifier(p)]
        if opts.min_followers:
            profiles = [p for p in profiles if (p.get("followers") or 0) >= opts.min_followers]

        await self._resolve_emails(profiles)
        if getattr(opts, "require_email", False):
            profiles = [p for p in profiles if p.get("email")]

        profiles = _rank(profiles, opts.sort)[:limit]
        return [_row(p) for p in profiles]

    async def _discover(self, terms: str, n: int) -> tuple[list[str], str | None]:
        for prov in self.chains.discovery:
            try:
                handles = await prov.discover(terms, n)
            except Exception:
                handles = []
            if handles:
                return handles, prov.name
        return [], None

    async def _enrich_all(self, handles: list[str], source: str | None) -> list[dict]:
        sem = asyncio.Semaphore(self.concurrency)

        async def one(handle: str) -> dict | None:
            async with sem:
                for prov in self.chains.enrichment:
                    try:
                        profile = await prov.enrich(handle)
                    except Exception:
                        profile = None
                    if profile:
                        profile["discovery_source"] = source
                        return profile
                return None

        results = await asyncio.gather(*(one(h) for h in handles))
        return [r for r in results if r]

    async def _resolve_emails(self, profiles: list[dict]) -> None:
        sem = asyncio.Semaphore(self.concurrency)

        async def one(profile: dict) -> None:
            async with sem:
                email, source = None, None
                for prov in self.chains.email:
                    try:
                        email = await prov.find(profile)
                    except Exception:
                        email = None
                    if email:
                        source = prov.name
                        break
                email = email.strip().lower() if email else None
                profile["email"] = email
                profile["email_source"] = source
                if email and self.chains.verify:
                    try:
                        profile["email_confidence"] = await self.chains.verify[0].verify(email)
                    except Exception:
                        profile["email_confidence"] = "unknown"
                else:
                    profile["email_confidence"] = None

        await asyncio.gather(*(one(p) for p in profiles))


def _dedup_lower(handles: list[str]) -> list[str]:
    seen, out = set(), []
    for h in handles:
        k = h.lower()
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _dedup_by_id(profiles: list[dict]) -> list[dict]:
    seen, out = set(), []
    for p in profiles:
        iid = p.get("instagram_id")
        if iid is not None and iid in seen:
            continue
        if iid is not None:
            seen.add(iid)
        out.append(p)
    return out


def _rank(profiles: list[dict], sort: str) -> list[dict]:
    def key(p):
        v = p.get(sort)
        return (v is not None, v if v is not None else 0)

    return sorted(profiles, key=key, reverse=True)


def _row(profile: dict) -> dict:
    row = {"email": profile.get("email")}
    for f in ROW_FIELDS:
        row[f] = profile.get(f)
    return row
