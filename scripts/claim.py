#!/usr/bin/env python3
"""Atomically claim the next batch of users — parallel-safe, with crash recovery.

Concurrent workflow runs would otherwise read the same cursor and process the same users. Under a
`state`-tab lease-lock, each claim either:

  * RECOVERS — reclaims a batch from the `claims` ledger that is still `in_progress` but whose lease
    has expired (its claimer died before persisting), re-fetching that exact range; or
  * TAKES NEW WORK — reads the cursor, fetches the next BATCH users after it, advances the cursor,
    and records a new `in_progress` claim row.

The lock is held only for this fast operation, never during OSINT/stats. The claim row is marked
`done` by persist.py after the batch is written, giving at-least-once processing (no dropped users);
persist also dedups output by email, so a reclaim race can't duplicate rows.

argv  : claim.py [BATCH_SIZE]   (default 100)
stdout: {"users": [{"email","name"}], "exhausted": bool, "claim_id": str|null,
         "claim_row": int|null, "reclaimed": bool}

Usage:
    .venv/bin/python scripts/claim.py 10
"""

from __future__ import annotations

import json
import sys
import time
import uuid

import users_api
from sheets_io import (
    acquire_lock,
    append_claim,
    claims_worksheet,
    find_reclaimable,
    get_cursor,
    open_spreadsheet,
    reclaim_row,
    release_lock,
    set_cursor,
    state_worksheet,
)

# How long a claimed batch may run before it's considered abandoned and reclaimable. Must comfortably
# exceed the time to OSINT + persist one batch; a generous value avoids reclaiming a slow-but-alive run.
CLAIM_LEASE_SECONDS = 1800


def _trim_to_end(users: list[dict], cursor_end: str) -> list[dict]:
    """Cut a re-fetched page at the original range boundary (defensive vs a larger reclaim limit)."""
    if not cursor_end:
        return users
    out = []
    for u in users:
        out.append(u)
        if u.get("email") == cursor_end:
            break
    return out


def claim(state_ws, claims_ws, fetch, limit: int, token: str,
          now=time.time, lease: float = CLAIM_LEASE_SECONDS, lock: bool = True) -> dict:
    """Claim a batch (recover an expired one, else take new work). Deps injectable for tests.

    ``fetch`` is ``(after, limit) -> list[{email, name}]``. Everything that touches the cursor /
    ledger happens inside the lock, so concurrent claimers get disjoint slices."""
    if lock:
        acquire_lock(state_ws, token)
    try:
        # 1. Recovery: take over a dead claimer's batch before handing out anything new.
        rec = find_reclaimable(claims_ws, now())
        if rec:
            row, claim_id, cursor_start, cursor_end = rec
            users = _trim_to_end(fetch(after=cursor_start or None, limit=limit), cursor_end)
            reclaim_row(claims_ws, row, token, now() + lease, now())
            return {"users": users, "exhausted": False, "claim_id": claim_id,
                    "claim_row": row, "reclaimed": True}

        # 2. New work: next slice after the cursor.
        cursor = get_cursor(state_ws) or None
        users = fetch(after=cursor, limit=limit)
        if not users:
            return {"users": [], "exhausted": True, "claim_id": None,
                    "claim_row": None, "reclaimed": False}
        new_cursor = users[-1]["email"]
        set_cursor(state_ws, new_cursor)
        claim_id = uuid.uuid4().hex
        row = append_claim(claims_ws, claim_id, cursor or "", new_cursor, token,
                           now() + lease, now())
        return {"users": users, "exhausted": len(users) < limit, "claim_id": claim_id,
                "claim_row": row, "reclaimed": False}
    finally:
        if lock:
            release_lock(state_ws, token)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    limit = 100
    if argv:
        try:
            limit = int(argv[0])
        except ValueError:
            print("claim: BATCH_SIZE must be an integer", file=sys.stderr)
            return 2

    spreadsheet = open_spreadsheet()
    state_ws = state_worksheet(spreadsheet)
    claims_ws = claims_worksheet(spreadsheet)
    token = uuid.uuid4().hex
    result = claim(state_ws, claims_ws, users_api.fetch_users, limit, token)

    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
