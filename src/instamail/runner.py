"""Run the selected plugins concurrently and collect their rows.

Each plugin's :meth:`~instamail.base.BasePlugin.search` is awaited once. Returned rows are
contract-checked immediately: a key mismatch raises :class:`~instamail.base.ContractViolation`
(fatal). Any other exception from a plugin is logged and that plugin contributes no rows — one
platform failing never aborts the run.
"""

from __future__ import annotations

import asyncio
import logging
from argparse import Namespace

from .base import BasePlugin, ContractViolation

log = logging.getLogger("instamail")


def _validate(plugin: BasePlugin, rows: list[dict]) -> None:
    expected = set(plugin.columns)
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != expected:
            got = set(row) if isinstance(row, dict) else type(row).__name__
            raise ContractViolation(
                f"plugin {plugin.name!r} row {i}: keys {got} != expected {expected}"
            )


async def _run_one(plugin: BasePlugin, terms: str, opts: Namespace) -> list[dict]:
    try:
        rows = await plugin.search(terms, opts)
    except ContractViolation:
        raise
    except Exception as exc:
        log.error("plugin %r failed: %s", plugin.name, exc)
        return []
    _validate(plugin, rows)  # outside try: a contract violation must stay fatal
    log.info("plugin %r: %d row(s)", plugin.name, len(rows))
    return rows


async def run(
    plugins: list[BasePlugin], terms: str, opts_by_plugin: dict[str, Namespace]
) -> dict[str, list[dict]]:
    """Run all ``plugins`` concurrently; return name -> validated rows (failed plugins -> [])."""
    results = await asyncio.gather(
        *(_run_one(p, terms, opts_by_plugin[p.name]) for p in plugins)
    )
    return {p.name: rows for p, rows in zip(plugins, results)}
