---
name: schedule-windows
description: Run the email-to-instagram pipeline within bounded, unattended time windows -- "resume the pipeline at 11pm, stop by 12:10", "run it from X to Y", or any number of start/stop pairs. Handles soft-drain, hard-stop failsafes, session-independent watchdogs, and rate-limit-aware retries automatically. Use whenever the user wants the instamail pipeline to run for a bounded stretch of time without staying at the keyboard.
---

# schedule-windows

Turns "resume the pipeline at X, stop at Y" into a fully automated, bounded, unattended run.
Built on ADR 0005: `scripts/claim.py` refuses new work while `.cache/STOP` exists (soft drain);
`scripts/schedule_windows.py` computes every timestamp/cron/prompt deterministically, so nothing
is interpolated by a model at fire time; this file is the protocol a (possibly cold, possibly 3AM)
Claude session follows when a scheduled prompt fires.

Two layers, deliberately redundant:
- **launchd watchdogs** -- session-independent hard floor. Armed once, up front, for every window.
  Even if the Claude session dies, a watchdog still touches `.cache/STOP` at its window's drain time.
- **cron matrix** (launch/drain/failsafe per window) -- the cooperative layer. Needs a live session,
  but is what actually launches/relaunches the pipeline and reacts to rate-limits intelligently.

If the session dies mid-night: the watchdog still bounds runaway spend; the Google Sheet remains the
durable resume point regardless (ADR 0001/0003); use the Resume-After-Interruption Checklist below.

## Invocation

Parse the request into `{"windows": [{"start":"HH:MM", "drainAt":"HH:MM"|"stopBy":"HH:MM"}, ...]}`.
`drainAt` = soft-drain fires there, hard stop follows 15min later (overshoot accepted). `stopBy` =
hard stop fires there sharp (drain = stopBy-15). Don't invent windows the user didn't ask for; ask
only if start-vs-stop is genuinely ambiguous.

## Setup order (strict -- no cron may exist before its protocol/state does)

1. **Idempotency check.** If `.cache/schedule-windows/active.json` exists and represents a run that
   hasn't fully settled (any window not yet past its failsafe with all its tasks handled), STOP and
   ask the user: refuse (leave the live run alone) or replace (first run the Failsafe steps below
   for every one of its windows, to tear down its crons/watchdogs/STOP, then continue). Never
   silently overwrite live run state.
2. **Run the math**: `echo '<windows JSON>' | .venv/bin/python scripts/schedule_windows.py`. Non-
   zero exit / `{"error": ...}` output -> STOP, show the user the exact error. Do not work around a
   validation failure yourself -- these are hard errors by design (ADR 0005), not bugs to route
   around.
3. **Echo resolved times to the user** before creating anything: state every window's launch/drain/
   failsafe in local HH:MM. If the user's phrasing was exact (e.g. they gave `drainAt` directly),
   proceeding immediately after stating the times is fine. If resolution required interpreting loose
   phrasing, or a `stopBy` was back-computed into an earlier `drainAt` the user didn't state
   explicitly, wait for explicit confirmation before continuing -- don't silently pre-adjust a time
   the user gave a different number for.
4. **Write `active.json`** (schema below) with every window `pending`, empty task sets, atomic write
   (temp file + rename) -- before any cron or watchdog exists, so nothing can ever fire into missing
   state.
5. **Submit watchdogs** for every window: `launchctl submit -l <label> -- ...` using the exact
   `launchd_cmd` string schedule_windows.py rendered. This is the session-independent floor; arm it
   before anything cooperative exists.
