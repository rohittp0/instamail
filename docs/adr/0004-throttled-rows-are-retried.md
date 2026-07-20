# A throttled search defers the user for reclaim, not a permanent dead-end

The resolve step reports `rate_limited=true` when its searches were blocked or throttled. Before
this decision, a rate-limited resolve row with no handle was **indistinguishable from a genuine
dead-end**: the Workflow dropped the `rate_limited` flag before persist, `persist.py` wrote the
user as a `none`-confidence row with no username, and `mark_claim_done` closed the claim. Because
output is deduped by email (ADR 0003), that user was then **permanently recorded as "no match" and
never retried** — even though the failure was transient (a search cap or provider throttle), not an
actual absence of an Instagram account.

This was low-visibility while WebSearch was the search tool (its per-session budget fails as a clean
all-or-nothing wall, and the run stops on the `rlCount` guard), but it silently corrupts data
whenever searches are throttled *intermittently* across a batch.

## Decision

Distinguish a transient search failure from a real dead-end, per row, in `persist.py`:

- The Workflow now passes `rate_limited` through to `persist.py` (it is part of each persist row).
- A resolve row that is **`rate_limited` AND has no `username`** is **deferred**: it is not written
  to `output`, and the claim it belongs to is **not** marked `done`. Its lease then expires and the
  batch is reclaimed and retried later — the existing at-least-once recovery path (ADR 0003).
- A row that **found a handle** is persisted normally, regardless of `rate_limited` (a valid result
  is a valid result). Genuine dead-ends (`rate_limited=false`, no handle) are still written as
  `none`, exactly as before.
- `mark_done` fires only when **nothing in the batch was deferred**, so a partly-throttled batch
  writes its good rows immediately (email dedup prevents duplicates on the eventual reclaim) while
  still leaving the claim open for the throttled remainder.

## Consequences

- **No more false permanent dead-ends** from throttling: throttled users are retried instead of
  being frozen as `none`. This preserves the ADR 0003 invariant (no user permanently dropped) under
  search-throttle conditions, not just crashes.
- **Recovery is eventual, not immediate** (same as ADR 0003): a deferred batch is reclaimed only
  after its `CLAIM_LEASE_SECONDS` (1800s) lease lapses. Acceptable for at-least-once; the retry
  should land in a session/window where the search budget is available.
- **Good rows in a throttled batch are not held hostage** — they persist right away; only the
  throttled users wait for reclaim. Dedup (ADR 0003) makes the partial write + later reclaim safe.
- **The `rlCount` run-stop guard is unchanged**: a majority-throttled batch still stops the run so
  we don't hammer a throttled provider. The two mechanisms compose — the guard stops *the run*, this
  decision protects *the data* — and neither depends on the search backend, so it holds whether the
  search cap is raised or a different search transport is used later.
