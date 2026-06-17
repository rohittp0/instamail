import csv
import json
from pathlib import Path
from typing import Any

from instamail.base import BasePlugin
from instamail.runner import FetchResult


class HeaderMismatch(Exception):
    """Existing output CSV header does not match the current plugin selection."""


def build_header(plugins: list[BasePlugin]) -> list[str]:
    cols = ["email"]
    for p in sorted(plugins, key=lambda p: p.name):
        cols.extend(f"{p.name}_{f}" for f in p.fields)
    return cols


def _serialize(value: Any) -> str | int | float:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (str, int, float)):
        return value
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


class CsvWriter:
    def __init__(self, path: Path, plugins: list[BasePlugin]):
        self.path = Path(path)
        self.plugins = sorted(plugins, key=lambda p: p.name)
        self.expected_header = build_header(self.plugins)
        self._fh = None
        self._writer = None

    def already_processed(self) -> set[str]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return set()
        with self.path.open(newline="") as f:
            reader = csv.reader(f)
            try:
                header = next(reader)
            except StopIteration:
                return set()
            if header != self.expected_header:
                raise HeaderMismatch(
                    f"output {self.path} header {header} != expected {self.expected_header}; "
                    "use a new output file for a different plugin set"
                )
            return {row[0] for row in reader if row}

    def open(self) -> None:
        is_new = not self.path.exists() or self.path.stat().st_size == 0
        self._fh = self.path.open("a", newline="")
        self._writer = csv.writer(self._fh)
        if is_new:
            self._writer.writerow(self.expected_header)
            self._fh.flush()

    def write_row(self, email: str, results: dict[str, FetchResult]) -> None:
        row: list[Any] = [email]
        for p in self.plugins:
            res = results.get(p.name)
            if res is not None and res.status == "ok" and res.data is not None:
                row.extend(_serialize(res.data.get(f)) for f in p.fields)
            else:
                row.extend("" for _ in p.fields)
        self._writer.writerow(row)
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
