import csv

from instamail.writer import _cell, write


def test_cell_serialization():
    assert _cell(None) == ""
    assert _cell("hi") == "hi"
    assert _cell(42) == "42"
    assert _cell(True) == "True"
    assert _cell([1, 2]) == "[1, 2]"
    assert _cell({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'  # sorted keys, JSON


def test_write_roundtrip(tmp_path):
    out = tmp_path / "out.csv"
    header = ["email", "p_name", "p_tags"]
    rows = [
        {"email": "a@x.com", "p_name": "Al", "p_tags": ["x", "y"]},
        {"email": "b@x.com", "p_name": None, "p_tags": None},
    ]
    write(header, rows, str(out))

    with out.open(newline="") as fh:
        got = list(csv.DictReader(fh))
    assert list(got[0].keys()) == header
    assert got[0]["p_tags"] == '["x", "y"]'
    assert got[1]["p_name"] == "" and got[1]["p_tags"] == ""
