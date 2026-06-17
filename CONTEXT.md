# Context: InstaMail

An OSINT CLI that enriches a list of emails into a single CSV by running pluggable
per-platform enrichment plugins concurrently.

## Glossary

- **Plugin** — a self-contained unit that knows how to turn one email into details
  about that person on one platform (e.g. Instagram). Subclasses `BasePlugin`,
  lives as a `.py` file dropped into the plugins directory (CWD-relative
  `./plugins/` by default, overridable with `--plugins-dir`, scanned at startup
  with no reinstall), and is identified by its `name`.

- **name** — a plugin's unique identifier (e.g. `instagram`), defined by the
  class attribute (not the filename). Used both to select the plugin on the CLI
  (`--plugins instagram`) and to prefix its CSV columns (`instagram_followers`).
  Duplicate names across plugin files, an unknown name in `--plugins`, an empty
  `plugins/` under `--plugins all`, and a plugin file that fails to import are all
  fatal (fail fast) — a silently-missing plugin would yield a CSV that looks
  complete but isn't.

- **fields** — the list of output field names a plugin declares up front. Combined
  with `name`, these fix the plugin's CSV columns regardless of which emails
  succeed. Column for field `f` of plugin `p` is `{p}_{f}`. Plugins are ordered
  in the CSV alphabetically by `name` (regardless of selection order or discovery
  order); fields within a plugin follow declared `fields` order. Cell
  serialization: scalars (`str`/`int`/`float`/`bool`) written as-is, `None` blank,
  any non-scalar (list/dict) JSON-encoded into the cell.

- **fetch(email)** — a plugin's async method that resolves one email into a dict of
  `field -> value`. The returned dict's keys must match the declared `fields`
  **exactly** — no extra keys, none missing. A field with no value must be
  returned explicitly as `None` (rendered as a blank CSV cell). Raises
  `AccountNotFound` on a clean miss, any other exception on a runtime failure.

- **Contract violation** — a returned dict whose keys do not exactly match
  `fields`. This is a plugin bug, not a data miss, so it is fatal: the run aborts
  immediately on first occurrence with a message naming the plugin and the
  offending keys. (Runtime *data* failures — not_found, timeout, network — never
  abort the run; only contract violations do.)

- **max_concurrency** — per-plugin cap on simultaneous in-flight `fetch` calls for
  that plugin. Each plugin gets its own semaphore sized to this value. There is no
  global concurrency ceiling — concurrency is governed entirely per-plugin, because
  rate limits are a property of the platform, not the run.

- **Run summary** — end-of-run line on stderr counting ok / not_found / error
  outcomes across all email × plugin tasks.

- **Streaming write** — rows are written to the output CSV as each email's selected
  plugins all complete (not buffered to the end), in input order via an ordered
  sliding window, flushed per row. A crash therefore leaves only complete rows.

- **Autoresume** — on startup, if the output CSV exists, the emails already present
  in its `email` column are skipped and new rows append in input order. "Has a row"
  == "fully processed" (guaranteed by per-row flush). Resume requires the existing
  CSV header to exactly match the current selection's expected header; a mismatch
  (different plugin set/fields) is fatal — resume continues the *same* run, and a
  changed plugin set requires a new output file.

- **Email cleaning** — input lines are validated and normalized with the
  `email-validator` library (syntax only; DNS/deliverability checks off). The
  normalized form is used for dedup and passed to plugins; invalid lines are
  logged and skipped. Rows appear in first-seen order after dedup. `email-validator`
  is the framework's one runtime dependency; plugins bring their own HTTP clients.
  The normalized email is **fully lowercased** (local part and domain) for both
  dedup and CSV output — email local parts are treated case-insensitively, which
  keeps dedup consistent with what is written out.
