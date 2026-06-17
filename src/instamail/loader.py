import importlib
import importlib.util
import inspect
import sys
from pathlib import Path

from instamail.base import BasePlugin


class PluginError(Exception):
    """Fatal plugin loading or selection error."""


def _load_file_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"instamail_plugin_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise PluginError(f"could not load plugin file: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:  # import-time failure must be fatal and named
        raise PluginError(f"failed to import plugin {path.name}: {e}") from e
    return module


def _import_package(name: str):
    try:
        return importlib.import_module(name)
    except Exception as e:
        raise PluginError(f"failed to import plugin package {name!r}: {e}") from e


def _register(module, registry, source, belongs) -> None:
    """Register BasePlugin subclasses found in `module` that `belongs(__module__)`."""
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, BasePlugin) and obj is not BasePlugin and belongs(obj.__module__):
            if obj.name in registry:
                raise PluginError(f"duplicate plugin name {obj.name!r} in {source}")
            registry[obj.name] = obj()


def load_plugins(plugins_dir: Path) -> dict[str, BasePlugin]:
    """Discover plugins under `plugins_dir`.

    Two layouts are supported:
      - a single `foo.py` file defining a BasePlugin subclass, and
      - a package directory `foo/` whose `__init__.py` re-exports its plugin class
        (e.g. `from .plugin import FooPlugin`) — the package is added to sys.path so
        its intra-package imports resolve, and the re-exported class is registered.
    Files/dirs starting with `_`, `__pycache__`, and a top-level `tests` dir are ignored.
    All load failures (import error, duplicate name) are fatal.
    """
    registry: dict[str, BasePlugin] = {}
    if not plugins_dir.is_dir():
        return registry

    resolved = str(plugins_dir.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)

    for entry in sorted(plugins_dir.iterdir()):
        if entry.name.startswith("_") or entry.name in ("__pycache__", "tests"):
            continue
        if entry.is_file() and entry.suffix == ".py":
            module = _load_file_module(entry)
            _register(module, registry, entry.name, lambda m, n=module.__name__: m == n)
        elif entry.is_dir() and (entry / "__init__.py").exists():
            pkg = _import_package(entry.name)
            _register(pkg, registry, entry.name,
                      lambda m, n=entry.name: m == n or m.startswith(n + "."))
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
