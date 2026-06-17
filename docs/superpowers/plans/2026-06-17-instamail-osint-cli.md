# InstaMail OSINT CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a pluggable async CLI that enriches a list of emails into a single combined CSV via drop-in per-platform plugins.

**Architecture:** A `src/` layout package. The CLI cleans emails, the loader discovers `BasePlugin` subclasses from a `./plugins/` directory, the runner fans out `email × plugin` async tasks gated by per-plugin semaphores and per-fetch timeouts, and the writer streams namespaced rows to a CSV that doubles as the autoresume journal. No example plugin ships.

**Tech Stack:** Python ≥3.14, asyncio, `email-validator`; tests with `pytest` + `pytest-asyncio`. Stdlib `csv`, `json`, `argparse`, `logging`, `importlib`.

## Global Constraints

- Python `requires-python >=3.14`; the venv at `.venv/` is Python 3.14.
- Sole runtime dependency: `email-validator`. Dev deps: `pytest`, `pytest-asyncio`. Plugins bring their own HTTP clients — the framework adds no HTTP client.
- No global concurrency ceiling; concurrency is per-plugin via `max_concurrency`. There is **no** `--concurrency` flag.
- CSV plugins ordered alphabetically by `name`; fields in declared order. Column for field `f` of plugin `p` is `{p}_{f}`. First column is `email`.
- Cell serialization: scalars (`str`/`int`/`float`/`bool`) as-is; `None` → blank; non-scalars JSON-encoded; non-JSON-serializable → `str()`.
- `fetch` must return a dict whose keys exactly match `fields`. Key mismatch = **contract violation = fatal (abort run)**. Runtime data failures (`not_found`/`error`/`timeout`) never abort the run.
- All loader problems are fatal: duplicate `name`, unknown selected name, empty dir under `--plugins all`, plugin import error.
- Output CSV is the resume journal; on resume the existing header must exactly equal the current selection's expected header or abort. Rows streamed in input order, flushed per row.
- Email cleaning: `email-validator` syntax-only (DNS off), trim, skip blanks, normalize, dedupe case-insensitively keeping first-seen order, log+skip invalid lines.

## File Structure

- `pyproject.toml` — packaging, deps, console script, pytest config.
- `src/instamail/__init__.py` — package marker / version.
- `src/instamail/base.py` — `BasePlugin`, `AccountNotFound`.
- `src/instamail/emails.py` — `clean_emails(lines) -> list[str]`.
- `src/instamail/loader.py` — `load_plugins`, `select_plugins`, `PluginError`.
- `src/instamail/runner.py` — `FetchResult`, `ContractViolation`, `iter_results`.
- `src/instamail/writer.py` — `build_header`, `CsvWriter`, `HeaderMismatch`.
- `src/instamail/cli.py` — `parse_args`, `main`, orchestration.
- `src/instamail/__main__.py` — `python -m instamail` entry.
- `plugins/.gitkeep` — empty drop-in dir.
- `tests/` — one test module per source module + `tests/fixtures/` plugins.

---

### Task 1: Project scaffolding & packaging

**Files:**
- Create: `pyproject.toml`, `src/instamail/__init__.py`, `plugins/.gitkeep`, `tests/__init__.py`, `tests/test_smoke.py`

**Interfaces:**
- Consumes: nothing.
- Produces: importable `instamail` package; `pytest` runnable; `instamail` console script bound to `instamail.cli:main`.

- [ ] **Step 1: Write the failing test**

`tests/test_smoke.py`:
```python
def test_package_imports():
    import instamail
    assert instamail.__version__ == "0.1.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_smoke.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'instamail'`.

- [ ] **Step 3: Write pyproject.toml and package marker**

`pyproject.toml`:
```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "instamail"
version = "0.1.0"
description = "Pluggable OSINT email-enrichment CLI"
requires-python = ">=3.14"
dependencies = ["email-validator>=2"]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[project.scripts]
instamail = "instamail.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

`src/instamail/__init__.py`:
```python
__version__ = "0.1.0"
```
Also create empty `plugins/.gitkeep` and empty `tests/__init__.py`.

- [ ] **Step 4: Install editable + dev deps, run test**

Run: `.venv/bin/pip install -e ".[dev]"` then `.venv/bin/python -m pytest tests/test_smoke.py -v`
Expected: install succeeds; test PASSES.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/instamail/__init__.py plugins/.gitkeep tests/
git commit -m "chore: scaffold instamail package and packaging"
```