6. **Create drain + failsafe cron jobs** for every window via `CronCreate` (`recurring: false`,
   using the script's `cron` field and `prompts.drain` / `prompts.failsafe` text verbatim). Record
   each returned job id in `active.json`.
7. **Create launch cron jobs last**, same way, using `prompts.launch`. Only once everything else
   exists, so a launch can never fire into a world without its own drain/failsafe/watchdog net.
8. **Verify**: `CronList` shows every job id just recorded; `launchctl list` shows every watchdog
   label. If any step 4-8 failed partway, roll back everything this invocation created (delete the
   partial crons/watchdogs, remove `active.json`) before reporting failure -- never leave half a
   schedule live.
9. **Confirm** the final resolved matrix and that the schedule is live.

## active.json schema

Path: `.cache/schedule-windows/active.json` (repo-local, gitignored -- deterministic for cron
prompts to find; NOT the Claude memory directory). Written atomically (temp + `os.replace`) on every
update; a matching append-only `.cache/schedule-windows/<run_id>/events.jsonl` gets one line per
event.

```json
{
  "run_id": "w20260719T211300",
  "created_at": "2026-07-19T21:13:00-07:00",
  "windows": [
    {
      "index": 1, "final": false,
      "launch": "...", "drain": "...", "guard": "...", "failsafe": "...",
      "watchdog_label": "com.instamail.wd1",
      "cron_job_ids": {"launch": "...", "drain": "...", "failsafe": "..."},
      "tasks": { "<task_id>": {"status": "launching|active|drain_requested|handled", "started_at": "..."} }
    }
  ],
  "pending_retry": null,
  "provider_throttle_count": 0,
  "cap_raised": false,
  "baseline": {"captured_at": "...", "cursor": "...", "output_rows": 0}
}
```

`baseline` is captured once, right after step 4 (before anything can touch the Sheet), by reading
the `state` tab cursor and `output` tab row count. The final failsafe's night report (wrap-up step 6
below) diffs its own end-of-run read against this baseline to report real deltas, not just absolute
counts.

`pending_retry`, when set, is `{"cron_id": "...", "fires_at": "...", "window_index": N}` -- it is a
**run-level singleton** (at most one across ALL windows combined, never one per window -- see the
Singleton retry invariant below).

`tasks` is the active-task set, tracked per-window but treated as one set across the whole run when
checking for "siblings" -- see the Notification State Machine below.

## Notification State Machine

