#!/usr/bin/env python3
"""Google Sheets gateway for the email->instagram workflow.

This is the single point that touches the spreadsheet; the JS Workflow's agents shell out to it.
Three tabs:

  output - one row per processed user (header in row 1; the workflow appends below)
  state  - a tiny key/value tab holding the API pagination cursor + a lease-lock used to make
           concurrent runs claim disjoint slices of users (see claim.py):
              A1 "cursor"        B1 <last processed email, or empty>
              A2 "lock_token"    B2 <holder token, or empty>
              A3 "lock_expires"  B3 <epoch seconds the lease expires, or empty>
  claims - ledger of claimed batches for at-least-once recovery: a batch claimed but never
           persisted (its claimer died) is reclaimed once its lease expires (see claim.py).

Input comes from the internal users API (users_api.py), not the sheet. Resume is the `state`
cursor: re-running continues API pagination after the last claimed email.

Auth: GOOGLE_SERVICE_ACCOUNT_JSON holds the base64-encoded service-account key JSON (raw JSON is
also accepted). Share the spreadsheet with the service-account email once.

Subcommands:
  ensure-sheets   create the output + state tabs and the output header (idempotent)
  append-output   read {"rows": [[...25 cells...], ...]} on stdin and append to output

Lock/cursor are used programmatically by claim.py, not via the CLI.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import random
import sys
import time

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

ENV_SPREADSHEET_ID = "GOOGLE_SHEETS_SPREADSHEET_ID"
ENV_SERVICE_ACCOUNT = "GOOGLE_SERVICE_ACCOUNT_JSON"

OUTPUT_TAB = "output"
STATE_TAB = "state"
CLAIMS_TAB = "claims"

# Ledger of claimed batches, for at-least-once recovery: a batch claimed but never persisted
# (its claimer died) is reclaimed once its lease expires. Columns A..G:
CLAIMS_HEADER = [
    "claim_id", "cursor_start", "cursor_end", "run_id", "status", "lease_expires", "updated_at",
]
CLAIM_STATUS_COL = "E"
CLAIM_LEASE_COL = "F"
CLAIM_UPDATED_COL = "G"
CLAIM_RUN_COL = "D"

OUTPUT_HEADER = [
    "email", "username", "match_confidence", "full_name", "followers", "following", "posts",
    "is_verified", "is_business", "is_private", "external_url", "biography", "avg_views",
    "max_views", "avg_likes", "avg_comments", "engagement_rate", "posts_analyzed", "reels_ratio",
    "posting_cadence_per_week", "last_post_date", "top_hashtags", "stats_status", "evidence_url",
    "resolved_at",
]

# state tab cells
CURSOR_CELL = "B1"
LOCK_TOKEN_CELL = "B2"
LOCK_EXPIRES_CELL = "B3"

LEASE_SECONDS = 120        # a held lock auto-expires after this (covers a crashed claimer)
ACQUIRE_TIMEOUT = 180      # give up trying to acquire after this
_POLL = 2.0                # base wait between acquire attempts / before read-back verify


def _load_service_account_info() -> dict:
    raw = os.environ.get(ENV_SERVICE_ACCOUNT)
    if not raw:
        raise SystemExit(f"sheets_io: {ENV_SERVICE_ACCOUNT} is not set")
    raw = raw.strip().strip('"').strip("'").strip()

    # Allow the raw key JSON to be pasted directly (not base64-encoded).
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"sheets_io: {ENV_SERVICE_ACCOUNT} looks like JSON but won't parse: {exc}")

    # Otherwise base64: tolerate whitespace/newlines and missing '=' padding.
    compact = "".join(raw.split())
    compact += "=" * (-len(compact) % 4)
    try:
        decoded = base64.b64decode(compact)
    except (binascii.Error, ValueError) as exc:
        raise SystemExit(f"sheets_io: {ENV_SERVICE_ACCOUNT} is not valid base64: {exc}")
    try:
        return json.loads(decoded)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SystemExit(
            f"sheets_io: decoded {ENV_SERVICE_ACCOUNT} is not valid JSON ({exc}). "
            "Re-encode the key file as single-line base64: "
            "base64 -i service-account.json | tr -d '\\n'")


def open_spreadsheet():
    """Authorize via the service account and open the configured spreadsheet."""
    import gspread

    spreadsheet_id = os.environ.get(ENV_SPREADSHEET_ID)
    if not spreadsheet_id:
        raise SystemExit(f"sheets_io: {ENV_SPREADSHEET_ID} is not set")
    client = gspread.service_account_from_dict(_load_service_account_info())
    return client.open_by_key(spreadsheet_id)


def _get_or_create(spreadsheet, title: str, cols: int):
    try:
        return spreadsheet.worksheet(title)
    except Exception:  # gspread.WorksheetNotFound (avoid importing the exception class)
        return spreadsheet.add_worksheet(title=title, rows=1000, cols=cols)


def state_worksheet(spreadsheet):
    """Return the state tab, creating it (with key labels) if missing."""
    try:
        ws = spreadsheet.worksheet(STATE_TAB)
    except Exception:
        ws = spreadsheet.add_worksheet(title=STATE_TAB, rows=10, cols=2)
    if not (ws.acell("A1").value or "").strip():
        ws.update(values=[["cursor"], ["lock_token"], ["lock_expires"]], range_name="A1:A3")
    return ws


def claims_worksheet(spreadsheet):
    """Return the claims ledger tab, creating it (with header) if missing."""
    ws = _get_or_create(spreadsheet, CLAIMS_TAB, cols=len(CLAIMS_HEADER))
    if not (ws.acell("A1").value or "").strip():
        ws.update(values=[CLAIMS_HEADER], range_name="A1")
    return ws


def ensure_sheets(spreadsheet) -> dict:
    """Idempotently create the output + state + claims tabs and their headers."""
    out = _get_or_create(spreadsheet, OUTPUT_TAB, cols=len(OUTPUT_HEADER))
    if not (out.acell("A1").value or "").strip():
        out.update(values=[OUTPUT_HEADER], range_name="A1")
    state_worksheet(spreadsheet)
    claims_worksheet(spreadsheet)
    return {"tabs": [OUTPUT_TAB, STATE_TAB, CLAIMS_TAB]}


# --- cursor -----------------------------------------------------------------

def get_cursor(state_ws) -> str:
    return (state_ws.acell(CURSOR_CELL).value or "").strip()


def set_cursor(state_ws, value: str) -> None:
    state_ws.update(values=[[value or ""]], range_name=CURSOR_CELL)


# --- lease-lock -------------------------------------------------------------

def acquire_lock(state_ws, token: str, lease: float = LEASE_SECONDS,
                 timeout: float = ACQUIRE_TIMEOUT, sleep=time.sleep, now=time.time) -> bool:
    """Acquire the claim lock by writing our token + expiry, then reading it back to confirm.

    Sheets has no compare-and-swap, so this is a lease lock with read-back verification: if a
    concurrent writer's token landed last, our read-back differs and we back off and retry.
    Randomized jitter de-syncs contenders. Returns True on success, raises on timeout."""
    deadline = now() + timeout
    while True:
        cur_token = (state_ws.acell(LOCK_TOKEN_CELL).value or "").strip()
        try:
            expires = float(state_ws.acell(LOCK_EXPIRES_CELL).value or 0)
        except (TypeError, ValueError):
            expires = 0.0

        if not cur_token or now() >= expires:
            state_ws.batch_update([
                {"range": LOCK_TOKEN_CELL, "values": [[token]]},
                {"range": LOCK_EXPIRES_CELL, "values": [[str(now() + lease)]]},
            ])
            sleep(_POLL + random.random())          # let any concurrent writer settle
            if (state_ws.acell(LOCK_TOKEN_CELL).value or "").strip() == token:
                return True                          # our token survived -> we hold the lease

        if now() >= deadline:
            raise SystemExit("sheets_io: could not acquire claim lock within timeout")
        sleep(_POLL + random.random() * 2)


def release_lock(state_ws, token: str) -> None:
    """Release the lock iff we still hold it (don't stomp a lease that already expired to another)."""
    if (state_ws.acell(LOCK_TOKEN_CELL).value or "").strip() == token:
        state_ws.batch_update([
            {"range": LOCK_TOKEN_CELL, "values": [[""]]},
            {"range": LOCK_EXPIRES_CELL, "values": [[""]]},
        ])


# --- claims ledger ----------------------------------------------------------

def append_claim(claims_ws, claim_id: str, cursor_start: str, cursor_end: str, run_id: str,
                 lease_expires: float, updated_at: float) -> int:
    """Append an in_progress claim row; returns its 1-based row number (caller holds the lock)."""
    existing = len(claims_ws.col_values(1))      # incl. header; no concurrent append under the lock
    row = existing + 1
    claims_ws.update(
        values=[[claim_id, cursor_start or "", cursor_end or "", run_id, "in_progress",
                 str(lease_expires), str(updated_at)]],
        range_name=f"A{row}:G{row}",
    )
    return row


def find_reclaimable(claims_ws, now: float):
    """Return (row, claim_id, cursor_start, cursor_end) for the first in_progress claim whose lease
    has expired (its claimer is presumed dead), or None. Caller holds the lock."""
    rows = claims_ws.get("A2:G") or []
    for offset, r in enumerate(rows):
        status = r[4] if len(r) > 4 else ""
        if status != "in_progress":
            continue
        try:
            lease = float(r[5]) if len(r) > 5 and r[5] else 0.0
        except (TypeError, ValueError):
            lease = 0.0
        if now >= lease:
            return (offset + 2, r[0], r[1] if len(r) > 1 else "", r[2] if len(r) > 2 else "")
    return None


def reclaim_row(claims_ws, row: int, run_id: str, lease_expires: float, updated_at: float) -> None:
    """Take over an expired in_progress claim: new owner + fresh lease (status stays in_progress)."""
    claims_ws.batch_update([
        {"range": f"{CLAIM_RUN_COL}{row}", "values": [[run_id]]},
        {"range": f"{CLAIM_LEASE_COL}{row}", "values": [[str(lease_expires)]]},
        {"range": f"{CLAIM_UPDATED_COL}{row}", "values": [[str(updated_at)]]},
    ])


def mark_claim_done(claims_ws, row: int, updated_at: float) -> None:
    claims_ws.batch_update([
        {"range": f"{CLAIM_STATUS_COL}{row}", "values": [["done"]]},
        {"range": f"{CLAIM_UPDATED_COL}{row}", "values": [[str(updated_at)]]},
    ])


# --- output -----------------------------------------------------------------

def read_output_emails(spreadsheet) -> set[str]:
    """Return the set of emails already in output col A (for idempotent persist / dedup)."""
    try:
        ws = spreadsheet.worksheet(OUTPUT_TAB)
    except Exception:
        return set()
    vals = ws.col_values(1)
    return {(v or "").strip().lower() for v in vals[1:] if (v or "").strip()}


def append_output(spreadsheet, rows: list[list]) -> int:
    """Append rows to the output tab in a single atomic call; returns the count appended."""
    if not rows:
        return 0
    ws = _get_or_create(spreadsheet, OUTPUT_TAB, cols=len(OUTPUT_HEADER))
    if not (ws.acell("A1").value or "").strip():
        ws.update(values=[OUTPUT_HEADER], range_name="A1")
    cleaned = [["" if c is None else c for c in row] for row in rows]
    ws.append_rows(cleaned, value_input_option="RAW")
    return len(cleaned)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in {"ensure-sheets", "append-output"}:
        print("usage: sheets_io.py {ensure-sheets|append-output}", file=sys.stderr)
        return 2
    cmd = argv[0]
    spreadsheet = open_spreadsheet()

    if cmd == "ensure-sheets":
        result = ensure_sheets(spreadsheet)
        json.dump(result, sys.stdout)
        sys.stdout.write("\n")
        return 0

    # append-output
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:  # noqa: BLE001
        print(f"sheets_io: invalid JSON on stdin: {exc}", file=sys.stderr)
        return 2
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        print('sheets_io: expected {"rows": [[...]]} on stdin', file=sys.stderr)
        return 2
    appended = append_output(spreadsheet, rows)
    json.dump({"appended": appended}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