---

### Task 2: Plugin contract (`base.py`)

**Files:**
- Create: `src/instamail/base.py`, `tests/test_base.py`

**Interfaces:**
- Produces:
  - `class AccountNotFound(Exception)`.
  - `class BasePlugin` with class attrs `name: str`, `fields: list[str]`, `max_concurrency: int = 5`, `timeout: float = 10.0`, and `async def fetch(self, email: str) -> dict[str, Any]` raising `NotImplementedError`.

- [ ] **Step 1: Write the failing test**

`tests/test_base.py`:
```python
import pytest
from instamail.base import BasePlugin, AccountNotFound


class Demo(BasePlugin):
    name = "demo"
    fields = ["a", "b"]

    async def fetch(self, email):
        return {"a": 1, "b": None}


def test_defaults_and_attrs():
    p = Demo()
    assert p.name == "demo"
    assert p.fields == ["a", "b"]
    assert p.max_concurrency == 5
    assert p.timeout == 10.0


async def test_fetch_runs():
    assert await Demo().fetch("x@y.com") == {"a": 1, "b": None}


async def test_base_fetch_not_implemented():
    with pytest.raises(NotImplementedError):
        await BasePlugin().fetch("x@y.com")


def test_account_not_found_is_exception():
    assert issubclass(AccountNotFound, Exception)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_base.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'instamail.base'`.

- [ ] **Step 3: Write the implementation**

`src/instamail/base.py`:
```python
from typing import Any


class AccountNotFound(Exception):
    """Raised by a plugin when an email cleanly has no account on its platform."""


class BasePlugin:
    """Base class for all enrichment plugins. Subclass and drop into ./plugins/."""

    name: str
    fields: list[str]
    max_concurrency: int = 5
    timeout: float = 10.0

    async def fetch(self, email: str) -> dict[str, Any]:
        """Resolve one email into {field: value}. Keys must exactly match `fields`;
        a field with no value must be None. Raise AccountNotFound on a clean miss."""
        raise NotImplementedError
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_base.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/instamail/base.py tests/test_base.py
git commit -m "feat: add BasePlugin contract and AccountNotFound"
```

---

### Task 3: Email cleaning (`emails.py`)

**Files:**
- Create: `src/instamail/emails.py`, `tests/test_emails.py`

**Interfaces:**
- Consumes: `email-validator`.
- Produces: `def clean_emails(lines: Iterable[str]) -> list[str]` — returns normalized, deduped (case-insensitive, first-seen order) valid emails; logs and skips blanks and invalid lines.

- [ ] **Step 1: Write the failing test**

`tests/test_emails.py`:
```python
from instamail.emails import clean_emails


def test_trims_and_skips_blanks():
    assert clean_emails(["  a@x.com  ", "", "   ", "b@x.com"]) == ["a@x.com", "b@x.com"]


def test_dedupes_case_insensitively_keeping_first_seen():
    assert clean_emails(["Alice@X.com", "alice@x.com", "c@x.com"]) == ["alice@x.com", "c@x.com"]


def test_normalizes_domain_case():
    # email-validator lowercases the domain
    assert clean_emails(["user@EXAMPLE.COM"]) == ["user@example.com"]


def test_skips_invalid_lines(caplog):
    out = clean_emails(["good@x.com", "not-an-email", "also bad@@x"])
    assert out == ["good@x.com"]
    assert "not-an-email" in caplog.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_emails.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'instamail.emails'`.

- [ ] **Step 3: Write the implementation**

`src/instamail/emails.py`:
```python
import logging
from typing import Iterable

from email_validator import EmailNotValidError, validate_email

log = logging.getLogger(__name__)


def clean_emails(lines: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            normalized = validate_email(line, check_deliverability=False).normalized
        except EmailNotValidError as e:
            log.warning("skipping invalid email %r: %s", line, e)
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_emails.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/instamail/emails.py tests/test_emails.py
git commit -m "feat: add email cleaning and normalization"
```

