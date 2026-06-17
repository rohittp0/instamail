import csv

from instamail.base import BasePlugin
from instamail.runner import FetchResult
from instamail.writer import CsvWriter, HeaderMismatch, build_header
import pytest


class A(BasePlugin):
    name = "alpha"
    fields = ["id", "n"]


class B(BasePlugin):
    name = "beta"
    fields = ["tags"]


def _ok(plugin, email, data):
    return FetchResult(email, plugin, "ok", data, None)


def _miss(plugin, email):
    return FetchResult(email, plugin, "not_found", None, "not_found")


def test_header_alphabetical_namespaced():
    assert build_header([B(), A()]) == ["email", "alpha_id", "alpha_n", "beta_tags"]


def test_writes_scalar_none_and_json(tmp_path):
    out = tmp_path / "o.csv"
    w = CsvWriter(out, [A(), B()])
    w.open()
    w.write_row("a@x.com", {
        "alpha": _ok("alpha", "a@x.com", {"id": 7, "n": None}),
        "beta": _ok("beta", "a@x.com", {"tags": ["x", "y"]}),
    })
    w.close()
    rows = list(csv.reader(out.open()))
    assert rows[0] == ["email", "alpha_id", "alpha_n", "beta_tags"]
    assert rows[1] == ["a@x.com", "7", "", '["x", "y"]']


def test_failed_plugin_blank_cells(tmp_path):
    out = tmp_path / "o.csv"
    w = CsvWriter(out, [A()])
    w.open()
    w.write_row("a@x.com", {"alpha": _miss("alpha", "a@x.com")})
    w.close()
    rows = list(csv.reader(out.open()))
    assert rows[1] == ["a@x.com", "", ""]


def test_already_processed_reads_emails(tmp_path):
    out = tmp_path / "o.csv"
    w = CsvWriter(out, [A()])
    w.open()
    w.write_row("a@x.com", {"alpha": _ok("alpha", "a@x.com", {"id": 1, "n": 2})})
    w.close()
    assert CsvWriter(out, [A()]).already_processed() == {"a@x.com"}


def test_header_mismatch_on_resume_is_fatal(tmp_path):
    out = tmp_path / "o.csv"
    w = CsvWriter(out, [A()])
    w.open(); w.close()
    with pytest.raises(HeaderMismatch):
        CsvWriter(out, [A(), B()]).already_processed()


def test_no_file_means_nothing_processed(tmp_path):
    assert CsvWriter(tmp_path / "missing.csv", [A()]).already_processed() == set()
