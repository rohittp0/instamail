from pathlib import Path

import pytest

from instamail.loader import LoaderError, discover_plugins, select_plugins

FIXTURES = Path(__file__).parent / "fixtures"


def _write_plugin(dir_path, filename, name, key="email", fields="['x']", body=None):
    src = body or (
        "from instamail.base import BasePlugin\n"
        f"class P(BasePlugin):\n"
        f"    name = {name!r}\n"
        f"    key = {key!r}\n"
        f"    fields = {fields}\n"
        "    async def search(self, terms, opts):\n"
        "        return []\n"
    )
    (dir_path / filename).write_text(src)


def test_discovers_file_and_package_layouts():
    found = discover_plugins(FIXTURES)
    assert "email_a" in found            # single-file
    assert "pkg" in found                # package re-exporting its class
    assert found["pkg"].fields == ["z"]


def test_select_all_returns_every_plugin_sorted():
    found = discover_plugins(FIXTURES)
    selected = select_plugins(found, "all")
    names = [p.name for p in selected]
    assert names == sorted(names)
    assert set(names) == set(found)


def test_select_subset_by_name():
    found = discover_plugins(FIXTURES)
    selected = select_plugins(found, "email_b,email_a")
    assert [p.name for p in selected] == ["email_a", "email_b"]  # canonical alpha order


def test_unknown_platform_is_fatal():
    found = discover_plugins(FIXTURES)
    with pytest.raises(LoaderError):
        select_plugins(found, "nope")


def test_empty_dir_under_all_is_fatal(tmp_path):
    with pytest.raises(LoaderError):
        select_plugins(discover_plugins(tmp_path), "all")


def test_duplicate_name_is_fatal(tmp_path):
    _write_plugin(tmp_path, "a.py", "dup")
    _write_plugin(tmp_path, "b.py", "dup")
    with pytest.raises(LoaderError):
        discover_plugins(tmp_path)


def test_prefix_name_collision_is_fatal(tmp_path):
    _write_plugin(tmp_path, "a.py", "foo")
    _write_plugin(tmp_path, "b.py", "foo_bar")
    with pytest.raises(LoaderError):
        discover_plugins(tmp_path)


def test_invalid_name_is_fatal(tmp_path):
    _write_plugin(tmp_path, "a.py", "Bad-Name")
    with pytest.raises(LoaderError):
        discover_plugins(tmp_path)


def test_import_error_is_fatal(tmp_path):
    (tmp_path / "broken.py").write_text("import a_module_that_does_not_exist_xyz\n")
    with pytest.raises(LoaderError):
        discover_plugins(tmp_path)