---

### Task 4: Plugin loader & selection (`loader.py`)

**Files:**
- Create: `src/instamail/loader.py`, `tests/test_loader.py`, `tests/fixtures/__init__.py`

**Interfaces:**
- Consumes: `BasePlugin` from `base.py`.
- Produces:
  - `class PluginError(Exception)` — fatal loader/selection error.
  - `def load_plugins(plugins_dir: Path) -> dict[str, BasePlugin]` — imports every `.py` in the dir, instantiates each `BasePlugin` subclass, keyed by `name`; raises `PluginError` on import failure or duplicate `name`.
  - `def select_plugins(registry: dict[str, BasePlugin], selection: str) -> list[BasePlugin]` — `selection` is `"all"` or comma list; returns list sorted by `name`; raises `PluginError` on unknown name or empty result.

- [ ] **Step 1: Write the failing test**

`tests/test_loader.py`:
```python
import textwrap
from pathlib import Path

import pytest

from instamail.loader import PluginError, load_plugins, select_plugins


def _write_plugin(dir_: Path, filename: str, body: str) -> None:
    (dir_ / filename).write_text(textwrap.dedent(body))


GOOD = """
    from instamail.base import BasePlugin
    class P(BasePlugin):
        name = "{name}"
        fields = ["x"]
        async def fetch(self, email):
            return {{"x": 1}}
"""


def test_loads_and_keys_by_name(tmp_path):
    _write_plugin(tmp_path, "a.py", GOOD.format(name="alpha"))
    _write_plugin(tmp_path, "b.py", GOOD.format(name="beta"))
    reg = load_plugins(tmp_path)
    assert set(reg) == {"alpha", "beta"}


def test_duplicate_name_is_fatal(tmp_path):
    _write_plugin(tmp_path, "a.py", GOOD.format(name="dup"))
    _write_plugin(tmp_path, "b.py", GOOD.format(name="dup"))
    with pytest.raises(PluginError, match="dup"):
        load_plugins(tmp_path)


def test_import_error_is_fatal(tmp_path):
    (tmp_path / "bad.py").write_text("import this_module_does_not_exist_xyz\n")
    with pytest.raises(PluginError, match="bad.py"):
        load_plugins(tmp_path)


def test_select_all_sorted(tmp_path):
    _write_plugin(tmp_path, "a.py", GOOD.format(name="beta"))
    _write_plugin(tmp_path, "b.py", GOOD.format(name="alpha"))
    reg = load_plugins(tmp_path)
    assert [p.name for p in select_plugins(reg, "all")] == ["alpha", "beta"]


def test_select_unknown_is_fatal(tmp_path):
    _write_plugin(tmp_path, "a.py", GOOD.format(name="alpha"))
    reg = load_plugins(tmp_path)
    with pytest.raises(PluginError, match="unknown"):
        select_plugins(reg, "nope")


def test_select_empty_all_is_fatal(tmp_path):
    with pytest.raises(PluginError, match="no plugins"):
        select_plugins(load_plugins(tmp_path), "all")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_loader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'instamail.loader'`.

- [ ] **Step 3: Write the implementation**

`src/instamail/loader.py`:
```python
import importlib.util
import inspect
from pathlib import Path

from instamail.base import BasePlugin


class PluginError(Exception):
    """Fatal plugin loading or selection error."""


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"instamail_plugin_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise PluginError(f"could not load plugin file: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:  # import-time failure must be fatal and named
        raise PluginError(f"failed to import plugin {path.name}: {e}") from e
    return module


def load_plugins(plugins_dir: Path) -> dict[str, BasePlugin]:
    registry: dict[str, BasePlugin] = {}
    if not plugins_dir.is_dir():
        return registry
    for path in sorted(plugins_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module = _load_module(path)
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BasePlugin) and obj is not BasePlugin and obj.__module__ == module.__name__:
                name = obj.name
                if name in registry:
                    raise PluginError(f"duplicate plugin name {name!r} in {path.name}")
                registry[name] = obj()
    return registry


def select_plugins(registry: dict[str, BasePlugin], selection: str) -> list[BasePlugin]:
    if selection == "all":
        chosen = list(registry.values())
        if not chosen:
            raise PluginError("no plugins found in plugins directory")
        return sorted(chosen, key=lambda p: p.name)
    names = [s.strip() for s in selection.split(",") if s.strip()]
    chosen = []
    for name in names:
        if name not in registry:
            raise PluginError(f"unknown plugin {name!r}; available: {sorted(registry) or 'none'}")
        chosen.append(registry[name])
    if not chosen:
        raise PluginError("no plugins selected")
    return sorted(chosen, key=lambda p: p.name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_loader.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/instamail/loader.py tests/test_loader.py tests/fixtures/__init__.py
git commit -m "feat: add plugin loader and selection with fail-fast errors"
```

