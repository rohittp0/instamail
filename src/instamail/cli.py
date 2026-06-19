"""Command-line entry point: two-phase argparse, then run -> merge -> write.

Phase 1 leniently parses only what's needed to *select* plugins. Phase 2 builds the full parser,
lets each selected plugin register its (name-prefixed) args, and strictly parses everything. Each
plugin then receives its own args un-prefixed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from argparse import Namespace

from dotenv import load_dotenv

from .base import ContractViolation
from .loader import LoaderError, discover_plugins, select_plugins
from .merge import merge
from .runner import run
from .writer import write

log = logging.getLogger("instamail")


class _PrefixProxy:
    """Wraps an argparse group so a plugin's unprefixed flags become ``--{name}-flag``.

    Forces an explicit prefixed ``dest`` and records each ``(clean_dest, prefixed_dest)`` pair so
    the parsed namespace can be de-prefixed back to what the plugin declared — by exact membership,
    never by string-prefix matching.
    """

    def __init__(self, group, prefix: str):
        self._group = group
        self._prefix = prefix
        self.dest_pairs: list[tuple[str, str]] = []

    def add_argument(self, *flags, **kwargs):
        long_flags = [f for f in flags if f.startswith("--")]
        if not long_flags:
            raise ValueError(
                f"plugin {self._prefix!r} must declare a long --flag (got {flags!r}); "
                "short options can't be namespaced"
            )
        if any(not f.startswith("--") for f in flags):
            raise ValueError(
                f"plugin {self._prefix!r} flag {flags!r}: only long --flags are supported"
            )
        clean = kwargs.get("dest") or long_flags[0][2:].replace("-", "_")
        prefixed = f"{self._prefix}_{clean}"
        kwargs["dest"] = prefixed
        new_flags = [f"--{self._prefix}-{f[2:]}" for f in flags]
        self.dest_pairs.append((clean, prefixed))
        return self._group.add_argument(*new_flags, **kwargs)


def _add_globals(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--platforms", default="all",
                        help="comma-separated plugin names, or 'all'")
    parser.add_argument("--plugins-dir", default="./plugins",
                        help="directory to discover plugins from")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    argv = list(sys.argv[1:] if argv is None else argv)

    # Phase 1: just enough to know which plugins, from where. Lenient by design.
    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    _add_globals(pre)
    pre_ns, _ = pre.parse_known_args(argv)

    try:
        discovered = discover_plugins(pre_ns.plugins_dir)
        selected = select_plugins(discovered, pre_ns.platforms)
    except LoaderError as exc:
        print(f"instamail: {exc}", file=sys.stderr)
        return 2

    # Phase 2: full, strict parser. Plugins register their (prefixed) args here.
    parser = argparse.ArgumentParser(prog="instamail", allow_abbrev=False)
    parser.add_argument("terms", help="search terms")
    _add_globals(parser)
    parser.add_argument("-o", "--output", default="out.csv", help="output CSV path")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="log per-plugin successes too")
    proxies: dict[str, _PrefixProxy] = {}
    for plugin in selected:
        proxy = _PrefixProxy(parser.add_argument_group(plugin.name), plugin.name)
        plugin.add_arguments(proxy)
        proxies[plugin.name] = proxy
    ns = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if ns.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    opts_by_plugin = {
        p.name: Namespace(**{clean: getattr(ns, prefixed)
                             for clean, prefixed in proxies[p.name].dest_pairs})
        for p in selected
    }

    try:
        rows_by_plugin = asyncio.run(run(selected, ns.terms, opts_by_plugin))
    except ContractViolation as exc:
        log.error("contract violation: %s", exc)
        return 1

    header, rows = merge(selected, rows_by_plugin)
    write(header, rows, ns.output)
    print(f"instamail: wrote {len(rows)} row(s) from {len(selected)} plugin(s) "
          f"to {ns.output}", file=sys.stderr)
    return 0
