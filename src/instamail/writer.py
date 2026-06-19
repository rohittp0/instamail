"""Write merged rows to CSV.

Cell serialization: scalars (str/int/float/bool) as-is, ``None`` -> blank, anything else (list /
dict) JSON-encoded. The first row is the header; columns follow merge's deterministic order.
"""

from __future__ import annotations

import csv
import json
import sys
from contextlib import contextmanager


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@contextmanager
def _open(path: str | None):
    if path is None:
        yield sys.stdout
    else:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            yield fh


def write(header: list[str], rows: list[dict], path: str | None = "out.csv") -> None:
    """Write ``rows`` (dicts keyed by ``header`` columns) as CSV to ``path`` (or stdout if None)."""
    with _open(path) as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for row in rows:
            w.writerow([_cell(row.get(col)) for col in header])