---

### Task 5: Async runner (`runner.py`)

**Files:**
- Create: `src/instamail/runner.py`, `tests/test_runner.py`

**Interfaces:**
- Consumes: `BasePlugin`, `AccountNotFound`.
- Produces:
  - `@dataclass class FetchResult` with `email: str`, `plugin: str`, `status: str` (`"ok"`/`"not_found"`/`"error"`), `data: dict | None`, `message: str | None`.
  - `class ContractViolation(Exception)` — fatal.
  - `async def iter_results(emails: list[str], plugins: list[BasePlugin], lookahead: int = 500) -> AsyncIterator[tuple[str, dict[str, FetchResult]]]` — yields `(email, {plugin_name: FetchResult})` in input order; per-plugin `asyncio.Semaphore(max_concurrency)`; each fetch wrapped in `asyncio.wait_for(timeout)`; raises `ContractViolation` (cancelling outstanding tasks) on a key-mismatch.

- [ ] **Step 1: Write the failing test**

`tests/test_runner.py`:
```python
import asyncio

import pytest

from instamail.base import AccountNotFound, BasePlugin
from instamail.runner import ContractViolation, FetchResult, iter_results


class Ok(BasePlugin):
    name = "ok"
    fields = ["x"]
    async def fetch(self, email):
        return {"x": email}


class Missing(BasePlugin):
    name = "missing"
    fields = ["x"]
    async def fetch(self, email):
        raise AccountNotFound("nope")


class Boom(BasePlugin):
    name = "boom"
    fields = ["x"]
    async def fetch(self, email):
        raise ValueError("kaboom")


class Slow(BasePlugin):
    name = "slow"
    fields = ["x"]
    timeout = 0.05
    async def fetch(self, email):
        await asyncio.sleep(1)
        return {"x": 1}


class Bad(BasePlugin):
    name = "bad"
    fields = ["x"]
    async def fetch(self, email):
        return {"y": 1}  # wrong key


async def _collect(emails, plugins):
    return [pair async for pair in iter_results(emails, plugins)]


async def test_ok_results_in_input_order():
    rows = await _collect(["b@x.com", "a@x.com"], [Ok()])
    assert [e for e, _ in rows] == ["b@x.com", "a@x.com"]
    assert rows[0][1]["ok"].status == "ok"
    assert rows[0][1]["ok"].data == {"x": "b@x.com"}


async def test_not_found_and_error_classified():
    rows = await _collect(["a@x.com"], [Missing(), Boom()])
    res = rows[0][1]
    assert res["missing"].status == "not_found"
    assert res["boom"].status == "error" and "kaboom" in res["boom"].message


async def test_timeout_is_error():
    rows = await _collect(["a@x.com"], [Slow()])
    assert rows[0][1]["slow"].status == "error"
    assert "timeout" in rows[0][1]["slow"].message


async def test_contract_violation_is_fatal():
    with pytest.raises(ContractViolation, match="bad"):
        await _collect(["a@x.com"], [Bad()])


async def test_per_plugin_concurrency_respected():
    class Counter(BasePlugin):
        name = "counter"
        fields = ["x"]
        max_concurrency = 2
        live = 0
        peak = 0
        async def fetch(self, email):
            type(self).live += 1
            type(self).peak = max(type(self).peak, type(self).live)
            await asyncio.sleep(0.01)
            type(self).live -= 1
            return {"x": 1}
    c = Counter()
    await _collect([f"{i}@x.com" for i in range(10)], [c])
    assert Counter.peak <= 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'instamail.runner'`.

