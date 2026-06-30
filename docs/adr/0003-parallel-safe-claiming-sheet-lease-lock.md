# Parallel-safe claiming with a claims ledger (at-least-once) + idempotent persist

Multiple people may run the `email-to-instagram` Workflow at once. Without coordination, concurrent
runs would read the same cursor, process the same users, and write duplicate `output` rows.
Additionally, a run that **crashes after claiming but before persisting** must not silently drop its
batch — once a later claim advances the cursor past it, a bare cursor can never recover it.

## Decision

Two mechanisms in the Sheet, both driven by `claim.py` under a `state`-tab lease-lock:

1. **Lease-lock around the claim** (`state` cells `lock_token`/`lock_expires`). A claim acquires the
   lock (write a uuid token + expiry, short sleep, read-back verify), does its cursor/ledger work,
   and releases. Held only for the fast claim, never during OSINT/stats — so concurrent runs claim
   **disjoint** slices.

2. **A `claims` ledger** (`claim_id, cursor_start, cursor_end, run_id, status, lease_expires,
   updated_at`) for **at-least-once** recovery. Each claim, under the lock:
   - first **reclaims** any `in_progress` row whose **lease has expired** (its claimer died),
     re-fetching that exact range (`fetch(after=cursor_start)`, trimmed to `cursor_end`); else
   - **takes new work**: read the cursor, fetch the next batch, advance the cursor, append a new
     `in_progress` claim row.
   `persist.py` marks the claim row `done` after writing. So a crashed batch is re-handed-out once
   its lease lapses, and no user is permanently dropped.

3. **Idempotent persist (dedup by email).** `persist.py` reads the set of emails already in `output`
   and skips any it already contains (and de-dups within the batch). Combined with at-least-once
   recovery this yields **effectively exactly-once** output: a recovery race (or a slow-but-alive
   run whose lease lapsed and got reclaimed) can reprocess a batch, but it can never write a
   duplicate row. This also closes the old lost-append-ack edge.

## Consequences

- **No double-processing in the normal case** (disjoint claims) and **no dropped users** (ledger
  recovery), with **no duplicate output rows** ever (email dedup). The Workflow loops until a claim
  returns *zero* users — meaning no new work *and* no reclaimable orphan — not merely until the API
  is exhausted.
- **Lease tuning:** the claim lease (`CLAIM_LEASE_SECONDS`, default 1800s) must comfortably exceed
  one batch's OSINT+persist time so a healthy run isn't reclaimed mid-flight. Too long delays
  recovery of a real crash; too short risks reclaim races (harmless thanks to dedup, but wasteful).
  No heartbeat is used — a generous lease keeps it simple.
- **Recovery is eventual, not immediate:** an orphan is only reclaimable after its lease expires, so
  a crashed batch is picked up by a later claim/run after that window — acceptable for at-least-once.
- **Costs at scale:** `find_reclaimable` reads the `claims` tab and `persist` reads `output` col A
  per batch. Both are single reads dwarfed by the OSINT time, but the `claims` tab grows with batch
  count; compacting `done` rows is a possible future optimization.
- **Lock is still best-effort** (Sheets has no compare-and-swap), but a rare double-claim now only
  costs redundant work — dedup guarantees output correctness regardless. A server-side atomic
  claim/ack endpoint remains the only way to also eliminate the redundant work; out of scope (the
  API is read-only).
