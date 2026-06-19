# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Rebuilt after a design pivot (the old email-keyed enrichment implementation was intentionally
deleted in commit `ec6d628`; do **not** restore it). The **framework** is built and tested, and
the first real plugin — `instagram` (`plugins/instagram/`) — is built and tested. Authoritative
docs: `CONTEXT.md` (glossary, incl. the Instagram-plugin terms) and `docs/adr/` (0001 = framework,
0002 = instagram free-first env-gated providers).

## What this is

InstaMail is a pluggable CLI that scrapes content-creator details from social media platforms.
The user invokes it with **search terms** and `--platforms` (a comma-separated list, or `all`).
Each platform is a **plugin**; the CLI fans the inputs out to the selected plugins, then merges
their results into a single CSV.

## Core model: key-based merge

- Each plugin receives the CLI inputs and returns an array of rows: `[{key_column, ...other columns}]`.
- `key_column` is an identity field such as `email` or `phone` — the value the merge joins on.
- The CLI merges rows across plugins **by key value**: when `key(pluginA) == key(pluginB)`, their
  columns are combined into one row.
- The **key column is emitted only once** in the output (no duplicate key columns across plugins).
- Unmatched keys, and plugins that key on **different identity types** (e.g. one on `email`, one
  on `phone`), produce rows where the absent plugin's columns are left **blank**.
- Final CSV header: `{plugin1}_{column1}, ..., {pluginN}_{columnN}` — columns are namespaced by
  plugin to avoid collisions; the shared key column is the exception (written once).

## Commands

Use the in-repo virtualenv interpreter (Python **3.14**): `.venv/bin/python`.

- Install (editable + dev deps): `.venv/bin/pip install -e ".[dev]"`
- Run the whole test suite: `.venv/bin/python -m pytest`
- Run one test: `.venv/bin/python -m pytest tests/test_cli.py::test_name -v`
- Run the tool: `.venv/bin/python -m instamail <search terms> --platforms all`
  (or the `instamail` console script after install — entrypoint is `instamail.cli:main`).

`pytest` runs with `asyncio_mode = "auto"`, so `async def` test functions need no marker. Fake
plugins for the framework tests live under `tests/fixtures/` and are loaded by **file path** via
`--plugins-dir` (never imported), so they don't shadow real plugins during collection.

## Architecture (`src/instamail/`)

One-way flow: `cli.main` → `loader` (discover/select) → `runner` (concurrent search) →
`merge` → `writer`.

- **`base.py`** — `BasePlugin` (class attrs `name`/`key`/`fields`, classmethod `add_arguments`,
  `async def search(terms, opts) -> list[dict]`) and the `ContractViolation` exception.
- **`loader.py`** — discovers plugins from a dir (single `foo.py` or package `foo/` re-exporting
  its class) by file path. **Fail-fast** on: duplicate `name`, unknown selection, empty dir under
  `all`, import error, malformed `name` (`[a-z0-9_]+`), or a `name` that is a prefix of another
  (would break flag de-prefixing).
- **`cli.py`** — two-phase argparse. Phase 1: lenient `parse_known_args` to *select* plugins.
  Phase 2: strict parser (`allow_abbrev=False`) where each selected plugin registers its args
  through `_PrefixProxy`, which rewrites flags to `--{name}-flag`, forces a prefixed `dest`, and
  records exact `(clean, prefixed)` dest pairs so each plugin gets its args back **unprefixed** by
  membership (never `startswith`).
- **`runner.py`** — `asyncio.gather` across plugins. Validates each returned row's keys
  immediately: a mismatch raises `ContractViolation` (fatal); any other plugin exception is logged
  and that plugin contributes no rows (isolated, non-fatal).
- **`merge.py`** — grouped-merge-then-stack by key value; header derived from selected-plugin
  metadata (so empty results still contribute columns); empty/`None` key → standalone row;
  duplicate key within one plugin → last-wins.
- **`writer.py`** — CSV out (default `out.csv`); cells: scalar as-is, `None`→blank, else JSON.

Plugins go in `plugins/` (CWD-relative, override with `--plugins-dir`), discovered at runtime —
adding one needs no reinstall. **No autoresume/streaming** — buffer → merge → write once.

## Instagram plugin (`plugins/instagram/`)

Keys on `email`; turns travel terms into ranked creator records via a layered pipeline
(`pipeline.py`): Discovery → Enrichment → Email → Verification → dedup/rank/limit. Each layer is a
**preference chain** of providers ordered FREE→OFFICIAL→PAID (`providers/registry.py`); a provider
is eligible only when its env credential is set, and the layer takes the **first success** so PAID
(Bright Data/Apify vendor, Hunter) runs only as a last resort. `--instagram-max-tier` caps the tier.
Free path = public scraping (`http.py`: `web_profile_info` via curl_cffi + DDG dorking) — **violates
IG ToS**, logged at runtime (see ADR 0002). Official/vendor adapters have real request code but are
mock-tested only (no live creds). Ranking uses avg/max Reel views (`metrics.py`) as a reach proxy.
Env vars in `.env.example`. Providers are constructor-injectable into `Pipeline` for network-free
tests (`plugins/instagram/tests/`).

## Dependencies

Runtime deps are deliberately minimal (`pyproject.toml`): `email-validator` (key
validation/normalization), `curl_cffi` (HTTP with browser-impersonation, for plugins that scrape
behind bot protection), `python-dotenv` (loads `.env` at startup so secrets reach all plugins).
Prefer letting plugins own their scraping specifics; keep the framework thin.

## Conventions

- Never commit harvested data or secrets: `emails.txt`, `out.csv`, `errors.csv`, `.cache/`, and
  `.env` are gitignored. The CSV is the only stdout-side artifact; diagnostics go to stderr.
- Plugin column namespacing (`{plugin}_{column}`) and single-emission of the key column are the
  invariants that keep merged CSVs unambiguous — preserve them in any plugin or writer change.
