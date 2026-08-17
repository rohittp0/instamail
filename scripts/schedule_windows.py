#!/usr/bin/env python3
"""Deterministic cron/launchd math for bounded pipeline windows (ADR 0005).

No LLM arithmetic at fire time: every timestamp, cron expression, launchd watchdog spec, and cron
prompt is fully computed and rendered here. The schedule-windows skill takes this script's output
verbatim and feeds it straight into CronCreate / launchctl calls -- it never re-derives a time.

A window is `{"start":"HH:MM", "drainAt":"HH:MM"}` or `{"start":"HH:MM", "stopBy":"HH:MM"}`
(exactly one of drainAt/stopBy). drainAt: drain fires there, hard stop = drainAt+15 (overshoot
accepted). stopBy: hard stop fires there sharp, drain = stopBy-15, guard = stopBy-25. Windows chain
in order given -- each window's start resolves to the next occurrence of that wall-clock time at or
after the previous window's failsafe (or, for the first window, at or after `now`; if that requires
rolling into tomorrow the whole call is rejected -- never silently reinterpreted as "tomorrow").

stdin : {"windows": [{"start","drainAt"|"stopBy"}, ...], "now": "<ISO8601, optional>",
         "run_id": "<optional>"}
stdout: {"run_id", "windows": [...]}  on success: exit 0
        {"error": "..."}             on any validation failure: exit 1 (hard error, never a silent fix)

Usage:
    echo '{"windows":[{"start":"23:00","drainAt":"00:10"}]}' | .venv/bin/python scripts/schedule_windows.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STOP_FILE_REL = ".cache/STOP"
ACTIVE_JSON_REL = ".cache/schedule-windows/active.json"
SKILL_MD_REL = ".claude/skills/schedule-windows/SKILL.md"

FAILSAFE_OFFSET = timedelta(minutes=15)
GUARD_OFFSET = timedelta(minutes=10)
GAP_BUFFER = timedelta(minutes=2)

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class WindowError(ValueError):
    """A hard validation error in the window spec. Never silently worked around."""


def _parse_hhmm(s):
    m = _HHMM_RE.match(s or "")
    if not m:
        raise WindowError(f"invalid HH:MM time: {s!r}")
    return int(m.group(1)), int(m.group(2))


def _next_occurrence(hhmm: str, after: datetime) -> datetime:
    """Earliest datetime with wall-clock hhmm that is >= `after`, rolling forward by whole days.

    This is the one and only wraparound rule -- applied uniformly, it resolves overnight spans
    (23:00 -> 00:10 next day) without ever needing to guess intent ("typo inference")."""
    h, m = _parse_hhmm(hhmm)
    candidate = after.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate < after:
        candidate += timedelta(days=1)
    return candidate


def resolve_windows(spec: dict, now: datetime) -> list[dict]:
    """Pure time math: window specs -> resolved datetimes, validated. Raises WindowError."""
    windows = spec.get("windows")
    if not isinstance(windows, list) or not windows:
        raise WindowError("windows must be a non-empty list")

    resolved = []
    cursor = now
    for i, w in enumerate(windows):
        idx = i + 1
        start_hhmm = w.get("start")
        if not start_hhmm:
            raise WindowError(f"window {idx}: missing start")
        has_drain = bool(w.get("drainAt"))
        has_stop = bool(w.get("stopBy"))
        if has_drain == has_stop:
            raise WindowError(f"window {idx}: exactly one of drainAt or stopBy is required")

        if i == 0:
            h, m = _parse_hhmm(start_hhmm)
            start_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if start_dt < now:
                raise WindowError(
                    f"window 1 start {start_hhmm} has already passed today ({now.isoformat()}); "
                    "refusing to silently roll it to tomorrow -- say the date explicitly or pick "
                    "a time later than now"
                )
        else:
            start_dt = _next_occurrence(start_hhmm, cursor)
        cursor = start_dt

        if has_drain:
            drain_dt = _next_occurrence(w["drainAt"], cursor)
            failsafe_dt = drain_dt + FAILSAFE_OFFSET
        else:
            stop_dt = _next_occurrence(w["stopBy"], cursor)
            drain_dt = stop_dt - FAILSAFE_OFFSET
            failsafe_dt = stop_dt
        guard_dt = drain_dt - GUARD_OFFSET

        # early-fire dodge: one-shot crons landing exactly on :00/:30 can fire up to 90s early
        launch_dt = start_dt + timedelta(minutes=1) if start_dt.minute in (0, 30) else start_dt

        if launch_dt >= guard_dt:
            raise WindowError(
                f"window {idx}: launch {launch_dt.isoformat()} >= guard {guard_dt.isoformat()} "
                "-- window is too short to be viable"
            )

        resolved.append({
            "index": idx, "final": False,   # fixed up once the full list is known, below
            "start": start_dt, "launch": launch_dt, "drain": drain_dt,
            "guard": guard_dt, "failsafe": failsafe_dt,
        })
        cursor = failsafe_dt  # the next window may not start until this one has fully settled

    for a, b in zip(resolved, resolved[1:]):
        if a["failsafe"] + GAP_BUFFER > b["launch"]:
            raise WindowError(
                f"window {a['index']} failsafe {a['failsafe'].isoformat()} + 2min > "
                f"window {b['index']} launch {b['launch'].isoformat()} -- windows are too close "
                "together"
            )

    resolved[-1]["final"] = True
    return resolved


# --- rendering: cron exprs, launchd specs, self-contained prompt text -------------------------

def _cron_expr(dt: datetime) -> str:
    return f"{dt.minute} {dt.hour} {dt.day} {dt.month} *"


def _label(idx: int) -> str:
    return f"com.instamail.wd{idx}"


def _stamp(dt: dict) -> dict:
    return {"iso": dt.isoformat(), "hhmm": dt.strftime("%H:%M"), "epoch": int(dt.timestamp())}


def _watchdog_cmd(label: str, target_epoch: int, stop_file_abs: str) -> str:
    return (
        f"launchctl submit -l {label} -- /bin/bash -c "
        f"'T={target_epoch}; S=$((T - $(date +%s))); "
        f'if [ "$S" -gt 0 ]; then sleep "$S"; fi; '
        f"mkdir -p \"$(dirname {stop_file_abs})\" && touch {stop_file_abs}'"
    )


def _launch_prompt(w, run_id, repo_root, active_json_abs, stop_file_abs, skill_md_rel, events_abs):
    idx = w["index"]
    prior = None if idx == 1 else _label(idx - 1)
    cleanup = (
        f"`launchctl remove {prior}` (ignore \"no such process\" if it already fired) then "
        f"`rm -f {stop_file_abs}`."
        if prior else
        f"`rm -f {stop_file_abs}` (defensive -- clears any stale STOP left by a prior night)."
    )
    return f"""Bounded-window LAUNCH -- schedule-windows run {run_id}, window {idx}{"/final" if w["final"] else ""} of this run.

