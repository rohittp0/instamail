# InstaMail

A search-driven OSINT CLI: given search terms and a set of platforms, it runs one **plugin**
per platform concurrently and merges their results into a single CSV keyed on an identity column.

## Language

**Plugin**:
A self-contained unit that searches one platform for creators matching the terms and returns
rows of details. Subclasses `BasePlugin`, lives in the plugins directory (CWD-relative
`./plugins/` by default, overridable with `--plugins-dir`), and is identified by its `name`.
_Avoid_: module, scraper, provider.

**name**:
A plugin's unique identifier (e.g. `instagram`), set as a class attribute, not derived from the
filename. Selects the plugin on the CLI (`--platforms instagram`), prefixes its CSV columns
(`instagram_followers`), and prefixes its CLI flags (`--instagram-min-followers`). Constrained to
`[a-z0-9_]+`, and no name may be a prefix of another (keeps flag namespacing unambiguous).

**key**:
The single column whose value identifies a person, declared by each plugin (e.g. `email`,
`phone`). Rows are merged across plugins by matching `key` **value**. Plugins return
already-normalized key values; the framework joins on the exact string.
_Avoid_: id, identifier, join column.

**fields**:
The list of output column names a plugin declares up front (excluding `key`). With `name`, these
fix the plugin's CSV columns regardless of which rows are found. Column for field `f` of plugin
`p` is `{p}_{f}`.

**search(terms, opts)**:
A plugin's async method, called **once** per run, that returns a list of row dicts. Each row's
keys must equal `{key} ∪ set(fields)` exactly — no extras, none missing; a field with no value is
explicit `None`. The plugin applies its own filter/sort/limit (from `opts`) inside this method.

**Contract violation**:
A returned row whose keys do not exactly match `{key} ∪ fields`. This is a plugin bug, not a data
miss, so it is **fatal** — the run aborts (`ContractViolation`). Runtime failures (a plugin
raising any other exception) are logged and never abort the run; that plugin just contributes no
rows.

**Merge**:
Combining per-plugin rows into one table. Plugins sharing a `key` type join into one row when
their key values match (`{plugin}_{field}` columns namespaced, `key` column emitted once). Plugins
with **different** key types cannot join: the output is a sparse **stacked** table with a column
per distinct key type, each row filling only its own plugin's columns and leaving the rest blank.
When some plugins share a key type and others don't, merge happens **within** each key type, then
the groups are unioned (grouped-merge-then-stack).

**Run summary**:
End-of-run line on stderr counting per-plugin outcomes (ok / failed / rows) across the run.

## Relationships

- A run selects one or more **Plugins** by `name` via `--platforms`.
- Each **Plugin** declares exactly one **key** and zero or more **fields**.
- **Merge** joins rows across **Plugins** that share a **key** type; mismatched key types stack.
- A **Contract violation** aborts the run; any other **Plugin** failure is isolated.

## Example dialogue

> **Dev:** "Two plugins both key on `email` and find `a@x.com` — one row or two?"
> **Domain expert:** "One. Same key type, same value, so their columns merge into a single row."
>
> **Dev:** "And if one keys on `email` and the other on `phone`?"
> **Domain expert:** "They can't be the same person as far as we know, so they stack — two rows,
> each filling only its own key and field columns, the other side blank."
>
> **Dev:** "A plugin returns a row missing one of its declared fields?"
> **Domain expert:** "That's a contract violation — the whole run aborts. A network timeout is
> different: log it, that plugin just adds nothing, the run continues."

## Flagged ambiguities

- "key" means the **identity join column** (email/phone), never a dict key in general or an API
  key. Plugin API credentials come from `.env`, not from `key`.
- Key-value normalization (e.g. email casing) is the **plugin's** responsibility; the framework
  joins on exact strings and stays key-type-agnostic.
