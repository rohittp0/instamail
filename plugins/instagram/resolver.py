"""Best-effort email -> Instagram username resolution.

Instagram deliberately severs the email->username link, so this is inherently
low-yield. Resolvers run in order and short-circuit on the first hit:
  1. breach lookup (IntelX, only if an API key is configured)  -> confidence "high"
  2. search-engine dork (DuckDuckGo HTML, no key)              -> confidence "medium"
  3. username permutation from the email local part, gated by  -> confidence "low"
     a profile full-name match to reduce false positives.
The HTTP-bearing resolvers are injectable so the orchestration is unit-testable.
"""

import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from .harvester import HarvestError, ProfileNotFound, _default_session

log = logging.getLogger(__name__)

_HANDLE_RE = re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9._]+)")
_VALID_HANDLE = re.compile(r"^[a-z0-9._]{1,30}$")
_RESERVED = {
    "p", "reel", "reels", "explore", "stories", "tv", "accounts", "about",
    "developer", "legal", "directory", "web", "api", "graphql", "oauth", "emails",
    "session", "challenge", "privacy", "terms",
}
_SENTINEL = object()


@dataclass
class Resolution:
    username: str
    method: str       # "breach" | "dork" | "permutation"
    confidence: str   # "high" | "medium" | "low"


def extract_instagram_handle(text: str) -> str | None:
    """Return the first non-reserved instagram.com/<handle> found in text, lowercased."""
    for match in _HANDLE_RE.finditer(text or ""):
        handle = match.group(1).strip("/").lower()
        if handle and handle not in _RESERVED:
            return handle
    return None


def username_permutations(local_part: str) -> list[str]:
    """Candidate handles derived from an email local part (e.g. 'john.smith')."""
    local = local_part.lower()
    raw = [
        local,
        local.replace(".", "_"),
        local.replace(".", ""),
        re.sub(r"[^a-z0-9]", "", local),
    ]
    out: list[str] = []
    for cand in raw:
        if cand and _VALID_HANDLE.match(cand) and cand not in out:
            out.append(cand)
    return out


def _name_tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _name_matches(local_part: str, full_name: str | None) -> bool:
    return bool(_name_tokens(local_part) & _name_tokens(full_name or ""))


class Resolver:
    def __init__(self, harvester, cache=None, intelx_api_key: str | None = None,
                 enable_permutation: bool = True, session=None,
                 breach_lookup: Callable[[str], Awaitable[str | None]] | None = None,
                 dork_lookup: Callable[[str], Awaitable[str | None]] | None = None):
        self._harvester = harvester
        self._cache = cache
        self._intelx_api_key = intelx_api_key
        self._enable_permutation = enable_permutation
        self._session = session
        self._breach_lookup = breach_lookup or self._default_breach
        self._dork_lookup = dork_lookup or self._default_dork

    async def resolve(self, email: str) -> Resolution | None:
        if self._cache is not None:
            cached = self._cache.get(email, default=_SENTINEL)
            if cached is not _SENTINEL:
                return Resolution(**cached) if cached else None

        result = await self._run_chain(email)

        if self._cache is not None:
            self._cache.set(email, result.__dict__ if result else None)
        return result

    async def _safe(self, fn, email: str, label: str) -> str | None:
        """Run a resolver step, degrading any failure to a miss so the chain continues."""
        try:
            return await fn(email)
        except Exception as e:  # a flaky/misconfigured resolver must not abort the email
            log.debug("instagram resolver step %s failed for %s: %s", label, email, e)
            return None

    async def _run_chain(self, email: str) -> Resolution | None:
        local = email.split("@", 1)[0]

        if self._intelx_api_key:
            handle = await self._safe(self._breach_lookup, email, "breach")
            if handle:
                return Resolution(handle.lower(), "breach", "high")

        handle = await self._safe(self._dork_lookup, email, "dork")
        if handle:
            return Resolution(handle.lower(), "dork", "medium")

        if self._enable_permutation:
            for candidate in username_permutations(local):
                try:
                    profile = await self._harvester.fetch_profile(candidate)
                except ProfileNotFound:
                    continue
                except HarvestError:
                    continue  # transient block on a guess; skip rather than abort
                if _name_matches(local, profile.get("full_name")):
                    return Resolution(candidate, "permutation", "low")
        return None

    # --- live (HTTP) defaults; exercised end-to-end, not in unit tests --------

    async def _default_dork(self, email: str) -> str | None:
        session = self._session or _default_session()
        resp = await session.get(
            "https://html.duckduckgo.com/html/",
            params={"q": f'"{email}" site:instagram.com'},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        return extract_instagram_handle(getattr(resp, "text", "") or "")

    # IntelX free-tier API host. The commercial host (2.intelx.io) returns 401 with an
    # EMPTY body for free-tier keys, which is what produced the JSONDecodeError; the free
    # host accepts the same key and returns JSON.
    _INTELX_HOST = "https://free.intelx.io"

    async def _default_breach(self, email: str) -> str | None:
        import asyncio

        session = self._session or _default_session()
        headers = {"x-key": self._intelx_api_key}
        init = await session.post(
            f"{self._INTELX_HOST}/intelligent/search",
            json={"term": email, "maxresults": 50, "media": 0, "sort": 2, "terminate": []},
            headers=headers,
        )
        search_id = (init.json() or {}).get("id")
        if not search_id:
            return None
        # Results are async: status 0=more, 1=done, 2=invalid id, 3=not ready yet — poll briefly.
        for _ in range(4):
            result = await session.get(
                f"{self._INTELX_HOST}/intelligent/search/result",
                params={"id": search_id, "limit": 50},
                headers=headers,
            )
            data = result.json() or {}
            handle = extract_instagram_handle(result.text)
            if handle:
                return handle
            if data.get("status") in (1, 2):
                break
            await asyncio.sleep(1)
        return None
