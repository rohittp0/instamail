#!/usr/bin/env python3
"""Client for the internal users API — the input source for the workflow.

  GET {INTERNAL_API_BASE}?limit=N[&email=<cursor>]   header: X-Internal-Key: <key>
  -> {"users": [{"email", "first_name", "last_name"}, ...]}

Pagination is cursor-based and *positional* (not email-sorted): pass the last email of a page as
`email=` to get the users after it. `fetch_users` follows pages internally (the API caps a page at
~100) until `limit` users are collected or the list is exhausted, and joins first/last into `name`.

Transient failures (5xx / connection errors — e.g. during a deploy) are retried with backoff so a
brief server blip doesn't abort a claim.

Env: INTERNAL_API_KEY (required), INTERNAL_API_BASE (optional, defaults to the dashboard endpoint).

Usage:
    .venv/bin/python scripts/users_api.py [--after EMAIL] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

ENV_API_KEY = "INTERNAL_API_KEY"
ENV_API_BASE = "INTERNAL_API_BASE"
DEFAULT_BASE = "https://dashboard.lascade.com/auth/v1/internal/users/after/"

PAGE_SIZE = 100          # the API's per-request cap
MAX_RETRIES = 5
BASE_BACKOFF = 1.5       # seconds; exponential


def _name(user: dict) -> str:
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    return f"{first} {last}".strip()


def _get_page(session, base: str, key: str, after: str | None, limit: int,
              sleep=time.sleep) -> list[dict]:
    """One API request with retry/backoff on transient errors. Returns the raw users list."""
    params = {"limit": limit}
    if after:
        params["email"] = after
    headers = {"X-Internal-Key": key}

    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(base, params=params, headers=headers, timeout=30)
        except Exception as exc:  # noqa: BLE001 - connection/DNS/timeout: retry
            last_exc = exc
            sleep(BASE_BACKOFF * (2 ** attempt))
            continue
        status = getattr(resp, "status_code", None)
        if status == 200:
            try:
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                raise SystemExit(f"users_api: 200 but non-JSON body: {exc}")
            users = payload.get("users") if isinstance(payload, dict) else None
            if users is None:
                raise SystemExit("users_api: response missing 'users' key")
            return users
        if status in (429,) or (isinstance(status, int) and 500 <= status < 600):
            last_exc = RuntimeError(f"transient status {status}")
            sleep(BASE_BACKOFF * (2 ** attempt))
            continue
        # 4xx (auth/bad request) is not retryable
        body = (getattr(resp, "text", "") or "")[:200]
        raise SystemExit(f"users_api: HTTP {status} from API: {body}")
    raise SystemExit(f"users_api: giving up after {MAX_RETRIES} attempts ({last_exc})")


def fetch_users(after: str | None = None, limit: int = 100, session=None,
                sleep=time.sleep) -> list[dict]:
    """Return up to ``limit`` users after the ``after`` cursor as [{email, name}], in API order.

    Follows pages (PAGE_SIZE each) until ``limit`` is reached or the API runs out. ``session`` is
    injectable for tests; defaults to a real requests.Session."""
    key = os.environ.get(ENV_API_KEY)
    if not key:
        raise SystemExit(f"users_api: {ENV_API_KEY} is not set")
    base = os.environ.get(ENV_API_BASE) or DEFAULT_BASE

    own_session = session is None
    if own_session:
        import requests
        session = requests.Session()

    out: list[dict] = []
    cursor = after
    try:
        while len(out) < limit:
            want = min(PAGE_SIZE, limit - len(out))
            page = _get_page(session, base, key, cursor, want, sleep=sleep)
            if not page:
                break
            for u in page:
                email = (u.get("email") or "").strip().lower()
                if email:
                    out.append({"email": email, "name": _name(u)})
            cursor = (page[-1].get("email") or "").strip().lower()
            if len(page) < want:   # API returned a short page -> exhausted
                break
    finally:
        if own_session:
            session.close()
    return out[:limit]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Fetch a slice of users from the internal API.")
    p.add_argument("--after", default=None, help="cursor: last email from the previous page")
    p.add_argument("--limit", type=int, default=100, help="max users to return")
    args = p.parse_args(argv)

    users = fetch_users(after=args.after, limit=args.limit)
    json.dump({"users": users, "count": len(users)}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
