from argparse import Namespace

from instamail.loader import discover_plugins

from instagram.pipeline import ROW_FIELDS
from instagram.plugin import InstagramPlugin


class FakePipeline:
    def __init__(self, rows):
        self.rows = rows
        self.ran = None

    async def run(self, terms, opts):
        self.ran = (terms, opts)
        return self.rows


async def test_search_uses_injected_pipeline():
    rows = [{"email": "a@x.com"}]
    plugin = InstagramPlugin(pipeline=FakePipeline(rows))
    out = await plugin.search("travel creator", Namespace())
    assert out == rows


def test_contract_fields_match_row_fields():
    assert InstagramPlugin.key == "email"
    assert InstagramPlugin.fields == list(ROW_FIELDS)


def test_loader_discovers_package_plugin():
    found = discover_plugins("plugins")
    assert "instagram" in found
    assert found["instagram"].key == "email"


def test_add_arguments_registers_expected_flags():
    captured = []

    class Group:
        def add_argument(self, *args, **kwargs):
            captured.append(args[0])

    InstagramPlugin.add_arguments(Group())
    for flag in ("--limit", "--min-followers", "--sort", "--window",
                 "--max-tier", "--travel-only", "--require-email"):
        assert flag in captured
