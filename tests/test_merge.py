from pathlib import Path

from instamail.loader import discover_plugins
from instamail.merge import build_header, merge

FIXTURES = Path(__file__).parent / "fixtures"
FOUND = discover_plugins(FIXTURES)


def _p(*names):
    return [FOUND[n] for n in names]


# Canonical data the email/phone fixtures return.
EMAIL_A = [
    {"email": "a@x.com", "name": "Al", "followers": 100},
    {"email": "b@x.com", "name": "Bea", "followers": 50},
]
EMAIL_B = [
    {"email": "a@x.com", "handle": "@al"},
    {"email": "c@x.com", "handle": "@cy"},
]
PHONE_A = [{"phone": "+15550001", "carrier": "AT&T"}]


def _by_value(header, rows, col, val):
    return next(r for r in rows if r[col] == val)


def test_same_key_join_and_blanks():
    plugins = _p("email_a", "email_b")
    header, rows = merge(plugins, {"email_a": EMAIL_A, "email_b": EMAIL_B})
    assert header == ["email", "email_a_name", "email_a_followers", "email_b_handle"]
    assert [r["email"] for r in rows] == ["a@x.com", "b@x.com", "c@x.com"]

    merged = _by_value(header, rows, "email", "a@x.com")
    assert merged["email_a_name"] == "Al" and merged["email_b_handle"] == "@al"
    # unmatched keys leave the other plugin's columns blank
    assert _by_value(header, rows, "email", "b@x.com")["email_b_handle"] is None
    assert _by_value(header, rows, "email", "c@x.com")["email_a_name"] is None


def test_mixed_key_stacks_sparsely():
    plugins = _p("email_a", "phone_a")
    header, rows = merge(plugins, {"email_a": EMAIL_A, "phone_a": PHONE_A})
    assert header == ["email", "phone", "email_a_name", "email_a_followers", "phone_a_carrier"]
    assert len(rows) == 3  # 2 email rows + 1 phone row, never joined

    phone_row = _by_value(header, rows, "phone", "+15550001")
    assert phone_row["email"] is None and phone_row["email_a_name"] is None
    email_row = _by_value(header, rows, "email", "a@x.com")
    assert email_row["phone"] is None and email_row["phone_a_carrier"] is None


def test_partial_mixed_groups_then_stacks():
    plugins = _p("email_a", "email_b", "phone_a")
    header, rows = merge(
        plugins, {"email_a": EMAIL_A, "email_b": EMAIL_B, "phone_a": PHONE_A}
    )
    # email_a + email_b still merge on a@x.com; phone stacks separately
    merged = _by_value(header, rows, "email", "a@x.com")
    assert merged["email_a_name"] == "Al" and merged["email_b_handle"] == "@al"
    assert any(r["phone"] == "+15550001" for r in rows)
    assert len(rows) == 4  # a, b, c (email) + phone


def test_duplicate_key_within_plugin_last_wins():
    plugins = _p("email_a")
    dupes = [
        {"email": "a@x.com", "name": "First", "followers": 1},
        {"email": "a@x.com", "name": "Second", "followers": 2},
    ]
    _, rows = merge(plugins, {"email_a": dupes})
    assert len(rows) == 1
    assert rows[0]["email_a_name"] == "Second"


def test_empty_result_still_contributes_columns():
    plugins = _p("email_a", "email_b")
    header, rows = merge(plugins, {"email_a": EMAIL_A, "email_b": []})
    assert "email_b_handle" in header
    assert all(r["email_b_handle"] is None for r in rows)


def test_none_key_rows_are_standalone():
    plugins = _p("email_a")
    rows_in = [
        {"email": None, "name": "X", "followers": 1},
        {"email": None, "name": "Y", "followers": 2},
        {"email": "", "name": "Z", "followers": 3},
    ]
    _, rows = merge(plugins, {"email_a": rows_in})
    assert len(rows) == 3  # never collapsed together


def test_build_header_independent_of_rows():
    plugins = _p("phone_a", "email_a")  # selection order shouldn't matter
    assert build_header(plugins) == [
        "email", "phone", "email_a_name", "email_a_followers", "phone_a_carrier"
    ]
