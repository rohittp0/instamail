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
