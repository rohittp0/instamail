import csv
from pathlib import Path

import pytest

from instamail.cli import main

FIXTURES = str(Path(__file__).parent / "fixtures")


def _read(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def test_end_to_end_merge(tmp_path):
    out = tmp_path / "out.csv"
    rc = main(["fitness", "--platforms", "email_a,email_b",
               "--plugins-dir", FIXTURES, "-o", str(out)])
    assert rc == 0
    rows = _read(out)
    assert list(rows[0].keys()) == ["email", "email_a_name", "email_a_followers", "email_b_handle"]
    a = next(r for r in rows if r["email"] == "a@x.com")
    assert a["email_a_name"] == "Al" and a["email_b_handle"] == "@al"


def test_prefixed_flag_reaches_plugin_unprefixed(tmp_path):
    out = tmp_path / "out.csv"
    main(["x", "--platforms", "email_a", "--plugins-dir", FIXTURES,
          "--email_a-min-followers", "80", "-o", str(out)])
    rows = _read(out)
    # opts.min_followers=80 filtered out Bea (50), kept Al (100)
    assert [r["email"] for r in rows] == ["a@x.com"]


def test_unknown_global_flag_exits_2(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["x", "--platforms", "email_a", "--plugins-dir", FIXTURES, "--nonsense"])
    assert exc.value.code == 2


def test_abbreviation_rejected(tmp_path):
    with pytest.raises(SystemExit):
        main(["x", "--platf", "email_a", "--plugins-dir", FIXTURES])  # allow_abbrev=False


def test_two_plugins_same_arg_name_dont_collide(tmp_path):
    pdir = tmp_path / "plugins"
    pdir.mkdir()
    for name, email in [("p1", "k@x.com"), ("p2", "k@x.com")]:
        (pdir / f"{name}.py").write_text(
            "from instamail.base import BasePlugin\n"
            f"class P(BasePlugin):\n"
            f"    name = {name!r}\n"
            "    key = 'email'\n"
            "    fields = ['lim']\n"
            "    @classmethod\n"
            "    def add_arguments(cls, group):\n"
            "        group.add_argument('--limit', type=int)\n"
            "    async def search(self, terms, opts):\n"
            f"        return [{{'email': {email!r}, 'lim': opts.limit}}]\n"
        )
    out = tmp_path / "out.csv"
    rc = main(["x", "--platforms", "all", "--plugins-dir", str(pdir),
               "--p1-limit", "1", "--p2-limit", "2", "-o", str(out)])
    assert rc == 0
    row = _read(out)[0]
    assert row["p1_lim"] == "1" and row["p2_lim"] == "2"


def test_contract_violation_aborts_without_writing(tmp_path):
    out = tmp_path / "out.csv"
    rc = main(["x", "--platforms", "email_a,violator",
               "--plugins-dir", FIXTURES, "-o", str(out)])
    assert rc == 1
    assert not out.exists()
