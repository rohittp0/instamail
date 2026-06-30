# Internal API input, Google Sheets output, cursor-based resume

Users to process come from the product's **internal users API**
(`GET …/internal/users/after/`, header `X-Internal-Key`, cursor-paginated by `email=<last_email>`,
returning `{"users":[{email, first_name, last_name}]}`). Results are written to a Google Spreadsheet:
an `output` tab (one row per processed user) and a small `state` tab (the pagination cursor + a
lease-lock). The Claude **Workflow**'s JS sandbox cannot touch the network/filesystem, so a Python
toolkit (`scripts/`, over `gspread` + `requests`) is the only gateway; the Workflow's agents shell
out to it.

This supersedes an earlier design where input also lived in the Sheet (`dump` tab → a `FILTER/UNIQUE`
formula → `input` tab) with formula-driven resume. That was dropped: the real source of "who to
process" is the product's user list, and a sheet full of random consumer emails resolved almost
entirely to dead-ends.

## Decision

- **Input = the internal users API**, paginated by a positional `email` cursor. The Workflow pulls
  users (email + name) in batches; the name is a strong OSINT seed.
- **Output = the Sheet `output` tab** (unchanged 25-column schema), appended one batch at a time.
- **Resume = a stored cursor in the `state` tab.** Re-running continues the API after the last
  claimed email. The cursor *must* be stored (not derived from the max email in `output`) because
  the API order is positional, not email-sorted — a derived cursor would skip or repeat users.

## Consequences

- **Resume is a single stored value**, advanced as work is claimed (see ADR 0003); a kill/rate-limit
  just leaves the cursor where it was. The cost is one extra tiny `state` read/write per claim.
- **Sheet shrinks to `output` + `state`** — no `dump`/`input` tabs, no spill-sizing, no cleaning
  formula. Email cleaning is no longer needed (the API yields real, deduped users).
- **A lost append ack can double-write one batch** (append is the atomic unit); rare and resolvable
  via a `=UNIQUE` view.
- **Operational setup:** `INTERNAL_API_KEY` (+ optional `INTERNAL_API_BASE`), and the spreadsheet
  shared with the service-account `client_email`, with `GOOGLE_SHEETS_SPREADSHEET_ID` +
  base64 `GOOGLE_SERVICE_ACCOUNT_JSON` in `.env`.