- [ ] **Step 3: Write the implementation**

`src/instamail/runner.py`:
```python
import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator

from instamail.base import AccountNotFound, BasePlugin


class ContractViolation(Exception):
    """A plugin returned a dict whose keys do not match its declared fields. Fatal."""


@dataclass
class FetchResult:
    email: str
    plugin: str
    status: str            # "ok" | "not_found" | "error"
    data: dict[str, Any] | None
    message: str | None


async def _run_one(plugin: BasePlugin, sem: asyncio.Semaphore, email: str) -> FetchResult:
    async with sem:
        try:
            result = await asyncio.wait_for(plugin.fetch(email), timeout=plugin.timeout)
        except AccountNotFound as e:
            return FetchResult(email, plugin.name, "not_found", None, str(e) or "not_found")
        except asyncio.TimeoutError:
            return FetchResult(email, plugin.name, "error", None, f"timeout after {plugin.timeout}s")
        except Exception as e:
            return FetchResult(email, plugin.name, "error", None, f"{type(e).__name__}: {e}")
    if set(result.keys()) != set(plugin.fields):
        extra = sorted(set(result) - set(plugin.fields))
        missing = sorted(set(plugin.fields) - set(result))
        raise ContractViolation(
            f"plugin {plugin.name!r} returned bad keys for {email}: extra={extra} missing={missing}"
        )
    return FetchResult(email, plugin.name, "ok", result, None)


async def iter_results(
    emails: list[str], plugins: list[BasePlugin], lookahead: int = 500
) -> AsyncIterator[tuple[str, dict[str, FetchResult]]]:
    sems = {p.name: asyncio.Semaphore(p.max_concurrency) for p in plugins}
    inflight: dict[str, list[tuple[str, asyncio.Task]]] = {}

    def schedule(email: str) -> None:
        inflight[email] = [
            (p.name, asyncio.create_task(_run_one(p, sems[p.name], email))) for p in plugins
        ]

    n = len(emails)
    nxt = 0
    while nxt < min(lookahead, n):
        schedule(emails[nxt])
        nxt += 1

    try:
        for email in emails:
            row = {name: await task for name, task in inflight.pop(email)}
            yield email, row
            if nxt < n:
                schedule(emails[nxt])
                nxt += 1
    except BaseException:
        for tasks in inflight.values():
            for _, t in tasks:
                t.cancel()
        raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_runner.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/instamail/runner.py tests/test_runner.py
git commit -m "feat: add async runner with per-plugin concurrency and ordered output"
```

---

### Task 6: CSV writer & autoresume (`writer.py`)

**Files:**
- Create: `src/instamail/writer.py`, `tests/test_writer.py`

**Interfaces:**
- Consumes: `BasePlugin`, `FetchResult`.
- Produces:
  - `def build_header(plugins: list[BasePlugin]) -> list[str]` — `["email"] + [f"{p.name}_{f}" ...]`, plugins sorted by name.
  - `class HeaderMismatch(Exception)`.
  - `class CsvWriter`: `__init__(path, plugins)`; `expected_header` property; `already_processed() -> set[str]` (reads existing email column, raises `HeaderMismatch` on header diff); `open()`; `write_row(email, results: dict[str, FetchResult])`; `close()`. Sorts plugins by name internally.

- [ ] **Step 1: Write the failing test**

