# Design: `email-to-instagram` reverse-resolution Workflow

## Summary

A saved, re-runnable Claude **Workflow** at `.claude/workflows/email-to-instagram.js` that takes a
file of emails and resolves each back to an Instagram **username** via agentic web OSINT, producing
a CSV with exactly `email,username,match_confidence`. It is **not** a `BasePlugin` and does not
touch the CLI/loader/merge/writer framework. See ADR 0003 and the "Reverse resolution" section of
`CONTEXT.md`.

## Inputs / outputs

- **`args`**: either a string path to the email file, or `{ input, output }`.
  - Input file: newline-delimited emails **or** a CSV with an email column.
  - `output` default: `email_to_instagram.csv` in CWD.
- **Output CSV**: header `email,username,match_confidence`; one row per *processed* email,
  including dead-ends (`email,,none`). CSV-escaped.
- **Sidecar checkpoint**: `<output-basename>.checkpoint.json` =
  `{ input, output, last_index, processed, total }`.

## Phases

1. **Load** (1 agent) — read the email file (Read/Bash) and the checkpoint if present; return the
   unique, valid, ordered email list and the resume index (`last_index + 1`, else 0). Workflow
   scripts have no filesystem access, so all I/O is via subagents.

2. **Resolve** (batched pipeline over emails from the resume index) — batch size ~20–30, kept well
   under the agent cap. For each email, an independent chain:
   - **Sweep** — parallel finder agents, each free to use any tool/approach, gathering both
     *direct hits* (pages where the email sits next to an `@handle`) and *identity attributes*
     (name, niche, location, reused usernames on other platforms, personal site, local-part guess).
   - **Stepping-stone pivot** — bounded ~3-round loop: if no confident direct match, feed identity
     attributes back as new search seeds (e.g. "Name niche city instagram", try reused username on
     instagram.com, follow personal-site social links). Each round lands a candidate or surfaces
     more attributes. Early-stops the instant a `high`-confidence match is confirmed.
   - **Synthesize + verify** — reconcile candidates, dedup, WebFetch the strongest evidence
     (third-party first, instagram.com only to close the chain) to confirm the chain returns to
     *this* email, emit the single best `{ email, username, match_confidence }`. One row per email;
     multiple legit accounts → emit highest-reach/most-relevant at `medium`.

3. **Checkpoint + append** (after each batch, 1 writer agent) — append the batch's rows to the CSV
   and advance `last_index` in the checkpoint. Kill-safe: CSV and checkpoint stay consistent up to
   the last completed batch.

## Confidence rubric (`match_confidence`)

- `high` — email and a single handle on the same page, or IG bio shows the exact email (direct).
- `medium` — reached via a corroborated identity pivot (matched name + niche + reused username).
- `low` — single weak hop / unverified guess.
- `none` — dead-end after pivoting; `username` blank.

## Termination & resume

- A run ends when: input exhausted, agent/token budget near limit, or the **rate-limit guard**
  trips (a batch's finders error above ~50% or signal rate limiting) — then checkpoint and stop.
- On stop, report `processed P/total; re-run with the same args to resume`. The **user decides**
  whether to re-invoke; same-args re-invocation resumes from the checkpoint.
- If `budget.total` is set (`+Nk`), the batch loop also stops when `budget.remaining()` is low.

## ToS posture

Inherited from ADR 0002. The workflow `log()`s a one-line ToS notice at start; verify agents prefer
third-party corroboration and hit instagram.com only to close a chain. No tier cap (free-tier OSINT).

## Out of scope (YAGNI)

- Numeric Instagram ID (handle/username only).
- Any change to the `instagram` plugin or the merge framework.
- Paid OSINT APIs / IG account-recovery oracle (web OSINT only).
- An evidence/source column (exactly the three requested columns).

## Caveat

Best-effort, partial coverage by nature — many emails have no public tie to a handle; `none` rows
are normal, not failures.
