# Google Sheets as the data substrate, with formula-driven resume

The pipeline reads its work and writes its results to a single Google Spreadsheet with three fixed
tabs — `dump` (raw user paste), `input` (a formula-built column of cleaned, deduped,
not-yet-processed emails), and `output` (one row per processed email) — instead of local files/CSV.
The Claude **Workflow**'s JS sandbox cannot touch the network or filesystem, so a small Python
toolkit (`scripts/sheets_io.py` over `gspread`, authed by a base64 service-account key in
`GOOGLE_SERVICE_ACCOUNT_JSON`) is the only gateway; the Workflow's agents shell out to it.

This replaces the previous file-in / CSV-out design and its sidecar `checkpoint.json`.

## Decision

- **Three tabs, one spreadsheet.** Users paste into `dump`; a single `FILTER/UNIQUE` formula builds
  `input` = `lower(trim(dump))` that is format-valid **and not present in `output`**, deduped.
  `ensure-sheets` installs that formula only when `input!A1` is empty, so a hand-tuned formula is
  never clobbered.
- **Resume is formula-driven, not file-driven.** Because `input` subtracts `output`, re-running the
  Workflow naturally processes only what is left. Every processed email — including dead-ends
  (`email,,none`) — is appended to `output`, so nothing is ever retried. There is no checkpoint file.
- **Append is the atomic unit.** Each batch is one `append_rows` call after its emails resolve.

## Consequences

- **Resume is trivial and robust** to kills/rate-limits: there is no checkpoint to keep consistent
  with the data — the data *is* the checkpoint. The cost is a network read of `input` per run.
- **Cleaning is limited to what formulas can do** (lowercase/trim, regex validity, dedup by
  normalized address, blocklist range). Gmail dot/+tag canonicalization and large relay/disposable
  filtering from the old `clean_emails.py` are **deferred**; they can return later as a sheets-aware
  Python refresh without changing this contract.
- **A lost API ack can double-write one batch.** If `append_rows` lands but its response is lost, a
  re-run re-resolves those emails (they weren't recorded) and appends a duplicate `output` row.
  Accepted; dedupe via a formula/periodic pass if it ever matters.
- **Single-writer assumption.** Two concurrent runs read the same `input` snapshot and double-process.
  Out of scope.
- **Operational setup:** the spreadsheet must be shared with the service-account `client_email`, and
  `GOOGLE_SHEETS_SPREADSHEET_ID` + `GOOGLE_SERVICE_ACCOUNT_JSON` (base64) set in `.env`.