0. Read {active_json_abs}. If missing or unparseable: setup never completed cleanly -- log a note
   to stderr and STOP. Do not launch anything against a cold/corrupt state.
1. Time guard FIRST: if now is at or past this window's guard ({w["guard"].isoformat()}), this is
   save-only -- do NOT launch, do not clear {stop_file_abs}, do not remove any watchdog (an
   earlier window's overdue drain/failsafe may still depend on them). Append a save-only note to
   {events_abs} and stop here.
2. Check active.json's active-task set. If a task from a PRIOR window is still marked
   launching/active (its failsafe did not settle it), do NOT clean up and do NOT silently skip:
   leave {stop_file_abs} in place (touch it if absent) so that task cannot claim new work, then:
   a. TaskOutput(task_id=<the task-id>, block=false) to check without waiting (TaskGet/TaskList
      are the TODO-task list, not background tasks -- do not use them here). If it actually
      finished, mark it handled in active.json and continue to step 3.
   b. Otherwise create ONE singleton retry cron for reconciliation at now+10min (or save-only if
      that would be past this window's guard), prompt: "Reconcile stuck task <task-id> from an
      earlier window per {skill_md_rel}, then retry this window's launch." Clear any other
      pending-retry id first (at most one pending retry ever exists). Stop here -- no cleanup,
      no launch.
3. Only now, immediately before launching, clean up: {cleanup}
4. Clear this window's pending-retry id in active.json, if any.
5. Write a `launching` marker for window {idx} into active.json (atomic temp-file + os.replace).
6. Launch the email-to-instagram Workflow now. Record the returned task-id in active.json's
   active-task set for window {idx} (replace the `launching` marker with `active`).
7. Append to {events_abs}: "window {idx} launched, task=<task-id>".

This launched task will notify later (maybe much later, maybe in a fresh session). When it does,
apply the Notification State Machine in {skill_md_rel} verbatim -- read that file if its contents
are not already in context this session."""


def _drain_prompt(w, run_id, active_json_abs, stop_file_abs, events_abs):
    idx = w["index"]
    return f"""Bounded-window DRAIN -- schedule-windows run {run_id}, window {idx}.

0. Read {active_json_abs} (informational only -- if missing/corrupt, proceed anyway: touching
   STOP is safe regardless of state, unlike LAUNCH/FAILSAFE which must not act on unknown state).
1. `mkdir -p "$(dirname {stop_file_abs})" && touch {stop_file_abs}`.
2. Append to {events_abs}: "window {idx} drain requested at {w["drain"].isoformat()}".
3. Do nothing else. This is a SOFT drain -- in-flight work finishes on its own, and the next
   claim.py call will refuse new work. The failsafe cron ({w["failsafe"].strftime("%H:%M")}) is
   the hard backstop if something is still running by then."""


def _failsafe_prompt(w, run_id, repo_root, active_json_abs, stop_file_abs, events_abs, memory_file_abs):
    idx = w["index"]
    label = _label(idx)
    final_step = (
        f"6. FINAL window of this run: write the full night outcome report (rows/cursor deltas "
        f"per window, any residual errors) to {memory_file_abs}. Remove all remaining "
        f"schedule-windows cron jobs and watchdog labels for run {run_id}."
        if w["final"] else
        "6. Not the final window -- the next window's own launch cron fires on schedule; nothing "
        "further to do here."
    )
    return f"""Bounded-window FAILSAFE -- schedule-windows run {run_id}, window {idx}{"/final -> night outcome report" if w["final"] else ""}.

0. Read {active_json_abs} (save-only if missing/corrupt -- do not guess at state).
1. TaskStop every task in this window's active-task set not already marked handled/completed.
2. CronDelete this window's pending-retry cron id if one is recorded; clear it in active.json.
3. `launchctl remove {label}` (ignore "no such process"); `rm -f {stop_file_abs}`.
4. One bounded claims-tab read (find_reclaimable pattern, scripts/sheets_io.py:219) plus an
   output-tab row count, for the wrap-up/night report.
5. Append a wrap-up line to {events_abs}.
{final_step}"""


def render_schedule(resolved: list[dict], run_id: str, repo_root: Path) -> dict:
    """Attach cron exprs, launchd specs, and fully self-contained prompt text to resolved windows."""
    repo_root = Path(repo_root)
    stop_file_abs = str(repo_root / STOP_FILE_REL)
    active_json_abs = str(repo_root / ACTIVE_JSON_REL)
    skill_md_rel = SKILL_MD_REL
    events_abs = str(repo_root / f".cache/schedule-windows/{run_id}/events.jsonl")
    memory_file_abs = (
        "~/.claude/projects/-Users-cherianthomas-dev-lascade-product-intelligence-instamail/"
        "memory/project_email_to_instagram_paused.md"
    )

    out_windows = []
    for w in resolved:
        idx = w["index"]
        watchdog_epoch = int(w["drain"].timestamp())
        rendered = {
            "index": idx,
            "final": w["final"],
            "start": _stamp(w["start"]),
            "launch": {**_stamp(w["launch"]), "cron": _cron_expr(w["launch"])},
            "drain": {**_stamp(w["drain"]), "cron": _cron_expr(w["drain"])},
            "guard": _stamp(w["guard"]),
            "failsafe": {**_stamp(w["failsafe"]), "cron": _cron_expr(w["failsafe"])},
            "watchdog": {"label": _label(idx), "target_epoch": watchdog_epoch},
            "prompts": {
                "launch": _launch_prompt(w, run_id, repo_root, active_json_abs, stop_file_abs,
                                          skill_md_rel, events_abs),
                "drain": _drain_prompt(w, run_id, active_json_abs, stop_file_abs, events_abs),
                "failsafe": _failsafe_prompt(w, run_id, repo_root, active_json_abs, stop_file_abs,
                                              events_abs, memory_file_abs),
            },
            "launchd_cmd": _watchdog_cmd(_label(idx), watchdog_epoch, stop_file_abs),
        }
        out_windows.append(rendered)

    return {
        "run_id": run_id,
        "repo_root": str(repo_root),
        "active_json": active_json_abs,
        "stop_file": stop_file_abs,
        "windows": out_windows,
    }


def build_schedule(spec: dict, now: datetime | None = None, run_id: str | None = None,
                    repo_root: Path | None = None) -> dict:
    now = now or datetime.now().astimezone()
    run_id = run_id or f"w{now:%Y%m%dT%H%M%S}"
    repo_root = repo_root or REPO_ROOT
    resolved = resolve_windows(spec, now)
    return render_schedule(resolved, run_id, repo_root)


def main(argv=None) -> int:
    try:
        spec = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        json.dump({"error": f"invalid JSON on stdin: {exc}"}, sys.stdout)
        sys.stdout.write("\n")
        return 1

    now = None
    if spec.get("now"):
        now = datetime.fromisoformat(spec["now"])
        if now.tzinfo is None:
            now = now.astimezone()

    try:
        out = build_schedule(spec, now=now, run_id=spec.get("run_id"))
    except WindowError as exc:
        json.dump({"error": str(exc)}, sys.stdout)
        sys.stdout.write("\n")
        return 1

    json.dump(out, sys.stdout, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
