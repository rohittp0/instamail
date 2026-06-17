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