Applies every time a launched Workflow task notifies (it may be minutes or hours later, possibly in
a fresh session -- re-read `active.json` fresh each time, don't rely on in-memory state). The
Workflow returns `{processed, stopped, done}`; a `status:killed` delivery can also arrive if a task
was stopped externally.

1. **`status:killed`** -> append a note to the events log. Already-handled or unknown task-id ->
   note only (deliveries can duplicate; this is idempotent).
2. **`stopped:'drain'`** -> mark this task `handled` in `active.json`. Once no task anywhere in the
   run is still `active`/`launching`: `rm -f .cache/STOP`, do a quick Sheet verify, save state. Do
   **not** relaunch (`.cache/STOP` persists as long as any sibling task is still alive, so it's not
   safe to clear it out from under them).
3. **`done:true`** (only possible from explicit `outcome:"exhausted"` per ADR 0005 -- a drain or
   error can never produce this) -> verify before believing it; **never treat as terminal while a
   sibling task is still running anywhere in the run**. Do one bounded claims-tab read
   (`find_reclaimable` pattern, `scripts/sheets_io.py:219`) and classify any `in_progress` claim row
   by lease-vs-now:
   - **lease expired** -> relaunch now (respecting this window's guard -- past it, save-only instead).
   - **lease live + a sibling task is running** -> do nothing; that sibling will surface it.
   - **lease live + no sibling running** -> an ADR-0004 deferral (throttled, waiting out its lease).
     An immediate relaunch would hot-loop against a live lease it can't reclaim yet -- instead
     create **one** retry cron at `earliest_lease_expiry + 1min`, capped at this window's guard
     (past the guard -> save-only, not a retry). Enforce the singleton invariant: clear any other
     pending retry first.
   - **no in_progress rows at all** -> genuinely complete. `CronDelete` every remaining job for this
     run (launch/drain/failsafe crons + any pending retry), `launchctl remove` every watchdog label,
     final save, write the night report (see Failsafe step 6).
4. **`stopped` is one of `rate-limited`, `agent-cap`, `token-budget`, `claim-error`,
   `reclaim-empty`** -> time guard first: if `now` is at or past this window's guard, this is
   save-only (same handling as a blackout, below) -- do not relaunch.
   - **Account/session usage limit (check BEFORE the generic paths)**: if the evidence for a
     `rate-limited` or `claim-error` stop -- the task's own output, the claim step's error text,
     or a probe's error message -- names a **Claude account or session usage limit** (e.g.
     "usage limit... resets at HH:MM"), this is NOT the WebSearch cap and NOT a provider
     throttle: do **not** edit any cap, do **not** increment `provider_throttle_count`, and do
     **not** relaunch immediately (a relaunch burns straight into the same exhausted usage
     window). Instead create **one** retry cron at `stated_reset_time + 2min`, capped at this
     window's guard (past the guard -> save-only), enforcing the singleton invariant. If no
     reset time is stated, fall through to the generic paths below. (First seen 2026-07-19,
     twice; distinct from both `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION` and provider
     throttling.)
   - **`rate-limited`**: probe with a single `WebSearch` call. If it comes back with a budget/quota-
     exhausted message, this was our own session cap -- raise it (edit the WebSearch env cap in
     `~/.claude/settings.json`, ×10 from its current value), set `cap_raised: true` in `active.json`
     (**do not** touch `provider_throttle_count` for this), then relaunch. If the probe succeeds
     normally, this was the underlying search **provider** throttling, not our cap: increment
     `provider_throttle_count`. Count is now 1 -> relaunch immediately (assume transient). Count is
     now >=2 -> back off: single retry cron at `now + 10min` (respecting the guard, else save-only),
     enforcing the singleton invariant. Reset `provider_throttle_count` to 0 only when you see a
     completion that both made progress (`processed > 0`) **and** stopped for a reason other than
     rate-limiting -- never reset it on a bare `done:true` (a lucky completion tells you nothing
     about whether the provider is still throttling).
   - **anything else in this group** -> relaunch immediately.
   - **Singleton retry invariant, always**: at most one pending retry cron exists across the whole
     run. Every relaunch path clears any existing pending-retry id first. A firing retry cron clears
     its own id from `active.json` immediately and re-checks time-guard + `.cache/STOP` + the active-
     task set before doing anything (closes the leapfrog race where two retries could otherwise both
     fire).
5. Every event (launch, drain, relaunch, retry scheduled, handled, error) is one appended line in
   this run's `events.jsonl`. Do a full Sheet verify (row count / cursor) at every wrap-up. Data
   durability itself never depends on any of this machinery -- it's per-batch Sheet persistence
   (ADR 0001/0003), which survives anything, including total session death.

## Blackout save-only semantics

If the Claude session itself freezes or dies mid-window (account/usage limit, crash, manual kill):
no further processing happens until a human or a fresh session resumes -- this is inherent, not a
bug ("launching and retrying need a live session" is a stated, accepted boundary). The watchdogs
still bound runaway spend regardless. On the next wake-up into a live session (a cron firing, or a
human resuming), always re-read `active.json` fresh; if `now` is past a window's guard, treat it as
save-only -- verify and save state, never launch new work.

## Stale-STOP hygiene

`.cache/STOP` surviving from a dead session is self-diagnosing: `claim.py` prints a loud stderr
warning every time it drains because of it. If the pipeline appears to be draining with no drain/
failsafe scheduled for "now", check `.cache/STOP`'s mtime against the active run's window matrix --
if it predates the current window's launch, it's stale. `rm .cache/STOP` clears it (each window's
own launch step already does this defensively; a manual resume should too).

## Resume-After-Interruption Checklist

Picking this up cold (fresh session, the prior one died, or the user says "resume the schedule"):

1. Read `.cache/schedule-windows/active.json`. Missing or unparseable -> there is no active
   schedule; treat any new request as a fresh invocation (Setup order, above).
2. If present: reconcile window-by-window against `now`. A window whose failsafe time has already
   passed should be settled -- verify with one bounded claims-tab + output-tab read (same pattern as
   Notification State Machine step 3) rather than assuming the file is accurate. A window still
   ahead needs its cron jobs to actually exist (`CronList`) -- if the session that created them died
   mid-setup, recreate whatever's missing, following the same strict ordering as Setup order.
3. Report current state (settled windows, pending windows, what's live right now) to the user before
   taking any action that changes schedule state.

## Failsafe wrap-up (window settles, whether by time or by state-machine completion)

1. Read `active.json` (save-only if missing/corrupt -- never guess at state).
2. `TaskStop` every task in this window's set not already `handled`/`completed`.
3. `CronDelete` this window's pending-retry cron id if one is recorded; clear it.
4. `launchctl remove <this window's watchdog label>` (ignore "no such process"); `rm -f .cache/STOP`.
5. One bounded claims-tab read + an output-tab row count, for the wrap-up.
6. Append the wrap-up line to `events.jsonl`. **If this is the final window of the run**: diff this
   final read against `active.json`'s `baseline` (cursor + output row count captured at setup) to
   get real deltas, not just end-state absolutes, and write the full night outcome report
   (rows/cursor deltas per window, any residual errors) to the memory file
   `~/.claude/projects/-Users-cherianthomas-dev-lascade-product-intelligence-instamail/memory/
   project_email_to_instagram_paused.md`, and remove any remaining cron jobs / watchdog labels for
   this run.
