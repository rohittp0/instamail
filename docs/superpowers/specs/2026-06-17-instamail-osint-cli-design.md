# InstaMail — OSINT email-enrichment CLI — Design

## Summary

A Python 3.14 CLI that reads a list of emails, runs a selected set of pluggable
per-platform enrichment plugins against each email concurrently, and streams a
single combined CSV. The deliverable is the **framework only** — no example
plugin ships; real plugins are dropped into `./plugins/` later. Email→account
resolution is entirely the plugin's concern.

See `CONTEXT.md` for the canonical glossary and `docs/adr/0001` for the
output-CSV-as-resume-journal decision.

## Plugin contract (`base.py`)

```python
class BasePlugin:
    name: str                 # unique id, e.g. "instagram"; also the CSV column prefix
    fields: list[str]         # declared output field names, fixed up front
    max_concurrency: int = 5  # per-plugin cap on simultaneous in-flight fetch calls
    timeout: float = 10.0     # per-fetch timeout in seconds

    async def fetch(self, email: str) -> dict[str, Any]:
        """Resolve one email. Return a dict whose keys EXACTLY match `fields`
        (no extras, none missing); a field with no value must be None.
        Raise AccountNotFound on a clean miss; any other exception on runtime failure."""
```

Exceptions defined in `base.py`: `AccountNotFound` (clean miss).

## Components

Each is a small, independently testable unit.

1. **`loader.py`** — scans the plugins directory (`./plugins/` by default,
   `--plugins-dir` to override), imports each `.py`, registers every `BasePlugin`
   subclass by `name`. Resolves `--plugins` (`all` or comma list). All loader
   problems are **fatal (fail fast)**: duplicate `name`, unknown selected name,
   empty dir under `all`, or a plugin file that fails to import.

2. **`runner.py`** — builds the `email × selected-plugin` task matrix. Each plugin
   gets its **own `asyncio.Semaphore(max_concurrency)`** (no global ceiling). Each
   `fetch` is wrapped in `asyncio.wait_for(timeout)`. Outcomes per task:
   - **ok** — returned dict; keys validated against `fields`.
   - **not_found** — `AccountNotFound` raised.
   - **error** — any other exception or timeout.
   - **contract violation** — returned dict keys ≠ `fields`: **fatal, aborts the
     whole run** naming plugin + offending keys. (Non-scalar values are not a
     violation — they are JSON-encoded by the writer; a value that is not
     JSON-serializable falls back to `str()`.)
   Runtime data failures (not_found/error/timeout) never abort the run; they are
   logged to the console and leave that plugin's cells blank.

3. **`writer.py`** — header is `email` + `{plugin}_{field}` for every selected
   plugin, plugins ordered **alphabetically by `name`**, fields in declared order.
   Cell serialization: scalars (`str`/`int`/`float`/`bool`) as-is, `None` blank,
   non-scalars (list/dict) JSON-encoded. **Streams** rows in input order via an
   ordered sliding window, flushing per row once all of an email's plugins finish.

4. **Autoresume** (in `writer.py` / `cli.py`) — if the output CSV exists, read its
   `email` column, skip those emails, append new rows. The existing header must
   exactly equal the current selection's expected header or the run aborts.

5. **`cli.py`** — argparse + orchestration; `email-validator`-based cleaning
   (syntax only, DNS off): trim, skip blanks, validate, normalize, dedupe
   case-insensitively keeping first-seen order, log+skip invalid lines.

6. **`__main__.py` / console script** — both invoke `cli.main`.

## CLI

```
instamail -i emails.txt [-o out.csv] [--plugins all|instagram,twitter]
          [--plugins-dir ./plugins] [-v] [--list-plugins]
```

- `-i/--input` required (one email per line). `-o/--output` default `out.csv`.
- `--plugins` default `all`. `--list-plugins` prints each plugin's `name`,
  `fields`, `max_concurrency`, then exits.
- `-v/--verbose` also logs successes; default logs warnings/errors only.
- No `--concurrency` flag — concurrency is governed per-plugin via `max_concurrency`.

## Logging (stderr)

- Per-failure lines, e.g. `WARNING instagram alice@x.com: not_found`,
  `WARNING instagram bob@x.com: timeout after 10s`.
- Skipped invalid input lines with reason.
- End-of-run summary: `Processed N emails × M plugins: A ok, B not_found, C error`.
- The CSV file is the only stdout-side artifact; console stays purely diagnostic.

## Data flow

parse+clean emails → resolve selected plugins → (autoresume: drop already-present
emails) → schedule `email × plugin` async tasks, each gated by its plugin's
semaphore and timeout → as each email's tasks all complete, the ordered window
emits its row → append+flush to CSV; failures logged → final summary.

## Packaging

- `pyproject.toml`: name `instamail`, `requires-python >=3.14`, runtime dep
  `email-validator`; dev extras `pytest`, `pytest-asyncio`.
- `src/` layout: `src/instamail/{__init__,__main__,cli,base,loader,runner,writer}.py`.
- Top-level `plugins/` directory (CWD-relative, scanned at runtime — no reinstall
  to add a plugin).

## Testing (pytest + pytest-asyncio)

- **loader**: discovers fixture plugins from a temp dir; fails fast on duplicate
  name / unknown selection / empty dir / import error.
- **runner**: per-plugin concurrency is respected; not_found/error/timeout are
  captured without aborting; contract violation aborts; key validation works.
- **writer**: namespaced alphabetical header; scalar/None/JSON serialization;
  streaming row order; per-row flush.
- **autoresume**: existing CSV emails skipped; header mismatch aborts; rows append.
- **cli**: email cleaning (trim/dedupe/normalize/skip-invalid); end-to-end smoke
  with a fixture plugin.

Fixture plugins live in `tests/` — no throwaway plugin ships in `./plugins/`.

## Out of scope (YAGNI)

- Any real platform integration / example plugin.
- Global concurrency ceiling.
- Per-plugin partial resume within a single email.
- Reconciling a changed plugin set into an existing CSV.
