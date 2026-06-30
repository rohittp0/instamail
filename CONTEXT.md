# InstaMail

A reverse-resolution pipeline: pull users (email + name) from the product's internal API, resolve
each owner's Instagram **username** and public **stats**, and append the result to a Google Sheet.
A Claude **Workflow** orchestrates the agentic discovery; a small Python toolkit (`scripts/`) does
all the I/O and stat-fetching the Workflow's JS sandbox cannot. Concurrent runs are **parallel-safe**.

## Language

**internal users API (input)**:
The source of who to process — `GET …/internal/users/after/` (header `X-Internal-Key`), returning
`{"users":[{email, first_name, last_name}]}`. **Cursor-paginated and positional** (not email-sorted):
pass the last email of a page as `email=<cursor>` to get the users after it. _Avoid_: dump tab, input
tab (the old Sheet-based source, removed).

**name (OSINT seed)**:
`first_name + last_name` for a user, passed into the resolve step as the strongest search seed
(`"<name> instagram"`, name+niche pivots). It is **not** written to `output` — it only steers
discovery.

**output (tab)**:
The one tab the Workflow writes — one row per **processed** user: the resolved handle (or blank),
`match_confidence`, the full stat set, `stats_status`, `evidence_url`, `resolved_at`. Append-only.

**state (tab)**:
A tiny key/value tab holding the **cursor** (B1) and the **lease-lock** cells (`lock_token` B2,
`lock_expires` B3).

**claims (tab)**:
The recovery **ledger** — one row per claimed batch
(`claim_id, cursor_start, cursor_end, run_id, status, lease_expires, updated_at`). A batch that is
`in_progress` past its lease (its claimer died) is **reclaimed** and reprocessed, so no user is
dropped; `persist.py` marks the row `done` after writing.

**cursor**:
The pagination position — the last email handed out for processing, stored in `state!B1`. **Resume**
re-reads it and continues the API after it. It must be stored (not derived from `output`) because
the API order is positional, not email-sorted.

**Claim**:
Atomically taking the next batch to process (`claim.py`), under the lease-lock. A claim first
**reclaims** an expired-lease `in_progress` ledger row (recovering a dead run's batch by re-fetching
its range); otherwise it takes **new work** — read the cursor, fetch the next `BATCH_SIZE` users,
advance the cursor, and record a new `in_progress` claim row. Because the cursor advances under the
lock, concurrent claimers get **disjoint** slices.

**at-least-once / idempotent persist**:
The processing guarantee: the claims ledger ensures every user is eventually processed even across
crashes (at-least-once), and `persist.py` **dedups by email** against `output` before appending, so
a recovery/reclaim race never writes a duplicate row. Together: effectively exactly-once output.

**lease-lock**:
A best-effort mutual-exclusion lock on the `state` tab: a claimer writes a unique token + an expiry
(lease) and reads it back to confirm it won. The lease lets a crashed claimer's lock auto-expire.
Held only during a Claim (fast), never during OSINT/stats.

**parallel-safety**:
The guarantee that multiple people running the Workflow at once neither process the same user nor
write duplicate `output` rows — provided by Claim + the lease-lock. (Best-effort, not a hard
transaction — see ADR 0003.)

**Reverse resolution**:
Resolving a user (email + name) back to their Instagram `username` via agentic web OSINT.

**match_confidence**:
Confidence the resolved `username` belongs to the user: `high` (email/full name + a single handle on
the same third-party page, **or** the fetched IG bio/personal-domain corroborates the email),
`medium` (corroborated identity pivot), `low` (single weak hop / guess), `none` (dead-end). The
`high`-by-bio case is decided **deterministically** in `persist.py`, not by the LLM.

**Stats**:
The public profile numbers fetched for a resolved handle by the **direct, non-agentic**
`instagram_stats.py` (followers/following/posts, verified/business/private, bio, external_url, plus
derived avg/max views, avg likes/comments, engagement rate, reels ratio, cadence, last post date,
top hashtags), from Instagram's `web_profile_info` endpoint.

**stats_status**:
Outcome of the stat fetch: `ok`, `private` (counts kept, view-metrics blank), `not_found`, `blocked`
(throttled / no usable session), `error`, or blank (a dead-end with no handle to fetch).

**Escalation**:
A lead-gated second resolve pass: the cheap **Sonnet** resolve agent runs first; only when it returns
`none`/`low` **and** flags `needs_escalation` (real leads but a reasoning gap, not a dead-end) does a
warm-started **Opus** agent retry with the prior findings to close the chain.

## Relationships

- internal users API --(Claim: lease-lock → cursor → page → advance cursor)--> **Workflow**.
- A **Resolve** agent (seeded with the user's **name**) produces a candidate handle +
  `match_confidence`; **Stats** + the deterministic bio-upgrade may raise that confidence; **Persist**
  appends the merged row to `output`.
- **Escalation** sits between Sonnet resolve and Persist, firing only for promising-but-unresolved users.
- **Resume** = the `state` **cursor**; **parallel-safety** = Claim under the **lease-lock**.

## Example dialogue

> **Dev:** "Two people start the workflow at the same time — do they step on each other?"
> **Domain expert:** "No. Each Claim takes the lease-lock, advances the cursor, and releases it, so
> the two runs get consecutive, non-overlapping batches. The lock is only held for the claim, not the
> slow OSINT."
>
> **Dev:** "Why store a cursor instead of just resuming after the last email in output?"
> **Domain expert:** "The API cursor is positional, not email-sorted — `h@…` can come after `r@…`.
> The max email in output would be wrong. The stored cursor is the only correct resume point."
>
> **Dev:** "The IG bio confirms the email — who decides that?"
> **Domain expert:** "`persist.py`, in plain Python, against the bio the stats fetch already returned.
> No model tokens, and the resolver never opens instagram.com."

## Flagged ambiguities

- **Resume is the `state` cursor**, advanced at *Claim* time. A crash after claiming leaves an
  orphaned `in_progress` ledger row, which a later claim **reclaims** once the lease expires — so
  recovery is *eventual*, not immediate.
- **`name` is input only** — an OSINT seed, never an `output` column.
- The lease-lock is **best-effort** (Google Sheets has no compare-and-swap); a rare double-claim is
  possible, but `persist.py`'s email dedup makes it harmless (no duplicate rows — just redundant
  work). Eliminating the redundant work too would need a server-side atomic claim endpoint (out of
  scope, API is read-only).
- `match_confidence` (user→handle) is the only confidence here.
