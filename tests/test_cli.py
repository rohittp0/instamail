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
