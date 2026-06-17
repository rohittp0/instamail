import argparse
import asyncio
import logging
import sys
from collections import Counter
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from instamail.emails import clean_emails
from instamail.loader import PluginError, load_plugins, select_plugins
from instamail.runner import ContractViolation, iter_results
from instamail.writer import CsvWriter, HeaderMismatch

log = logging.getLogger("instamail")


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="instamail", description="OSINT email-enrichment CLI")
    p.add_argument("-i", "--input", help="file of emails, one per line")
    p.add_argument("-o", "--output", default="out.csv", help="output CSV (default: out.csv)")
    p.add_argument("--plugins", default="all", help="'all' or comma-separated plugin names")
    p.add_argument("--plugins-dir", default="plugins", help="plugin directory (default: ./plugins)")
    p.add_argument("--list-plugins", action="store_true", help="list available plugins and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="also log successes")
    return p.parse_args(argv)


async def _drive(writer, emails, plugins, counts):
    async for email, results in iter_results(emails, plugins):
        for res in results.values():
            counts[res.status] += 1
            if res.status != "ok":
                log.warning("%s %s: %s", res.plugin, res.email, res.message)
            else:
                log.debug("%s %s: ok", res.plugin, res.email)
        writer.write_row(email, results)


def main(argv=None) -> int:
    # Load .env first so env vars (e.g. plugin API keys / cookies) are available to every
    # plugin before any plugin is imported or instantiated. Search from the working directory
    # (usecwd) since the installed CLI lives elsewhere; real env vars take precedence.
    load_dotenv(find_dotenv(usecwd=True))
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        registry = load_plugins(Path(args.plugins_dir))
        if args.list_plugins:
            for name in sorted(registry):
                p = registry[name]
                print(f"{name}\tfields={p.fields}\tmax_concurrency={p.max_concurrency}")
            return 0
        plugins = select_plugins(registry, args.plugins)
    except PluginError as e:
        log.error("%s", e)
        return 2

    if not args.input:
        log.error("missing required -i/--input")
        return 2

    input_path = Path(args.input)
    try:
        emails = clean_emails(input_path.read_text().splitlines())
    except OSError as e:
        log.error("cannot read input file %r: %s", str(input_path), e)
        return 2
    writer = CsvWriter(Path(args.output), plugins)
    try:
        done = writer.already_processed()
    except HeaderMismatch as e:
        log.error("%s", e)
        return 2
    todo = [e for e in emails if e not in done]

    writer.open()
    counts: Counter = Counter()
    try:
        asyncio.run(_drive(writer, todo, plugins, counts))
    except ContractViolation as e:
        log.error("%s", e)
        return 2
    finally:
        writer.close()

    log.warning(
        "Processed %d emails x %d plugins: %d ok, %d not_found, %d error",
        len(todo), len(plugins), counts["ok"], counts["not_found"], counts["error"],
    )
    return 0
