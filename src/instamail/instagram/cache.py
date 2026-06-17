"""Tiny on-disk JSON cache with TTL, used to avoid re-resolving emails and re-fetching
profiles across runs (and to make the framework's autoresume cheap). Keys are hashed so
arbitrary strings (emails, usernames) map to safe filenames."""

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

_MISSING = object()


class JsonCache:
    def __init__(self, directory: Path | str, ttl: float, now: Callable[[], float] = time.time):
        self.directory = Path(directory)
        self.ttl = ttl
        self._now = now

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.json"

    def get(self, key: str, default: Any = None) -> Any:
        path = self._path(key)
        if not path.exists():
            return default
        try:
            entry = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return default
        if self._now() - entry.get("t", 0) > self.ttl:
            return default
        return entry["v"]

    def set(self, key: str, value: Any) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        path.write_text(json.dumps({"t": self._now(), "v": value}))