`tests/test_writer.py`:
```python
import csv

from instamail.base import BasePlugin
from instamail.runner import FetchResult
from instamail.writer import CsvWriter, HeaderMismatch, build_header
import pytest


class A(BasePlugin):
    name = "alpha"
    fields = ["id", "n"]


class B(BasePlugin):
    name = "beta"
    fields = ["tags"]


def _ok(plugin, email, data):
    return FetchResult(email, plugin, "ok", data, None)


def _miss(plugin, email):
    return FetchResult(email, plugin, "not_found", None, "not_found")


def test_header_alphabetical_namespaced():
    assert build_header([B(), A()]) == ["email", "alpha_id", "alpha_n", "beta_tags"]


def test_writes_scalar_none_and_json(tmp_path):
    out = tmp_path / "o.csv"
    w = CsvWriter(out, [A(), B()])
    w.open()
    w.write_row("a@x.com", {
        "alpha": _ok("alpha", "a@x.com", {"id": 7, "n": None}),
        "beta": _ok("beta", "a@x.com", {"tags": ["x", "y"]}),
    })
    w.close()
    rows = list(csv.reader(out.open()))
    assert rows[0] == ["email", "alpha_id", "alpha_n", "beta_tags"]
    assert rows[1] == ["a@x.com", "7", "", '["x", "y"]']


def test_failed_plugin_blank_cells(tmp_path):
    out = tmp_path / "o.csv"
    w = CsvWriter(out, [A()])
    w.open()
    w.write_row("a@x.com", {"alpha": _miss("alpha", "a@x.com")})
    w.close()
    rows = list(csv.reader(out.open()))
    assert rows[1] == ["a@x.com", "", ""]


def test_already_processed_reads_emails(tmp_path):
    out = tmp_path / "o.csv"
    w = CsvWriter(out, [A()])
    w.open()
    w.write_row("a@x.com", {"alpha": _ok("alpha", "a@x.com", {"id": 1, "n": 2})})
    w.close()
    assert CsvWriter(out, [A()]).already_processed() == {"a@x.com"}


def test_header_mismatch_on_resume_is_fatal(tmp_path):
    out = tmp_path / "o.csv"
    w = CsvWriter(out, [A()])
    w.open(); w.close()
    with pytest.raises(HeaderMismatch):
        CsvWriter(out, [A(), B()]).already_processed()


def test_no_file_means_nothing_processed(tmp_path):
    assert CsvWriter(tmp_path / "missing.csv", [A()]).already_processed() == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_writer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'instamail.writer'`.

- [ ] **Step 3: Write the implementation**

`src/instamail/writer.py`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_writer.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/instamail/writer.py tests/test_writer.py
git commit -m "feat: add streaming CSV writer with autoresume header guard"
```

---

### Task 7: CLI orchestration & entry points (`cli.py`, `__main__.py`)

**Files:**
- Create: `src/instamail/cli.py`, `src/instamail/__main__.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `clean_emails`, `load_plugins`, `select_plugins`, `PluginError`, `iter_results`, `CsvWriter`, `HeaderMismatch`.
- Produces: `def parse_args(argv=None)`; `def main(argv=None) -> int`. `python -m instamail` and the `instamail` console script both call `main`.

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:
```python
import csv
import textwrap
from pathlib import Path

from instamail.cli import main

GOOD = """
    from instamail.base import BasePlugin
    class P(BasePlugin):
        name = "demo"
        fields = ["upper"]
        async def fetch(self, email):
            return {"upper": email.upper()}
"""


def _setup(tmp_path):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "demo.py").write_text(textwrap.dedent(GOOD))
    inp = tmp_path / "emails.txt"
    inp.write_text("a@x.com\nA@X.com\nbad-line\n\nb@x.com\n")
    return pdir, inp


def test_end_to_end(tmp_path):
    pdir, inp = _setup(tmp_path)
    out = tmp_path / "out.csv"
    rc = main(["-i", str(inp), "-o", str(out), "--plugins-dir", str(pdir), "--plugins", "all"])
    assert rc == 0
    rows = list(csv.reader(out.open()))
    assert rows[0] == ["email", "demo_upper"]
    # invalid + duplicate removed; input order preserved
    assert rows[1] == ["a@x.com", "A@X.COM"]
    assert rows[2] == ["b@x.com", "B@X.COM"]
    assert len(rows) == 3


def test_autoresume_skips_done(tmp_path):
    pdir, inp = _setup(tmp_path)
    out = tmp_path / "out.csv"
    main(["-i", str(inp), "-o", str(out), "--plugins-dir", str(pdir)])
    # second run appends nothing new
    main(["-i", str(inp), "-o", str(out), "--plugins-dir", str(pdir)])
    rows = list(csv.reader(out.open()))
    assert len(rows) == 3  # header + 2 unique emails, not duplicated


def test_list_plugins(tmp_path, capsys):
    pdir, _ = _setup(tmp_path)
    rc = main(["--list-plugins", "--plugins-dir", str(pdir)])
    assert rc == 0
    assert "demo" in capsys.readouterr().out


def test_unknown_plugin_returns_nonzero(tmp_path):
    pdir, inp = _setup(tmp_path)
    rc = main(["-i", str(inp), "-o", str(tmp_path / "o.csv"),
               "--plugins-dir", str(pdir), "--plugins", "nope"])
    assert rc != 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'instamail.cli'`.

