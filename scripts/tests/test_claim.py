"""claim: disjoint slices, ledger recording, and crash recovery (parallel-safety + at-least-once).

The lock itself is covered in test_sheets_io; here lock=False and we drive the claims ledger with
fake worksheets to verify (a) cursor advancement yields non-overlapping slices and (b) an expired
in_progress claim is reclaimed before new work is handed out."""

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
    # cursor now at the end; next claim finds nothing and no reclaimable -> empty/terminal
    r2 = claim(st, cl, _fetch, limit=10, token="t2", now=lambda: 1.0, lock=False)
    assert r2["users"] == [] and r2["claim_id"] is None
