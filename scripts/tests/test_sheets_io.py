"""sheets_io: append shape, service-account decoding, cursor, and the lease-lock (no live Sheets)."""

import base64
import json

import pytest

import sheets_io
from sheets_io import (
    OUTPUT_HEADER,
    acquire_lock,
    append_claim,
    append_output,
    find_reclaimable,
    get_cursor,
    mark_claim_done,
    read_output_emails,
    reclaim_row,
    release_lock,
    set_cursor,
)


# --- a tiny A1-addressable fake worksheet ----------------------------------

def _col_to_num(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - ord("A") + 1)
    return n


def _num_to_col(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def _parse_cell(addr: str):
    i = 0
    while i < len(addr) and addr[i].isalpha():
        i += 1
    return _col_to_num(addr[:i]), int(addr[i:])


class _Cell:
    def __init__(self, value):
        self.value = value


class _WS:
    """Stores cells by A1 address; writes a values grid starting at a range's first cell."""

    def __init__(self, cells=None):
        self.cells = dict(cells or {})
        self.appended = []

    def acell(self, addr, value_render_option=None):
        return _Cell(self.cells.get(addr))

    def col_values(self, n):
        col = _num_to_col(n)
        rows = sorted(int(a[len(col):]) for a in self.cells if a.startswith(col) and a[len(col):].isdigit())
        return [self.cells.get(f"{col}{r}", "") for r in rows]

    def get(self, rng):
        # e.g. "A2:G" -> rows from row 2 to the last populated row, cols A..G
        start = rng.split(":")[0]
        c0, r0 = _parse_cell(start)
        end_col = "".join(ch for ch in rng.split(":")[1] if ch.isalpha()) or "G"
        c1 = _col_to_num(end_col)
        max_row = max((int("".join(ch for ch in a if ch.isdigit())) for a in self.cells), default=r0 - 1)
        out = []
        for r in range(r0, max_row + 1):
            out.append([self.cells.get(f"{_num_to_col(c)}{r}", "") for c in range(c0, c1 + 1)])
        return out

    def _write(self, range_name, values):
        start = range_name.split(":")[0]
        c0, r0 = _parse_cell(start)
        for dr, row in enumerate(values):
            for dc, val in enumerate(row):
                self.cells[f"{_num_to_col(c0 + dc)}{r0 + dr}"] = val

    def update(self, values=None, range_name=None, value_input_option=None):
        self._write(range_name, values)

    def batch_update(self, data):
        for d in data:
            self._write(d["range"], d["values"])

    def append_rows(self, rows, value_input_option=None):
        self.appended.append((rows, value_input_option))


class _Spreadsheet:
    def __init__(self, ws_map):
        self._ws = ws_map

    def worksheet(self, title):
        if title in self._ws:
            return self._ws[title]
        raise Exception("WorksheetNotFound")

    def add_worksheet(self, title, rows, cols):
        ws = _WS()
        self._ws[title] = ws
        return ws


class _Clock:
    """Virtual clock: now() is frozen, sleep() advances it (so lock loops terminate fast)."""

    def __init__(self, t=1000.0):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


# --- append_output ----------------------------------------------------------

def test_append_output_converts_none_to_blank_and_uses_raw():
    ws = _WS(cells={"A1": "email"})   # header present -> not rewritten
    ss = _Spreadsheet({"output": ws})
    row = [None] * len(OUTPUT_HEADER)
    row[0] = "a@x.com"
    n = append_output(ss, [row])
    assert n == 1
    sent_rows, opt = ws.appended[0]
    assert opt == "RAW"
    assert sent_rows[0][0] == "a@x.com"
    assert all(c == "" for c in sent_rows[0][1:])


def test_append_output_empty_is_noop():
    ss = _Spreadsheet({"output": _WS(cells={"A1": "email"})})
    assert append_output(ss, []) == 0


# --- cursor -----------------------------------------------------------------

def test_cursor_round_trip():
    ws = _WS()
    assert get_cursor(ws) == ""
    set_cursor(ws, "rohit@lascade.com")
    assert get_cursor(ws) == "rohit@lascade.com"


# --- lease-lock -------------------------------------------------------------

def test_acquire_then_blocked_then_released():
    ws = _WS()
    clk = _Clock()
    assert acquire_lock(ws, "A", sleep=clk.sleep, now=clk.now) is True

    # second holder cannot acquire while A's lease is live
    with pytest.raises(SystemExit):
        acquire_lock(ws, "B", timeout=1, sleep=clk.sleep, now=clk.now)

    release_lock(ws, "A")
    assert acquire_lock(ws, "B", sleep=clk.sleep, now=clk.now) is True


def test_expired_lease_can_be_taken_over():
    ws = _WS()
    clk = _Clock()
    acquire_lock(ws, "A", lease=10, sleep=clk.sleep, now=clk.now)
    clk.t += 1000                       # A's lease is now long expired
    assert acquire_lock(ws, "B", sleep=clk.sleep, now=clk.now) is True


def test_release_only_clears_own_token():
    ws = _WS()
    clk = _Clock()
    acquire_lock(ws, "A", sleep=clk.sleep, now=clk.now)
    release_lock(ws, "SOMEONE_ELSE")    # not the holder -> no-op
    assert ws.acell("B2").value == "A"


# --- claims ledger ----------------------------------------------------------

def test_append_claim_and_reclaim_lifecycle():
    cl = _WS(cells={"A1": "claim_id"})   # header present -> first append lands on row 2

    row = append_claim(cl, "c1", "", "b@x", "run1", lease_expires=1100.0, updated_at=1000.0)
    assert row == 2
    assert cl.cells["A2"] == "c1" and cl.cells["E2"] == "in_progress" and cl.cells["C2"] == "b@x"

    # not reclaimable while the lease is live
    assert find_reclaimable(cl, now=1050.0) is None
    # reclaimable once expired
    rec = find_reclaimable(cl, now=2000.0)
    assert rec == (2, "c1", "", "b@x")

    reclaim_row(cl, 2, "run2", lease_expires=2200.0, updated_at=2000.0)
    assert cl.cells["D2"] == "run2" and cl.cells["E2"] == "in_progress"
    assert find_reclaimable(cl, now=2100.0) is None     # new lease live again

    mark_claim_done(cl, 2, updated_at=2300.0)
    assert cl.cells["E2"] == "done"
    assert find_reclaimable(cl, now=9999.0) is None     # done rows are never reclaimed


def test_read_output_emails():
    ws = _WS(cells={"A1": "email", "A2": "a@x.com", "A3": "B@X.com", "A4": ""})
    ss = _Spreadsheet({"output": ws})
    assert read_output_emails(ss) == {"a@x.com", "b@x.com"}


# --- service account --------------------------------------------------------

def test_load_service_account_info_decodes_base64(monkeypatch):
    info = {"type": "service_account", "client_email": "x@y.iam.gserviceaccount.com"}
    encoded = base64.b64encode(json.dumps(info).encode()).decode()
    monkeypatch.setenv(sheets_io.ENV_SERVICE_ACCOUNT, encoded)
    assert sheets_io._load_service_account_info() == info


def test_load_service_account_info_accepts_missing_padding(monkeypatch):
    info = {"type": "service_account"}
    encoded = base64.b64encode(json.dumps(info).encode()).decode().rstrip("=")
    monkeypatch.setenv(sheets_io.ENV_SERVICE_ACCOUNT, encoded)
    assert sheets_io._load_service_account_info() == info


def test_load_service_account_info_accepts_raw_json(monkeypatch):
    info = {"type": "service_account", "client_email": "z@z.iam"}
    monkeypatch.setenv(sheets_io.ENV_SERVICE_ACCOUNT, json.dumps(info))
    assert sheets_io._load_service_account_info() == info


def test_load_service_account_info_missing(monkeypatch):
    monkeypatch.delenv(sheets_io.ENV_SERVICE_ACCOUNT, raising=False)
    with pytest.raises(SystemExit):
        sheets_io._load_service_account_info()
