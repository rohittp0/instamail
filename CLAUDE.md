# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

InstaMail is a pluggable OSINT CLI: it reads a list of emails, runs a selected set of
per-platform enrichment **plugins** against each email concurrently, and writes a single
combined CSV. The repository ships the **framework only** — no real plugins. Email→account
resolution is entirely each plugin's concern.

Authoritative design docs (read these before changing behavior):
- `CONTEXT.md` — canonical glossary (plugin contract, concurrency model, autoresume).
- `docs/adr/0001-output-csv-as-resume-journal.md` — why the output CSV is the resume journal.
- `docs/superpowers/specs/2026-06-17-instamail-osint-cli-design.md` — full design.
- `docs/superpowers/plans/2026-06-17-instamail-osint-cli.md` — the task-by-task build plan.

## Commands

Always use the in-repo virtualenv interpreter (Python 3.14): `.venv/bin/python`.

- Install (editable + dev deps): `.venv/bin/pip install -e ".[dev]"`
- Run the whole test suite: `.venv/bin/python -m pytest`
- Run one test: `.venv/bin/python -m pytest tests/test_runner.py::test_timeout_is_error -v`
- Run the tool: `.venv/bin/python -m instamail -i emails.txt --plugins all` (or the `instamail` console script after install)
- List available plugins: `.venv/bin/python -m instamail --list-plugins`

`pytest` is configured with `asyncio_mode = "auto"` (see `pyproject.toml`), so `async def`
test functions run without an explicit marker.

## Architecture

The pipeline is a one-directional flow across small single-responsibility modules under
`src/instamail/`:

`cli.py` (orchestration) → `emails.clean_emails` → `loader.load_plugins`/`select_plugins`
→ `runner.iter_results` (async fan-out) → `writer.CsvWriter` (streaming output).

Things that span multiple files and are easy to get wrong:

- **Plugin contract (`base.py`).** A plugin subclasses `BasePlugin`, sets `name`, `fields`,
  `max_concurrency`, `timeout`, and implements `async def fetch(email) -> dict`. The returned
  dict's keys must **exactly** match `fields` (no extras, none missing); a field with no value
  must be `None`. A key mismatch is a **contract violation = fatal, aborts the whole run**
  (it's a plugin bug that would recur on every email). `fetch` raises `AccountNotFound` on a
  clean miss; any other exception is a runtime `error`. Runtime failures (not_found / error /
  timeout) are logged and **never** abort the run.

- **Concurrency is per-plugin, not global (`runner.py`).** Each plugin gets its own
  `asyncio.Semaphore(max_concurrency)` because rate limits are a property of the platform, not
  the run. There is deliberately **no** global concurrency cap and **no** `--concurrency` flag.
  `iter_results` yields rows in input order via a bounded look-ahead window, so a single slow
  email (near its `timeout`) holds back rows behind it — this is the intended ordered-output
  trade-off.

- **Output CSV doubles as the resume journal (`writer.py`).** Rows stream and flush per email
  only after *all* its selected plugins finish, so "has a row" == "fully processed". On startup,
  emails already present in the output CSV are skipped and new rows append. The existing header
  must **exactly equal** the current selection's expected header, or the run aborts — resume
  continues the *same* run; a changed plugin set requires a new output file.

- **CSV column layout.** Plugins are ordered **alphabetically by `name`** (regardless of
  selection/discovery order); columns are `{plugin}_{field}`; the first column is `email`. Cell
  serialization: scalars as-is, `None` → blank, non-scalars JSON-encoded.

- **Loader is fail-fast (`loader.py`).** Duplicate plugin `name`, an unknown name in `--plugins`,
  an empty plugins dir under `--plugins all`, and a plugin file that fails to import are all
  fatal — a silently-missing plugin would yield a CSV that looks complete but isn't.

## Conventions

- Plugins live in `./plugins/` (CWD-relative, overridable with `--plugins-dir`), scanned at
  runtime — adding a plugin needs no reinstall. Two layouts: a single `foo.py` file, or a
  self-contained package `foo/` whose `__init__.py` re-exports its plugin class (e.g.
  `from .plugin import FooPlugin`) — the loader puts `./plugins/` on `sys.path` so intra-package
  imports resolve, and registers the re-exported class. The bundled `instagram` plugin uses the
  package layout (with its own `tests/`). Files/dirs starting with `_`, `__pycache__`, and a
  top-level `tests` dir are ignored.
- The CSV file is the only stdout-side artifact; all diagnostics (per-failure lines, skipped
  invalid emails, the end-of-run summary) go to stderr via `logging`. `-v` also logs successes.
- `email-validator` is the **only** runtime dependency (syntax validation + normalization, DNS
  off). Plugins bring their own HTTP clients; do not add a framework-level HTTP client.
