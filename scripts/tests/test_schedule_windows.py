"""schedule_windows: deterministic time math (no LLM arithmetic at fire time, per ADR 0005/plan).

resolve_windows() is pure and injectable (`now`) — this is the part correctness hinges on. The
golden case below is the plan's own Part 3 worked example (3 Codex review rounds verified this
exact matrix by hand); reproducing it here means the code and the reviewed design stay in sync.
render_schedule()/build_schedule() are checked more loosely (key facts present, not exact prose) —
their job is baking already-correct values into text, not deriving new ones."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from schedule_windows import WindowError, build_schedule, render_schedule, resolve_windows

TONIGHT = datetime(2026, 7, 19, 21, 13, 0)  # a Sunday evening, before either window


def _at(day, h, m):
    return datetime(2026, 7, day, h, m, 0)


def test_golden_matrix_matches_plan_part3():
    spec = {"windows": [
        {"start": "23:00", "drainAt": "00:10"},
        {"start": "00:30", "drainAt": "03:26"},
    ]}
    w1, w2 = resolve_windows(spec, TONIGHT)

    assert w1["launch"] == _at(19, 23, 1)
    assert w1["drain"] == _at(20, 0, 10)
    assert w1["guard"] == _at(20, 0, 0)
    assert w1["failsafe"] == _at(20, 0, 25)

    assert w2["launch"] == _at(20, 0, 31)
    assert w2["drain"] == _at(20, 3, 26)
    assert w2["guard"] == _at(20, 3, 16)
    assert w2["failsafe"] == _at(20, 3, 41)

    # explicit gap check from the plan: 00:25 + 2min <= 00:31
    assert w1["failsafe"] + timedelta(minutes=2) <= w2["launch"]


def test_stopby_back_computes_drain_and_guard():
    just_after_midnight = _at(20, 0, 0)
    spec = {"windows": [{"start": "00:30", "stopBy": "03:30"}]}
    (w,) = resolve_windows(spec, just_after_midnight)
    assert w["drain"] == _at(20, 3, 15)     # stopBy - 15
    assert w["guard"] == _at(20, 3, 5)      # stopBy - 25 == drain - 10
    assert w["failsafe"] == _at(20, 3, 30)  # == stopBy exactly


def test_launch_plus_one_minute_only_on_the_hour_or_half_hour():
    spec = {"windows": [{"start": "23:05", "drainAt": "23:40"}]}
    (w,) = resolve_windows(spec, TONIGHT)
    assert w["launch"] == _at(19, 23, 5)    # not on :00/:30 -> no early-fire dodge needed


def test_requires_exactly_one_of_drainat_or_stopby():
    with pytest.raises(WindowError):
        resolve_windows({"windows": [{"start": "23:00"}]}, TONIGHT)
    with pytest.raises(WindowError):
        resolve_windows({"windows": [
            {"start": "23:00", "drainAt": "00:10", "stopBy": "00:30"}
        ]}, TONIGHT)


def test_first_window_wholly_in_the_past_is_rejected_not_rolled():
    # "now" is 21:13; 20:00 today has already passed -- must error, never silently mean tomorrow.
    spec = {"windows": [{"start": "20:00", "drainAt": "20:30"}]}
    with pytest.raises(WindowError, match="already passed"):
        resolve_windows(spec, TONIGHT)


def test_overnight_wraparound_resolves_forward_unambiguously():
    # second timestamp numerically "before" the first -> next calendar day, not a typo guess.
    spec = {"windows": [{"start": "23:00", "drainAt": "22:00"}]}
    (w,) = resolve_windows(spec, TONIGHT)
    assert w["drain"] == _at(20, 22, 0)     # next day 22:00, not rejected, not "corrected"


def test_window_too_short_is_rejected():
    spec = {"windows": [{"start": "23:00", "drainAt": "23:05"}]}   # guard would be 22:55, before launch
    with pytest.raises(WindowError, match="too short|launch"):
        resolve_windows(spec, TONIGHT)


def test_adjacent_windows_too_close_are_rejected():
    spec = {"windows": [
        {"start": "23:00", "drainAt": "00:10"},
        {"start": "00:26", "drainAt": "03:26"},   # W1 failsafe 00:25 + 2min = 00:27 > 00:26 launch
    ]}
    with pytest.raises(WindowError, match="close|gap"):
        resolve_windows(spec, TONIGHT)


def test_final_window_flag_set_on_last_only():
    spec = {"windows": [
        {"start": "23:00", "drainAt": "00:10"},
        {"start": "00:30", "drainAt": "03:26"},
    ]}
    w1, w2 = resolve_windows(spec, TONIGHT)
    assert w1["final"] is False
    assert w2["final"] is True


def test_empty_windows_list_rejected():
    with pytest.raises(WindowError):
        resolve_windows({"windows": []}, TONIGHT)


# --- rendering: cron exprs, labels, prompts, launchd cmd -------------------------------------

def test_render_schedule_produces_cron_and_launchd_and_prompts():
    spec = {"windows": [
        {"start": "23:00", "drainAt": "00:10"},
        {"start": "00:30", "drainAt": "03:26"},
    ]}
    resolved = resolve_windows(spec, TONIGHT)
    out = render_schedule(resolved, run_id="w20260719T211300", repo_root=Path("/repo"))

    w1, w2 = out["windows"]
    assert w1["launch"]["cron"] == "1 23 19 7 *"
    assert w1["drain"]["cron"] == "10 0 20 7 *"
    assert w1["failsafe"]["cron"] == "25 0 20 7 *"
    assert w1["watchdog"]["label"] == "com.instamail.wd1"
    assert w1["watchdog"]["target_epoch"] == int(w1["drain"]["epoch"])
    assert w2["watchdog"]["label"] == "com.instamail.wd2"

    # prompts are self-contained: concrete paths baked in, no placeholders left
    for p in (w1["prompts"]["launch"], w1["prompts"]["drain"], w1["prompts"]["failsafe"]):
        assert "{" not in p and "}" not in p
    assert "/repo/.cache/schedule-windows/active.json" in w1["prompts"]["launch"]
    assert "/repo/.cache/schedule-windows/active.json" in w1["prompts"]["drain"]
    assert "/repo/.cache/schedule-windows/active.json" in w1["prompts"]["failsafe"]
    assert "com.instamail.wd1" in w2["prompts"]["launch"]      # W2 launch cleans up W1's watchdog
    assert "com.instamail.wd1" not in w1["prompts"]["launch"]  # W1 has no prior watchdog to remove
    assert "night outcome report" in w2["prompts"]["failsafe"].lower()  # final window only
    assert "night outcome report" not in w1["prompts"]["failsafe"].lower()

    assert "launchctl submit" in w1["launchd_cmd"] and "com.instamail.wd1" in w1["launchd_cmd"]
    assert str(w1["watchdog"]["target_epoch"]) in w1["launchd_cmd"]


def test_build_schedule_end_to_end_matches_golden_matrix():
    spec = {"windows": [
        {"start": "23:00", "drainAt": "00:10"},
        {"start": "00:30", "drainAt": "03:26"},
    ]}
    out = build_schedule(spec, now=TONIGHT, run_id="w20260719T211300", repo_root=Path("/repo"))
    assert out["run_id"] == "w20260719T211300"
    assert len(out["windows"]) == 2
    assert out["windows"][0]["drain"]["hhmm"] == "00:10"
    assert out["windows"][1]["drain"]["hhmm"] == "03:26"


def test_build_schedule_raises_are_catchable_with_message():
    with pytest.raises(WindowError):
        build_schedule({"windows": []}, now=TONIGHT, run_id="x", repo_root=Path("/repo"))
