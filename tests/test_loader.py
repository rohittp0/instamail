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


def _write_package(tmp_path, pkg_name, plugin_name):
    pkg = tmp_path / pkg_name
    pkg.mkdir()
    (pkg / "helper.py").write_text("FIELD = 'x'\n")
    (pkg / "impl.py").write_text(textwrap.dedent(f"""
        from instamail.base import BasePlugin
        from .helper import FIELD  # intra-package import must resolve
        class P(BasePlugin):
            name = "{plugin_name}"
            fields = [FIELD]
            async def fetch(self, email):
                return {{FIELD: 1}}
    """))
    (pkg / "__init__.py").write_text("from .impl import P\n")
    return pkg


def test_loads_package_plugin_via_init_reexport(tmp_path):
    _write_package(tmp_path, "pkgalpha", "alphapkg")
    reg = load_plugins(tmp_path)
    assert "alphapkg" in reg
    assert reg["alphapkg"].fields == ["x"]  # intra-package import worked


def test_package_named_tests_is_skipped(tmp_path):
    t = tmp_path / "tests"
    t.mkdir()
    (t / "__init__.py").write_text("")
    (t / "test_stuff.py").write_text("x = 1\n")
    assert load_plugins(tmp_path) == {}


def test_underscore_package_is_skipped(tmp_path):
    pkg = tmp_path / "_private"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(textwrap.dedent("""
        from instamail.base import BasePlugin
        class P(BasePlugin):
            name = "secret"
            fields = ["a"]
            async def fetch(self, email):
                return {"a": 1}
    """))
    assert load_plugins(tmp_path) == {}


def test_package_import_error_is_fatal(tmp_path):
    pkg = tmp_path / "pkgbroken"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("import this_module_does_not_exist_xyz\n")
    with pytest.raises(PluginError, match="pkgbroken"):
        load_plugins(tmp_path)
