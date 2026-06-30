# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

InstaMail resolves Instagram handles + public stats for the product's users. Users (email + name)
come from the **internal users API**; a Claude **Workflow** resolves each owner's Instagram handle
via web OSINT (seeded with their name), fetches public stats with a direct script, and appends a row
to the `output` tab of a Google Sheet. Re-running resumes from a stored cursor, and concurrent runs
are **parallel-safe** (each claims a disjoint slice via a lease-lock on the `state` tab).

Authoritative docs: `CONTEXT.md` (glossary) and `docs/adr/` (0001 = API input / Sheets output /
cursor resume, 0002 = token-efficient agentic resolve, 0003 = parallel-safe claiming).

## Layout

- `.claude/workflows/email-to-instagram.js` — the orchestrator (Claim → Resolve → Escalate → Persist).
  Its JS sandbox has **no network/filesystem access**; it fans out agents that shell out to Python.
- `scripts/` — the Python toolkit the agents call:
  - `users_api.py` — internal users API client: `fetch_users(after, limit)` → `[{email, name}]`.
  - `claim.py` — parallel-safe atomic claim: lease-lock → cursor → API page → advance cursor.
  - `ig_profile.py` — `Harvester` (public `web_profile_info` via curl_cffi) + `compute_metrics` + `project_profile`.
  - `instagram_stats.py` — direct, non-agentic stats CLI: stdin `{usernames}` → stdout `{username: stats}`.
  - `sheets_io.py` — gspread gateway: `ensure-sheets` / `append-output` + cursor & lease-lock helpers.
  - `persist.py` — per-batch orchestrator: stats + deterministic email-in-bio confidence upgrade + append.
  - `tests/` — network-free pytest suite.

## Commands

Use the in-repo venv (Python 3.14): `.venv/bin/python`.

- Install deps: `.venv/bin/pip install -e ".[dev]"` (or install the `pyproject` deps directly).
- Run tests: `.venv/bin/python -m pytest` (`asyncio_mode=auto`; `scripts/` is on `pythonpath`).
- Stats one-off: `echo '{"usernames":["natgeo"]}' | .venv/bin/python scripts/instagram_stats.py`
- Fetch users: `.venv/bin/python scripts/users_api.py --limit 5`
- Claim a batch (advances the cursor): `.venv/bin/python scripts/claim.py 10`
- Sheet setup: `.venv/bin/python scripts/sheets_io.py ensure-sheets`
- Run the pipeline: invoke the `email-to-instagram` Workflow (optionally `args: {batch_size}`).

## Configuration (`.env`)

`INTERNAL_API_KEY` (+ optional `INTERNAL_API_BASE`), `GOOGLE_SHEETS_SPREADSHEET_ID`,
`GOOGLE_SERVICE_ACCOUNT_JSON` (base64 of the SA key JSON; share the sheet with its `client_email`),
and optional `INSTAGRAM_SESSIONID` (booster for stats; works without it). Scripts load `.env` via
python-dotenv. See `.env.example`.

## Conventions

- **Never commit** secrets or harvested data: `.env`, `service-account*.json`, `.cache/`, and stray
  `*.csv` / `emails*` files are gitignored.
- **Resume is the `state`-tab cursor, not a file** — never reintroduce a checkpoint file (ADR 0001).
- **Concurrent runs claim under the lease-lock** (ADR 0003) — keep the cursor advance inside the lock.
- **The resolve agent must never fetch instagram.com** — the profile read is Python's job, and the
  email-in-bio confidence upgrade is deterministic (ADR 0002). Preserve both to keep the design
  token-efficient.
- Keep the Python scripts single-responsibility and importable so the tests stay network-free and
  the persist agent stays a near-zero-token Bash call.
