"""sheets_io: read/append shape + service-account decoding (no live Sheets)."""

import base64
import json

import pytest

import sheets_io
from sheets_io import OUTPUT_HEADER, append_output, read_input


class _Cell:
    def __init__(self, value):
        self.value = value


class _WS:
    def __init__(self, a1="email", col1=None):
        self._a1 = a1
        self._col1 = col1 or []
        self.appended = []
        self.updates = []

    def acell(self, addr, value_render_option=None):
        return _Cell(self._a1)

    def col_values(self, n):
        return self._col1

    def get(self, rng):
        return [[v] for v in self._col1]

    def append_rows(self, rows, value_input_option=None):
        self.appended.append((rows, value_input_option))

    def update(self, values=None, range_name=None, value_input_option=None):
        self.updates.append((range_name, values, value_input_option))


class _Spreadsheet:
    def __init__(self, ws_map):
        self._ws = ws_map

    def worksheet(self, title):
        if title in self._ws:
            return self._ws[title]
        raise Exception("WorksheetNotFound")

    def add_worksheet(self, title, rows, cols):
        ws = _WS(a1="")
        self._ws[title] = ws
        return ws


def test_read_input_lowercases_dedups_skips_header_and_blanks():
    ws = _WS(col1=["a@x.com", "", "email", "b@y.com", "A@X.com"])
    ss = _Spreadsheet({"input": ws})
    assert read_input(ss) == ["a@x.com", "b@y.com"]


def test_read_input_limit_caps_kept_emails():
    ws = _WS(col1=["a@x.com", "b@y.com", "c@z.com", "d@w.com"])
    ss = _Spreadsheet({"input": ws})
    assert read_input(ss, limit=2) == ["a@x.com", "b@y.com"]


def test_append_output_converts_none_to_blank_and_uses_raw():
    ws = _WS(a1="email")   # header already present -> not rewritten
    ss = _Spreadsheet({"output": ws})
    row = [None] * len(OUTPUT_HEADER)
    row[0] = "a@x.com"
    n = append_output(ss, [row])
    assert n == 1
    sent_rows, opt = ws.appended[0]
    assert opt == "RAW"
    assert sent_rows[0][0] == "a@x.com"
    assert sent_rows[0][1] == ""          # None -> ""
    assert all(c == "" for c in sent_rows[0][1:])


def test_append_output_empty_is_noop():
    ss = _Spreadsheet({"output": _WS()})
    assert append_output(ss, []) == 0


def test_load_service_account_info_decodes_base64(monkeypatch):
    info = {"type": "service_account", "client_email": "x@y.iam.gserviceaccount.com"}
    encoded = base64.b64encode(json.dumps(info).encode()).decode()
    monkeypatch.setenv(sheets_io.ENV_SERVICE_ACCOUNT, encoded)
    assert sheets_io._load_service_account_info() == info


def test_load_service_account_info_rejects_bad_base64(monkeypatch):
    monkeypatch.setenv(sheets_io.ENV_SERVICE_ACCOUNT, "!!!not-base64!!!")
    with pytest.raises(SystemExit):
        sheets_io._load_service_account_info()


def test_load_service_account_info_missing(monkeypatch):
    monkeypatch.delenv(sheets_io.ENV_SERVICE_ACCOUNT, raising=False)
    with pytest.raises(SystemExit):
        sheets_io._load_service_account_info()
