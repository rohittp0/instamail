"""Merge per-plugin rows into one table, keyed on the shared identity column.

Plugins that share a ``key`` type join on key *value*; plugins with different key types can't
join and stack into a sparse table (grouped-merge-then-stack). Columns are namespaced
``{plugin}_{field}``; each distinct key type is one un-namespaced column emitted once. See
``CONTEXT.md`` for the canonical merge semantics.

Precondition: every row already satisfies its plugin's contract (the runner validated it).
"""

from __future__ import annotations

from .base import BasePlugin


def build_header(plugins: list[BasePlugin]) -> list[str]:
    """Column order: distinct key columns (alphabetical), then plugins alphabetical, fields in
    declared order. Derived from plugin metadata, so empty results still contribute columns."""
    key_columns = sorted({p.key for p in plugins})
    field_columns = [
        f"{p.name}_{field}"
        for p in sorted(plugins, key=lambda p: p.name)
        for field in p.fields
    ]
    return key_columns + field_columns


def merge(plugins: list[BasePlugin], rows_by_plugin: dict[str, list[dict]]) -> tuple[list[str], list[dict]]:
    """Return ``(header, rows)`` where each row is a dict over every header column.

    Rows with a matching key value (same key type) merge into one; an empty/``None`` key value is
    emitted as its own standalone row (never grouped with other empties). A duplicate key value
    within a single plugin is last-wins.
    """
    header = build_header(plugins)
    out_rows: list[dict] = []
    index: dict[tuple[str, object], dict] = {}  # (key_type, key_value) -> merged row

    def new_row() -> dict:
        row = {col: None for col in header}
        out_rows.append(row)
        return row

    for plugin in sorted(plugins, key=lambda p: p.name):
        for data in rows_by_plugin.get(plugin.name, []):
            kv = data[plugin.key]
            if kv is None or kv == "":
                row = new_row()  # unjoinable: stands alone
            else:
                slot = (plugin.key, kv)
                row = index.get(slot)
                if row is None:
                    row = new_row()
                    index[slot] = row
                row[plugin.key] = kv
            for field in plugin.fields:
                row[f"{plugin.name}_{field}"] = data[field]

    return header, out_rows
