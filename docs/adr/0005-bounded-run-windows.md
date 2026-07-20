# Bounded run windows: an explicit claim outcome, a STOP-file soft-drain, and a deterministic scheduler

`batch_size` in `claim.py` bounds a single claim, not a run — the Workflow loops claim → resolve →
escalate → persist unbounded until something external kills it, so there was no way to say "resume
at 11pm, stop by 12:10am" without babysitting the session. Building that requires an external
scheduler to reach into a running pipeline, but the Workflow's JS sandbox has no filesystem/clock
access — `claim.py` is the only piece that runs as a real subprocess on every iteration, so it's the
only place a stop signal can land. This also exposed a latent ambiguity: an empty claim result meant
either "no users left" or "something told this run to stop," and callers couldn't tell which.

## Decision

- **`claim()` returns a required `outcome`**: `claimed` (new or reclaimed work handed out),
  `exhausted` (API and reclaim both empty — genuinely done), `reclaim_empty` (a reclaimed lease
  trimmed to zero users), or `drain` (`.cache/STOP` present, so nothing was claimed). `drain` is
  checked twice — at the top of `main()`, before `open_spreadsheet()` (credential-free, instant), and
  again inside `claim()` under the lock, closing the race against a STOP file that lands mid-claim.
- **`outcome` flows into the Workflow's `CLAIM_SCHEMA`** (enum adds `error`, the claim-agent's own
  failure fallback) and maps to `stopReason`: `drain`→`'drain'`, `error`→`'claim-error'`,
  `reclaim_empty`→`'reclaim-empty'`, `exhausted`→`done:true`. **`done:true` can now only arise from
  an explicit `outcome:"exhausted"`** — a drain or an error can never be silently read as "pipeline
  completely done."
- **`.cache/STOP` (gitignored) is the drain sentinel** claim.py checks each invocation. It is a
  *soft* drain: already-claimed batches finish resolving/escalating/persisting instead of being
  killed; anything left unfinished stays recoverable through the existing lease reclaim (ADR 0003).
- **`scripts/schedule_windows.py` computes a window with zero LLM arithmetic.** A window is
  `{start, drainAt}` or `{start, stopBy}` (drain = `stopBy − 15`, guarded by a `stopBy − 25` launch
  cutoff — past it, a wake-up only saves state and never launches new claiming). The script derives
  every timestamp — drain, the 15-minute-later `TaskStop` hard-stop failsafe, guards, watchdog epoch
  — plus rendered cron prompt text and a `launchd` job spec, so nothing is re-derived at fire time.
- **The `schedule-windows` skill is the permanent entry point**: "resume at X, stop at Y" runs the
  script, confirms the resolved times with the user, persists state to
  `.cache/schedule-windows/active.json` (repo-local and gitignored, not the memory dir, so cron-fired
  prompts can find it deterministically), submits `launchd` watchdog jobs, creates the cooperative
  cron matrix (launch/drain/failsafe per window), and verifies everything landed.
- **The two layers are deliberately redundant.** `launchd` watchdogs are the hard,
  session-independent floor — they touch `STOP` at the drain time even if the scheduling session has
  died. The cron matrix is cooperative (needs a live session) but behaves better when one exists:
  relaunches, cap-raising, clean reporting.

## Consequences

- **Unattended runs are boundable without babysitting**: "resume at 11pm, stop by 12:10am" is one
  skill call, and the bound holds even if the scheduling session dies mid-window.
- **The done/stopped ambiguity is closed**: any caller inspecting `outcome`/`stopReason` can always
  tell a real end-of-data condition from an external stop.
- **Overshoot is accepted by design, not a bug**: a `drainAt` window can run ~15 minutes past
  `drainAt` before the failsafe hard-stops it; a `stopBy` window backs its drain off 15 minutes to
  land the hard stop on time. Both trade a little extra runtime for never killing in-flight work.
- **The Sheet stays the durable resume point regardless of how a run stops** (ADR 0001/0003): a
  drained, failsafe-stopped, or crashed run all leave the same recoverable state, so one
  resume-after-interruption checklist covers every case.
