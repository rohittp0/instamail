"""Instagram `users/lookup` primitive: account existence + obfuscated recovery hints.

POST https://i.instagram.com/api/v1/users/lookup/ with a body of the literal form
`signed_body=SIGNATURE.<urlencoded {"q": <email|username>, "skip_recovery": "1"}>`.
Instagram no longer verifies the HMAC for this endpoint, so the literal string
"SIGNATURE" is accepted (this is exactly what the maintained `toutatis` tool does).
`skip_recovery=1` means the call only *reads* the obfuscated contact points — it does
not send a recovery email/SMS.

The response carries `obfuscated_email` / `obfuscated_phone` for the matched account
(e.g. `t******9@gmail.com`, `+91 ***-***-**09`), `{"message": "No users found"}` on a
clean miss, or `{"status": "fail"}` when throttled. It works anonymously but is
aggressively IP-throttled (429 from datacenter IPs); a residential/mobile IP and/or an
`INSTAGRAM_SESSIONID` make it reliable.

Why this matters: it is the precision backbone of resolution. Given a *candidate*
username we ask Instagram for that account's obfuscated recovery email and check it
against the target email (first char + last char of the local part + exact domain).
That turns a weak "the handle looks like the name" guess into a match Instagram itself
confirms — and, just as importantly, lets us *refute* a same-named stranger whose
recovery email does not match (the false-positive the old name-only heuristic accepted).
The network deps are injected so the logic is unit-testable.
"""

import asyncio
import json
import os
from typing import Any, Awaitable, Callable
from urllib.parse import quote_plus

from .harvester import APP_ID

LOOKUP_URL = "https://i.instagram.com/api/v1/users/lookup/"
_USER_AGENT = "Instagram 101.0.0.15.120 Android"


class LookupBlocked(Exception):
    """Transient/throttled response (429 / non-JSON / status=fail) that survived retries."""


def obfuscated_email_matches(obf_email: str | None, email: str) -> bool:
    """True when Instagram's masked recovery email is consistent with `email`.

    Instagram masks the local part as first-char + stars + last-char and leaves the
    domain intact (e.g. `t******9@gmail.com`). We require the domain to match exactly
    and the first and last visible characters of the local part to match. This is
    deliberately strict on the domain (cheap, high-signal) and tolerant of the mask
    width (which Instagram does not keep proportional to the real length)."""
    if not obf_email or "@" not in obf_email or "@" not in email:
        return False
    obf_local, _, obf_domain = obf_email.strip().lower().rpartition("@")
    local, _, domain = email.strip().lower().rpartition("@")
    if not obf_local or not local or obf_domain != domain:
        return False
    if obf_local[0] != local[0]:
        return False
    # The char immediately before '@' in the mask is the real last char of the local
    # part — but guard the rare short-local case where it is itself masked.
    last_visible = obf_local[-1]
    if last_visible == "*":
        return False
    return last_visible == local[-1]


class InstagramLookup:
    """Thin async client over users/lookup with harvester-style retry/backoff.

    `lookup(q)` returns the parsed JSON dict on a hit (carrying obfuscated_email /
    obfuscated_phone), None on a clean "No users found" miss, and raises LookupBlocked
    when the endpoint is throttled past all retries. Resolution treats LookupBlocked as
    "no information" (degrade), never as a refutation."""

    def __init__(self, session: Any = None, sessionid: str | None = None,
                 rate_limiter: Any = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 max_retries: int = 3, base_backoff: float = 2.0):
        if session is None:
            from .harvester import _default_session
            session = _default_session()
        self._session = session
        self._sessionid = sessionid if sessionid is not None else os.environ.get("INSTAGRAM_SESSIONID")
        self._rate_limiter = rate_limiter
        self._sleep = sleep
        self._max_retries = max_retries
        self._base_backoff = base_backoff

    def _headers(self, body: str) -> dict[str, str]:
        return {
            "Accept-Language": "en-US",
            "User-Agent": _USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-IG-App-ID": APP_ID,
            "Accept-Encoding": "gzip, deflate",
            "Content-Length": str(len(body)),
        }

    def _cookies(self) -> dict[str, str] | None:
        return {"sessionid": self._sessionid} if self._sessionid else None

    @staticmethod
    def _body(q: str) -> str:
        return "signed_body=SIGNATURE." + quote_plus(
            json.dumps({"q": q, "skip_recovery": "1"}, separators=(",", ":")))

    async def lookup(self, q: str) -> dict[str, Any] | None:
        body = self._body(q)
        for attempt in range(self._max_retries):
            if self._rate_limiter is not None:
                await self._rate_limiter.wait()
            resp = await self._session.post(
                LOOKUP_URL, data=body, headers=self._headers(body), cookies=self._cookies())
            status = getattr(resp, "status_code", None)
            payload: Any = None
            try:
                payload = resp.json()
            except Exception:
                payload = None

            if status == 200 and isinstance(payload, dict):
                if "obfuscated_email" in payload or "obfuscated_phone" in payload:
                    return payload
                if payload.get("message") == "No users found":
                    return None
                # status=fail / empty: throttle or soft block -> retry
            if status == 404:
                return None
            await self._sleep(self._retry_delay(attempt, resp))
        raise LookupBlocked(f"users/lookup throttled after {self._max_retries} attempts (q={q!r})")

    async def obfuscation_for(self, q: str) -> dict[str, Any] | None:
        """Best-effort wrapper: returns the lookup dict or None, swallowing blocks.

        Used by the resolver, where a throttled lookup must degrade to "no info"
        rather than abort the candidate."""
        try:
            return await self.lookup(q)
        except LookupBlocked:
            return None

    def _retry_delay(self, attempt: int, resp: Any) -> float:
        retry_after = (getattr(resp, "headers", {}) or {}).get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except (TypeError, ValueError):
                pass
        return self._base_backoff * (2 ** attempt)
