#!/usr/bin/env python3
"""Google Sheets gateway for the email->instagram workflow.

This is the single point that touches the spreadsheet; the JS Workflow's agents shell out to it.
Three fixed tabs:

  dump   - raw emails the user pastes (col A, row 1 = "email" header)
  input  - one computed column of cleaned, deduped, not-yet-processed emails (formula in A1)
  output - one row per processed email (header in row 1; the workflow appends below)

Resume is formula-driven: `input` excludes anything already in `output`, so re-running the
workflow naturally picks up only unprocessed emails — there is no checkpoint file.

Auth: GOOGLE_SERVICE_ACCOUNT_JSON holds the *base64-encoded* service-account key JSON itself
(not a path). Share the spreadsheet with the service-account email once.

Subcommands:
  ensure-sheets   create the three tabs + output header + the input formula (idempotent)
  read-input      print {"emails": [...]} from the computed input column
  append-output   read {"rows": [[...25 cells...], ...]} on stdin and append to output

Usage:
    .venv/bin/python scripts/sheets_io.py ensure-sheets
    .venv/bin/python scripts/sheets_io.py read-input
    echo '{"rows": [[...]]}' | .venv/bin/python scripts/sheets_io.py append-output
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

ENV_SPREADSHEET_ID = "GOOGLE_SHEETS_SPREADSHEET_ID"
ENV_SERVICE_ACCOUNT = "GOOGLE_SERVICE_ACCOUNT_JSON"

DUMP_TAB = "dump"
INPUT_TAB = "input"
OUTPUT_TAB = "output"

OUTPUT_HEADER = [
    "email", "username", "match_confidence", "full_name", "followers", "following", "posts",
    "is_verified", "is_business", "is_private", "external_url", "biography", "avg_views",
    "max_views", "avg_likes", "avg_comments", "engagement_rate", "posts_analyzed", "reels_ratio",
    "posting_cadence_per_week", "last_post_date", "top_hashtags", "stats_status", "evidence_url",
    "resolved_at",
]

# Cleans dump -> input: lowercase/trim, format-valid, not already in output, deduped. Installed
# into input!A1 only when that cell is empty, so a hand-tuned formula is never overwritten.
INPUT_FORMULA = (
    '=IFERROR(UNIQUE(FILTER('
    'LOWER(TRIM(dump!A2:A)),'
    'LEN(TRIM(dump!A2:A)),'
    'REGEXMATCH(LOWER(TRIM(dump!A2:A)), "^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$"),'
    'ISNA(MATCH(LOWER(TRIM(dump!A2:A)), output!A:A, 0))'
    ')), )'
)


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
    """Authorize via the base64 service account and open the configured spreadsheet."""
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


def ensure_sheets(spreadsheet) -> dict:
    """Idempotently create the three tabs, the output header, and the input formula."""
    dump = _get_or_create(spreadsheet, DUMP_TAB, cols=1)
    inp = _get_or_create(spreadsheet, INPUT_TAB, cols=1)
    out = _get_or_create(spreadsheet, OUTPUT_TAB, cols=len(OUTPUT_HEADER))

    # The input formula spills up to one row per dump email; a too-small input tab raises a
    # "please insert more rows" spill error. Size input to hold the worst case (every dump row
    # passes the filter). Re-run ensure-sheets after a large dump paste to grow it again.
    if inp.row_count < dump.row_count:
        inp.resize(rows=dump.row_count, cols=1)

    if not (dump.acell("A1").value or "").strip():
        dump.update(values=[["email"]], range_name="A1")

    if not (out.acell("A1").value or "").strip():
        out.update(values=[OUTPUT_HEADER], range_name="A1")

    installed_formula = False
    if not (inp.acell("A1", value_render_option="FORMULA").value or "").strip():
        inp.update(values=[[INPUT_FORMULA]], range_name="A1", value_input_option="USER_ENTERED")
        installed_formula = True

    return {"tabs": [DUMP_TAB, INPUT_TAB, OUTPUT_TAB], "input_formula_installed": installed_formula}


def read_input(spreadsheet, limit: int | None = None) -> list[str]:
    """Return cleaned, not-yet-processed emails from the computed input column.

    ``limit`` caps how many are returned (a bounded range read) — the workflow takes only the
    next slice per run, since resume re-reads what's left. The cap is applied to *kept* emails,
    so it survives blanks/dupes near the top of the column."""
    ws = spreadsheet.worksheet(INPUT_TAB)
    if limit:
        # over-fetch a little so dropped blanks/dupes don't starve the slice
        raw = ws.get(f"A1:A{int(limit) * 2 + 50}")
        cells = [(row[0] if row else "") for row in raw]
    else:
        cells = ws.col_values(1)

    out: list[str] = []
    for cell in cells:
        val = (cell or "").strip().lower()
        if not val or val == "email" or val.startswith("#"):  # blanks / stray header / error spill
            continue
        if val not in out:
            out.append(val)
            if limit and len(out) >= limit:
                break
    return out


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
    if not argv or argv[0] not in {"ensure-sheets", "read-input", "append-output"}:
        print("usage: sheets_io.py {ensure-sheets|read-input|append-output}", file=sys.stderr)
        return 2
    cmd = argv[0]
    spreadsheet = open_spreadsheet()

    if cmd == "ensure-sheets":
        result = ensure_sheets(spreadsheet)
        json.dump(result, sys.stdout)
        sys.stdout.write("\n")
        return 0

    if cmd == "read-input":
        limit = None
        if len(argv) > 1:
            try:
                limit = int(argv[1])
            except ValueError:
                print("sheets_io: read-input limit must be an integer", file=sys.stderr)
                return 2
        emails = read_input(spreadsheet, limit=limit)
        json.dump({"emails": emails}, sys.stdout)
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
