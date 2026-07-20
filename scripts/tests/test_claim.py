"""claim: disjoint slices, ledger recording, and crash recovery (parallel-safety + at-least-once).

The lock itself is covered in test_sheets_io; here lock=False and we drive the claims ledger with
fake worksheets to verify (a) cursor advancement yields non-overlapping slices and (b) an expired
in_progress claim is reclaimed before new work is handed out.

Also covers the `outcome` contract (claimed/exhausted/reclaim_empty/drain) each return dict must
carry, and the STOP-file soft-drain signal (ADR 0005) claim.py checks before doing any claiming."""

import json

import claim as claim_module
from claim import claim

USERS = [{"email": e, "name": e[0].upper()} for e in ("a@x", "b@x", "c@x", "d@x", "e@x")]


def _fetch(after=None, limit=100):
    if after is None:
        start = 0
    else:
        start = next((i + 1 for i, u in enumerate(USERS) if u["email"] == after), len(USERS))
    return USERS[start:start + limit]


class _Cell:
    def __init__(self, v):
        self.value = v


class _FakeState:
    def __init__(self):
        self.cells = {}

    def acell(self, addr, value_render_option=None):
        return _Cell(self.cells.get(addr))

    def update(self, values=None, range_name=None, value_input_option=None):
        self.cells[range_name] = values[0][0]


class _FakeClaims:
    """Row-addressable fake of the claims ledger (header in row 1)."""

    def __init__(self):
        self.rows = {}   # row_number -> [7 cells]

    def col_values(self, n):
        last = max(self.rows) if self.rows else 1
        return ["claim_id"] + ["x"] * (last - 1)   # row1 header + a marker per data row

    def get(self, rng):   # "A2:G"
        out = []
        for r in range(2, (max(self.rows) if self.rows else 1) + 1):
            out.append(self.rows.get(r, [""] * 7))
        return out

    def update(self, values=None, range_name=None, value_input_option=None):
        row = int("".join(c for c in range_name.split(":")[0] if c.isdigit()))
        self.rows[row] = list(values[0])

    def batch_update(self, data):
        for d in data:
            cell = d["range"]
            col = "".join(c for c in cell if c.isalpha())
            row = int("".join(c for c in cell if c.isdigit()))
            idx = ord(col.upper()) - ord("A")
            cur = self.rows.get(row, [""] * 7)
            cur[idx] = d["values"][0][0]
            self.rows[row] = cur


def test_sequential_claims_disjoint_and_recorded():
    st, cl = _FakeState(), _FakeClaims()
    clock = [1000.0]

    r1 = claim(st, cl, _fetch, limit=2, token="t1", now=lambda: clock[0], lock=False)
    r2 = claim(st, cl, _fetch, limit=2, token="t2", now=lambda: clock[0], lock=False)

    assert [u["email"] for u in r1["users"]] == ["a@x", "b@x"]
    assert [u["email"] for u in r2["users"]] == ["c@x", "d@x"]
    assert set(e["email"] for e in r1["users"]).isdisjoint(e["email"] for e in r2["users"])
    assert st.cells["B1"] == "d@x"                      # cursor advanced
    assert r1["claim_row"] == 2 and r2["claim_row"] == 3
    assert not r1["reclaimed"] and not r2["reclaimed"]
    assert r1["outcome"] == "claimed" and r2["outcome"] == "claimed"
    # both recorded as in_progress with their ranges
    assert cl.rows[2][4] == "in_progress" and cl.rows[2][1] == "" and cl.rows[2][2] == "b@x"
    assert cl.rows[3][1] == "b@x" and cl.rows[3][2] == "d@x"