- [ ] **Step 3: Write the implementation**

`src/instamail/cli.py`:
```python
import argparse
import asyncio
import logging
import sys
from collections import Counter
from pathlib import Path

from instamail.emails import clean_emails
from instamail.loader import PluginError, load_plugins, select_plugins
from instamail.runner import iter_results
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

    emails = clean_emails(Path(args.input).read_text().splitlines())
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
    finally:
        writer.close()

    log.warning(
        "Processed %d emails x %d plugins: %d ok, %d not_found, %d error",
        len(todo), len(plugins), counts["ok"], counts["not_found"], counts["error"],
    )
    return 0
```

`src/instamail/__main__.py`:
```python
import sys

from instamail.cli import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -v`
Expected: ALL pass (smoke, base, emails, loader, runner, writer, cli).

- [ ] **Step 5: Commit**

```bash
git add src/instamail/cli.py src/instamail/__main__.py tests/test_cli.py
git commit -m "feat: add CLI orchestration, entry points, and autoresume wiring"
```

---

### Task 8: README & docs polish

**Files:**
- Create: `README.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Write README**

`README.md` covering: what it is, install (`pip install -e ".[dev]"`), usage (`instamail -i emails.txt --plugins all`), writing a plugin (subclass `BasePlugin`, set `name`/`fields`/`max_concurrency`/`timeout`, implement `async def fetch`, drop file in `./plugins/`), the exact-keys contract and `AccountNotFound`, autoresume behavior, and a worked plugin example. Reference `CONTEXT.md` and `docs/adr/`.

- [ ] **Step 2: Verify usage snippet runs**

Run: `.venv/bin/python -m instamail --list-plugins --plugins-dir plugins`
Expected: exits 0, prints nothing (empty `plugins/`).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: add README with plugin authoring guide"
```

---

## Self-Review

**Spec coverage:**
- Plugin contract (name/fields/max_concurrency/timeout/fetch) → Task 2. ✓
- Email cleaning (validate/normalize/dedupe-ci/skip) → Task 3. ✓
- Loader fail-fast (dup/unknown/empty/import) + `--list-plugins` → Tasks 4, 7. ✓
- Per-plugin semaphore, no global cap, timeout, outcome classes, contract-violation fatal → Task 5. ✓
- Alphabetical namespaced header, scalar/None/JSON serialization, streaming flush → Task 6. ✓
- Autoresume via output CSV + header-match guard → Tasks 6, 7. ✓
- CLI flags (`-i`,`-o`,`--plugins`,`--plugins-dir`,`-v`,`--list-plugins`), no `--concurrency`, stderr logging + summary → Task 7. ✓
- Packaging (src layout, console script, `python -m`, requires-python 3.14, email-validator) → Task 1. ✓
- Tests per module + fixtures → every task. ✓

**Placeholder scan:** none — all steps carry real code/commands.

**Type consistency:** `FetchResult(email, plugin, status, data, message)` defined in Task 5 and consumed identically in Tasks 6/7. `iter_results` / `clean_emails` / `load_plugins` / `select_plugins` / `CsvWriter` signatures match across producer and consumer tasks. `expected_header` used consistently in Task 6.

**Known caveat:** the ordered window holds up to `lookahead` (500) emails' results in memory; a single slow email near its timeout delays rows behind it. This is the deliberate ordered-output trade from the spec; bounded by `lookahead` and per-plugin `timeout`.
