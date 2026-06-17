"""Email -> Instagram username resolution.

Instagram deliberately severs the email->username link, so resolution is a layered,
best-effort pipeline. Steps run in order and short-circuit on the first *confident* hit;
each step degrades to a miss (never an abort) if its network dependency is unavailable:

  1. contact-import (`address_book/link`, needs a session)  -> "contact_import" / high
        Instagram matches the uploaded email to an account directly. The only step that
        can find a handle unrelated to the email/name.
  2. candidate generation + verification                    -> "lookup_verified" / high
        Generate handles (search-engine dork + email-local permutations), then ask
        Instagram (`users/lookup`) for each existing candidate's obfuscated recovery
        email and check it against the target. A match confirms; a *mismatch refutes*
        and the candidate is dropped — this is what rejects same-named strangers.
  3. degraded fallbacks (when lookup is unavailable/blocked):
        - a dork hit that exists                             -> "dork" / medium
        - a permutation whose profile name overlaps the local part -> "permutation" / low

The old resolver was steps 2-fallback only (dork -> medium, permutation+name -> low).
That both *missed* accounts (permutations never dropped a trailing digit; a name token
had to overlap) and *accepted false positives* (any same-named account passed). Steps 1
and the lookup verification fix both failure modes. The network deps are injected so the
orchestration is unit-testable.
"""

import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from .harvester import HarvestError, ProfileNotFound, _default_session
from .lookup import obfuscated_email_matches

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
    method: str       # "contact_import" | "lookup_verified" | "dork" | "permutation"
    confidence: str   # "high" | "medium" | "low"
    obfuscated_email: str | None = None   # Instagram's masked recovery email, if seen
    obfuscated_phone: str | None = None   # Instagram's masked recovery phone, if seen


def extract_instagram_handle(text: str) -> str | None:
    """Return the first non-reserved instagram.com/<handle> found in text, lowercased."""
    for match in _HANDLE_RE.finditer(text or ""):
        handle = match.group(1).strip("/").lower()
        if handle and handle not in _RESERVED:
            return handle
    return None


def username_permutations(local_part: str) -> list[str]:
    """Candidate handles derived from an email local part (e.g. 'john.smith', 'tprohit9').

    Beyond the literal/separator variants, this drops `+tag` sub-addressing and a trailing
    digit run (`tprohit9` -> `tprohit`) and a letters-only form — common when a handle was
    taken so the user appended numbers in the email but not the handle. Verification
    (step 2) keeps the extra candidates from causing false positives."""
    local = local_part.lower().split("+", 1)[0]  # drop +tag sub-addressing
    no_trailing_digits = re.sub(r"\d+$", "", local)
    raw = [
        local,
        local.replace(".", "_"),
        local.replace(".", ""),
        re.sub(r"[^a-z0-9]", "", local),
        no_trailing_digits,
        re.sub(r"[^a-z0-9]", "", no_trailing_digits),
        re.sub(r"[^a-z]", "", local),
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
    def __init__(self, harvester, cache=None, enable_permutation: bool = True, session=None,
                 dork_lookup: Callable[[str], Awaitable[str | None]] | None = None,
                 lookup=None, contacts=None):
        self._harvester = harvester
        self._cache = cache
        self._enable_permutation = enable_permutation
        self._session = session
        self._dork_lookup = dork_lookup or self._default_dork
        self._lookup = lookup        # InstagramLookup | None  (obfuscation verification)
        self._contacts = contacts    # ContactImporter   | None  (direct match)

    async def resolve(self, email: str) -> Resolution | None:
        if self._cache is not None:
            cached = self._cache.get(email, default=_SENTINEL)
            if cached is not _SENTINEL:
                return Resolution(**cached) if cached else None

        result = await self._run_chain(email)

        if self._cache is not None:
            self._cache.set(email, result.__dict__ if result else None)
        return result

    async def _safe(self, fn, email: str, label: str):
        """Run a resolver step, degrading any failure to None so the chain continues."""
        try:
            return await fn(email)
        except Exception as e:  # a flaky/misconfigured resolver must not abort the email
            log.debug("instagram resolver step %s failed for %s: %s", label, email, e)
            return None

    async def _lookup_hints(self, query: str) -> dict | None:
        """Best-effort users/lookup; None when no client, a miss, or a throttle."""
        if self._lookup is None:
            return None
        return await self._safe(self._lookup.obfuscation_for, query, "lookup")

    def _mk(self, username: str, method: str, confidence: str, hints: dict | None) -> Resolution:
        hints = hints or {}
        return Resolution(username.lower(), method, confidence,
                          obfuscated_email=hints.get("obfuscated_email"),
                          obfuscated_phone=hints.get("obfuscated_phone"))

    async def _run_chain(self, email: str) -> Resolution | None:
        local = email.split("@", 1)[0]

        # 1) Contact-import: a direct, Instagram-confirmed match (handle may be unguessable).
        if self._contacts is not None:
            for match in await self._safe(lambda _e: self._contacts.find_by_email(_e), email, "contacts") or []:
                username = (match.get("username") or "").lower()
                if username:
                    return self._mk(username, "contact_import", "high", await self._lookup_hints(username))

        # 2) Candidate generation: dork hit first, then email-local permutations.
        candidates: list[tuple[str, str]] = []
        handle = await self._safe(self._dork_lookup, email, "dork")
        if handle:
            candidates.append((handle.lower(), "dork"))
        if self._enable_permutation:
            candidates.extend((p, "permutation") for p in username_permutations(local))

        fallback: Resolution | None = None  # best non-verified hit (medium dork > low name)
        seen: set[str] = set()
        for cand, source in candidates:
            if cand in seen:
                continue
            seen.add(cand)
            try:
                profile = await self._harvester.fetch_profile(cand)
            except ProfileNotFound:
                continue
            except HarvestError:
                continue  # transient block on a guess; skip rather than abort

            hints = await self._lookup_hints(cand)
            obf_email = (hints or {}).get("obfuscated_email")
            if obf_email is not None:
                if obfuscated_email_matches(obf_email, email):
                    return self._mk(cand, "lookup_verified", "high", hints)
                continue  # Instagram says this account's email is NOT the target -> refuted

            # 3) No lookup signal -> degrade to source-based confidence.
            if source == "dork":
                if fallback is None or fallback.confidence == "low":
                    fallback = self._mk(cand, "dork", "medium", hints)
            elif _name_matches(local, profile.get("full_name")):
                if fallback is None:
                    fallback = self._mk(cand, "permutation", "low", hints)
        return fallback

    # --- live (HTTP) defaults; exercised end-to-end, not in unit tests --------

    async def _default_dork(self, email: str) -> str | None:
        session = self._session or _default_session()
        resp = await session.get(
            "https://html.duckduckgo.com/html/",
            params={"q": f'"{email}" site:instagram.com'},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        return extract_instagram_handle(getattr(resp, "text", "") or "")