def test_expired_claim_is_reclaimed_before_new_work():
    st, cl = _FakeState(), _FakeClaims()
    clock = [1000.0]
    now = lambda: clock[0]

    # claim a@x,b@x with a short lease, then let the lease expire (claimer "died")
    r1 = claim(st, cl, _fetch, limit=2, token="t1", now=now, lease=10, lock=False)
    assert r1["claim_row"] == 2
    clock[0] += 100                                     # lease (1000+10) now expired

    r2 = claim(st, cl, _fetch, limit=2, token="t2", now=now, lock=False)
    assert r2["reclaimed"] is True
    assert r2["claim_row"] == 2                          # same ledger row, re-owned
    assert [u["email"] for u in r2["users"]] == ["a@x", "b@x"]   # re-fetched original range
    assert cl.rows[2][3] == "t2"                         # run_id taken over
    assert r2["outcome"] == "claimed"                     # non-empty reclaim is still "claimed"


def test_reclaim_trimmed_to_zero_is_reclaim_empty():
    st, cl = _FakeState(), _FakeClaims()
    clock = [1000.0]
    now = lambda: clock[0]

    claim(st, cl, _fetch, limit=2, token="t1", now=now, lease=10, lock=False)
    clock[0] += 100                                       # lease expired

    empty_fetch = lambda after=None, limit=100: []        # underlying range no longer resolves
    r2 = claim(st, cl, empty_fetch, limit=2, token="t2", now=now, lock=False)
    assert r2["reclaimed"] is True
    assert r2["users"] == []
    assert r2["outcome"] == "reclaim_empty"


def test_no_recovery_when_lease_live():
    st, cl = _FakeState(), _FakeClaims()
    clock = [1000.0]
    now = lambda: clock[0]

    claim(st, cl, _fetch, limit=2, token="t1", now=now, lease=10000, lock=False)
    r2 = claim(st, cl, _fetch, limit=2, token="t2", now=now, lock=False)
    assert r2["reclaimed"] is False                      # t1's lease still live -> new work
    assert [u["email"] for u in r2["users"]] == ["c@x", "d@x"]


def test_exhausted_and_empty():
    st, cl = _FakeState(), _FakeClaims()
    r = claim(st, cl, _fetch, limit=10, token="t", now=lambda: 1.0, lock=False)
    assert r["exhausted"] is True and len(r["users"]) == 5
    assert r["outcome"] == "claimed"                      # got users -> claimed, even if the page was short
    # cursor now at the end; next claim finds nothing and no reclaimable -> empty/terminal
    r2 = claim(st, cl, _fetch, limit=10, token="t2", now=lambda: 1.0, lock=False)
    assert r2["users"] == [] and r2["claim_id"] is None
    assert r2["outcome"] == "exhausted"                   # genuinely no work left anywhere


def test_stop_check_drains_under_lock_before_any_claiming():
    st, cl = _FakeState(), _FakeClaims()
    r = claim(st, cl, _fetch, limit=2, token="t1", now=lambda: 1000.0, lock=False,
              stop_check=lambda: True)
    assert r == {"users": [], "exhausted": False, "claim_id": None, "claim_row": None,
                 "reclaimed": False, "outcome": "drain"}
    # nothing was claimed: cursor untouched, no ledger row written
    assert "B1" not in st.cells
    assert cl.rows == {}


def test_stop_check_false_claims_normally():
    st, cl = _FakeState(), _FakeClaims()
    r = claim(st, cl, _fetch, limit=2, token="t1", now=lambda: 1000.0, lock=False,
              stop_check=lambda: False)
    assert r["outcome"] == "claimed"


def test_main_drains_when_stop_file_present(tmp_path, capsys):
    """main() must check STOP before open_spreadsheet() — credential-free. If it called
    open_spreadsheet() first it would raise SystemExit here (no env vars in the test env), so a
    clean rc==0 drain result also proves the ordering, not just the output shape."""
    stop_file = tmp_path / "STOP"
    stop_file.write_text("")
    original = claim_module.STOP_FILE
    claim_module.STOP_FILE = stop_file
    try:
        rc = claim_module.main(["5"])
    finally:
        claim_module.STOP_FILE = original
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["outcome"] == "drain"
