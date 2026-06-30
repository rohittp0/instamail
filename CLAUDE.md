# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

InstaMail resolves Instagram handles + public stats for a list of emails, **Sheets-native**. Emails
live in a Google Sheet (`dump` tab); a formula builds an `input` tab (cleaned, deduped, minus
already-processed); a Claude **Workflow** reads `input`, resolves each owner's Instagram handle via
web OSINT, fetches public stats with a direct script, and appends a row to `output`. Re-running
resumes automatically — the `input` formula already excludes anything in `output`.

Authoritative docs: `CONTEXT.md` (glossary) and `docs/adr/` (0001 = Sheets substrate + formula
resume, 0002 = token-efficient agentic resolve).

## Layout

- `.claude/workflows/email-to-instagram.js` — the orchestrator (Load → Resolve → Escalate → Persist).
  Its JS sandbox has **no network/filesystem access**; it fans out agents that shell out to Python.
- `scripts/` — the Python toolkit the agents call:
  - `ig_profile.py` — `Harvester` (public `web_profile_info` via curl_cffi) + `compute_metrics` + `project_profile`.
  - `instagram_stats.py` — direct, non-agentic stats CLI: stdin `{usernames}` → stdout `{username: stats}`.
  - `sheets_io.py` — gspread gateway: `ensure-sheets` / `read-input` / `append-output`.
  - `persist.py` — per-batch orchestrator: stats + deterministic email-in-bio confidence upgrade + append.
  - `tests/` — network-free pytest suite.

## Commands

Use the in-repo venv (Python 3.14): `.venv/bin/python`.

- Install deps: `.venv/bin/pip install -e ".[dev]"` (or install the `pyproject` deps directly).
- Run tests: `.venv/bin/python -m pytest` (`asyncio_mode=auto`; `scripts/` is on `pythonpath`).
- Stats one-off: `echo '{"usernames":["natgeo"]}' | .venv/bin/python scripts/instagram_stats.py`
- Sheet setup: `.venv/bin/python scripts/sheets_io.py ensure-sheets`
- Run the pipeline: invoke the `email-to-instagram` Workflow (optionally `args: {batch_size}`).

## Configuration (`.env`)

`GOOGLE_SHEETS_SPREADSHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON` (base64 of the SA key JSON; share the
sheet with its `client_email`), and optional `INSTAGRAM_SESSIONID` (booster for stats; works without
it). Scripts load `.env` via python-dotenv. See `.env.example`.

## Conventions

- **Never commit** secrets or harvested data: `.env`, `service-account*.json`, `.cache/`, and stray
  `*.csv` / `emails*` files are gitignored.
- **Resume is the sheet, not a file** — never reintroduce a checkpoint file (ADR 0001).
- **The resolve agent must never fetch instagram.com** — the profile read is Python's job, and the
  email-in-bio confidence upgrade is deterministic (ADR 0002). Preserve both to keep the design
  token-efficient.
- Keep the Python scripts single-responsibility and importable so the tests stay network-free and
  the persist agent stays a near-zero-token Bash call.
