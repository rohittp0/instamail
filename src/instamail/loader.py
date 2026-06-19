"""Discover and select plugins from a directory at runtime.

A plugin is either a single ``foo.py`` file or a package ``foo/`` whose ``__init__`` re-exports
its plugin class. Discovery is fail-fast: anything that would silently yield an incomplete CSV
(duplicate names, an unknown selection, an empty dir, an import error, a malformed name) aborts.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from pathlib import Path

from .base import BasePlugin

_NAME_RE = re.compile(r"^[a-z0-9_]+$")


class LoaderError(Exception):
    """A fatal plugin-discovery or selection error."""


def _load_module_from_path(path: Path):
    """Import a .py file or package dir by file path under a unique synthetic module name."""
    if path.is_dir():
        target = path / "__init__.py"
        mod_name = f"_instamail_plugin_{path.name}"
        search_locations = [str(path)]  # makes it a package so relative imports resolve
    else:
        target = path
        mod_name = f"_instamail_plugin_{path.stem}"
        search_locations = None

    spec = importlib.util.spec_from_file_location(
        mod_name, target, submodule_search_locations=search_locations
    )
    if spec is None or spec.loader is None:
        raise LoaderError(f"cannot load plugin at {path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so intra-package relative imports resolve.
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # import-time failure is fatal
        raise LoaderError(f"failed to import plugin at {path}: {exc}") from exc
    return module


def _plugin_class(module) -> type[BasePlugin] | None:
    """Return the single BasePlugin subclass defined/exported by ``module``, if any."""
    found = [
        obj
        for obj in vars(module).values()
        if inspect.isclass(obj) and issubclass(obj, BasePlugin) and obj is not BasePlugin
    ]
    if not found:
        return None
    if len(found) > 1:
        raise LoaderError(
            f"plugin module {module.__name__} exports multiple plugin classes: "
            f"{', '.join(c.__name__ for c in found)}"
        )
    return found[0]


def discover_plugins(plugins_dir: str | Path) -> dict[str, BasePlugin]:
    """Discover every plugin under ``plugins_dir``, returning name -> instance.

    Ignores entries beginning with ``_`` (and ``__pycache__``) and a top-level ``tests`` dir.
    Fatal on duplicate ``name``, malformed ``name``, or a name that is a prefix of another.
    """
    root = Path(plugins_dir)
    if not root.is_dir():
        raise LoaderError(f"plugins dir does not exist: {root}")

    instances: dict[str, BasePlugin] = {}
    for entry in sorted(root.iterdir()):
        if entry.name.startswith((".", "_")) or entry.name == "tests":
            continue
        if entry.is_dir():
            if not (entry / "__init__.py").exists():
                continue
        elif entry.suffix != ".py":
            continue

        cls = _plugin_class(_load_module_from_path(entry))
        if cls is None:
            continue

        name = cls.name
        if not _NAME_RE.match(name or ""):
            raise LoaderError(f"plugin in {entry} has invalid name {name!r} (need [a-z0-9_]+)")
        if name in instances:
            raise LoaderError(f"duplicate plugin name {name!r}")
        instances[name] = cls()

    _check_no_prefix_collisions(list(instances))
    return instances


def _check_no_prefix_collisions(names: list[str]) -> None:
    """Reject any name that is a prefix of another, which would break flag de-prefixing."""
    for a in names:
        for b in names:
            if a != b and b.startswith(a + "_"):
                raise LoaderError(
                    f"plugin name {a!r} is a prefix of {b!r}; flag namespacing would be ambiguous"
                )


def select_plugins(discovered: dict[str, BasePlugin], platforms: str) -> list[BasePlugin]:
    """Select plugins by the ``--platforms`` value: ``all`` or a comma-separated name list.

    Returns instances sorted alphabetically by ``name`` (the canonical CSV column order). Fatal on
    an empty dir under ``all`` or an unknown name in the selection.
    """
    if platforms.strip() == "all":
        if not discovered:
            raise LoaderError("no plugins found and --platforms all was requested")
        return [discovered[n] for n in sorted(discovered)]

    wanted = [p.strip() for p in platforms.split(",") if p.strip()]
    unknown = [w for w in wanted if w not in discovered]
    if unknown:
        available = ", ".join(sorted(discovered)) or "(none)"
        raise LoaderError(f"unknown platform(s): {', '.join(unknown)}. Available: {available}")
    # De-dup while keeping canonical alphabetical order.
    return [discovered[n] for n in sorted(set(wanted))]
