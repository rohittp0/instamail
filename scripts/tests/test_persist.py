"""persist: confidence upgrade, row building, and run() wiring (network-free)."""

from ig_profile import project_profile
from persist import build_rows, run, upgrade_confidence
from sheets_io import OUTPUT_HEADER

COL = {name: i for i, name in enumerate(OUTPUT_HEADER)}


def _stats(**over):
    base = {"stats_status": "ok", "biography": "", "external_url": "", "full_name": ""}
    base.update(over)
    return base


def test_upgrade_full_email_in_bio():
    s = _stats(biography="reach me at jane@example.com anytime")
    assert upgrade_confidence("jane@example.com", "low", s) == "high"


def test_upgrade_distinctive_localpart():
    s = _stats(biography="hey it's johndoe123 here")
    assert upgrade_confidence("johndoe123@gmail.com", "none", s) == "high"


def test_upgrade_personal_domain_in_url():
    s = _stats(external_url="https://janedoe.com/about")
    assert upgrade_confidence("jane@janedoe.com", "low", s) == "high"


def test_no_upgrade_freemail_domain():
    s = _stats(biography="just a gmail.com user", external_url="https://gmail.com")
    assert upgrade_confidence("x@gmail.com", "low", s) == "low"


def test_no_upgrade_when_stats_unavailable():
    s = _stats(stats_status="blocked", biography="jane@example.com")
    assert upgrade_confidence("jane@example.com", "low", s) == "low"


def test_high_stays_high():
    assert upgrade_confidence("a@b.com", "high", _stats()) == "high"


def test_build_rows_resolved_and_dead_end(public_user):
    alpha = project_profile(public_user)          # biography contains jane@example.com
    alpha["stats_status"] = "ok"
    resolve_rows = [
        {"email": "jane@example.com", "username": "alpha", "match_confidence": "medium", "evidence_url": "http://e"},
        {"email": "b@y.com", "username": None, "match_confidence": "none", "evidence_url": ""},
    ]
    rows = build_rows(resolve_rows, {"alpha": alpha}, "2026-06-30T00:00:00+00:00")

    assert len(rows) == 2
    assert all(len(r) == len(OUTPUT_HEADER) for r in rows)

    resolved = rows[0]
    assert resolved[COL["email"]] == "jane@example.com"
    assert resolved[COL["username"]] == "alpha"
    assert resolved[COL["match_confidence"]] == "high"          # upgraded by bio match
    assert resolved[COL["followers"]] == 1000
    assert resolved[COL["stats_status"]] == "ok"
    assert resolved[COL["top_hashtags"]] == "travel, nature"    # list joined for the cell
    assert resolved[COL["resolved_at"]] == "2026-06-30T00:00:00+00:00"

    dead = rows[1]
    assert dead[COL["email"]] == "b@y.com"
    assert dead[COL["username"]] == ""
    assert dead[COL["match_confidence"]] == "none"
    assert dead[COL["stats_status"]] == ""
    assert dead[COL["followers"]] is None


def test_run_wires_fetch_and_appender(public_user):
    captured = []

    async def fake_fetch(usernames):
        assert usernames == ["alpha"]
        s = project_profile(public_user)
        s["stats_status"] = "ok"
        return {"alpha": s}

    def fake_appender(rows):
        captured.extend(rows)
        return len(rows)

    resolve_rows = [
        {"email": "jane@example.com", "username": "alpha", "match_confidence": "low", "evidence_url": ""},
        {"email": "z@z.com", "username": None, "match_confidence": "none", "evidence_url": ""},
    ]
    n = run(resolve_rows, fetch=fake_fetch, appender=fake_appender, now=lambda: "T")
    assert n == 2
    assert len(captured) == 2
    assert captured[0][COL["resolved_at"]] == "T"
