"""Contact-import resolver: email -> username via Instagram's `address_book/link/`.

This is the single highest-*recall* method. Instead of guessing a handle from the
email/name (which fails whenever the handle is unrelated — the common case), it uploads
the target email as a phone-book contact and reads back the account Instagram itself
matches. That is the only technique that can resolve an *unguessable* handle.

Requirements / caveats (why it is gated and best-effort):
  - Needs an authenticated `INSTAGRAM_SESSIONID` (the numeric account id is the prefix
    of the cookie) and, in practice, a residential/mobile IP — datacenter IPs get
    403/`login_required` even with a valid session (verified during development).
  - Only finds accounts that allow discovery by email ("Settings -> ... -> let people
    find you by email"), which is the default for most non-private accounts.
  - The syncing account uploads a contact, so use a throwaway/OSINT account.

Modeled on `instagrapi.address_book_link`: POST form body
`signed_body=SIGNATURE.<urlencoded json>` (the HMAC is not verified) carrying the
`contacts` array plus device identifiers, with the mobile header set and session cookie.
Network is injected so payload-building and response-parsing are unit-testable; the live
HTTP path is exercised end-to-end (like the harvester), not in unit tests.
"""

import json
import logging
import os
import uuid
from typing import Any
from urllib.parse import quote_plus, unquote

from .harvester import APP_ID

log = logging.getLogger(__name__)

LINK_URL = "https://i.instagram.com/api/v1/address_book/link/"
_USER_AGENT = (
    "Instagram 269.0.0.18.75 Android (28/9; 420dpi; 1080x2131; Xiaomi; "
    "Redmi Note 7; lavender; qcom; en_US; 314665256)"
)


def account_id_from_sessionid(sessionid: str | None) -> str | None:
    """Instagram's `sessionid` cookie is `<numeric_user_id>%3A...`; pull the id."""
    if not sessionid:
        return None
    head = unquote(sessionid).split(":", 1)[0].strip()
    return head if head.isdigit() else None


def extract_matches(payload: Any) -> list[dict[str, Any]]:
    """Pull the *direct* contact matches (username/full_name/pk) from a link response.

    Only the `users` array holds accounts Instagram matched to an uploaded contact;
    other keys (e.g. people-you-may-know suggestions) are intentionally ignored to
    avoid false positives. Defensive about minor shape differences across app versions."""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("users")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        user = entry.get("user") if isinstance(entry, dict) and isinstance(entry.get("user"), dict) else entry
        if isinstance(user, dict) and user.get("username"):
            out.append({"username": str(user["username"]).lower(),
                        "full_name": user.get("full_name"),
                        "pk": user.get("pk") or user.get("id")})
    return out


class ContactImporter:
    """Email -> matched Instagram accounts via address_book/link (gated on a session)."""

    def __init__(self, session: Any = None, sessionid: str | None = None,
                 device_seed: str | None = None):
        self._explicit_session = session
        self._session = session
        self._sessionid = sessionid if sessionid is not None else os.environ.get("INSTAGRAM_SESSIONID")
        self._uid = account_id_from_sessionid(self._sessionid)
        seed = device_seed or (self._sessionid or "anon")
        # Stable per-account device identifiers (Instagram dislikes churn).
        self._uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, "uuid:" + seed))
        self._phone_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, "phone:" + seed))
        self._device_id = "android-" + uuid.uuid5(uuid.NAMESPACE_DNS, "dev:" + seed).hex[:16]

    @property
    def enabled(self) -> bool:
        """Contact-import only works authenticated, so skip entirely without a session."""
        return bool(self._sessionid and self._uid)

    def _ensure_session(self) -> Any:
        if self._session is None:
            from .harvester import _default_session
            self._session = _default_session()
        return self._session

    def _contacts_payload(self, email: str) -> dict[str, str]:
        contacts = [{
            "first_name": email.split("@", 1)[0],
            "last_name": "",
            "phone_numbers": [],
            "email_addresses": [email],
        }]
        return {
            "contacts": json.dumps(contacts, separators=(",", ":")),
            "phone_id": self._phone_id,
            "device_id": self._device_id,
            "module": "find_friends_contacts",
            "source": "user_setting",
            "_uuid": self._uuid,
            "_uid": self._uid or "",
        }

    @staticmethod
    def _encode(data: dict[str, str]) -> str:
        return "signed_body=SIGNATURE." + quote_plus(json.dumps(data, separators=(",", ":")))

    def _headers(self, body: str) -> dict[str, str]:
        return {
            "User-Agent": _USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-IG-App-ID": APP_ID,
            "X-IG-Device-ID": self._uuid,
            "X-IG-Android-ID": self._device_id,
            "X-IG-Capabilities": "3brTv10=",
            "X-IG-Connection-Type": "WIFI",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "en-US",
            "Content-Length": str(len(body)),
        }

    def _cookies(self) -> dict[str, str]:
        return {"sessionid": self._sessionid, "ds_user_id": self._uid or ""}

    async def find_by_email(self, email: str) -> list[dict[str, Any]]:
        """Return direct contact matches for `email`, or [] when disabled/no match/blocked."""
        if not self.enabled:
            return []
        body = self._encode(self._contacts_payload(email))
        session = self._ensure_session()
        try:
            resp = await session.post(LINK_URL, data=body,
                                      params={"include": "extra_display_name,thumbnails"},
                                      headers=self._headers(body), cookies=self._cookies())
        except Exception as e:
            log.debug("contact-import request failed for %s: %s", email, e)
            return []
        if getattr(resp, "status_code", None) != 200:
            log.debug("contact-import %s -> HTTP %s", email, getattr(resp, "status_code", "?"))
            return []
        try:
            payload = resp.json()
        except Exception:
            return []
        matches = extract_matches(payload)
        if not matches:
            log.debug("contact-import %s -> no direct matches (keys=%s)",
                      email, list(payload.keys()) if isinstance(payload, dict) else type(payload))
        return matches
