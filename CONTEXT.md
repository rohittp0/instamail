# InstaMail

A Sheets-native reverse-resolution pipeline: given emails in a Google Sheet, resolve each owner's
Instagram **username** and public **stats** into an output sheet. A Claude **Workflow** orchestrates
the agentic discovery; a small Python toolkit (`scripts/`) does all the I/O and stat-fetching the
Workflow's JS sandbox cannot.

## Language

**dump (tab)**:
The raw paste target — column A, one email per row (row 1 is the `email` header). The user dumps
whatever they have here; cleanliness is not required.

**input (tab)**:
A single computed column of the emails still to process, built by **one spreadsheet formula** over
`dump`: lowercase/trim → format-valid → **not already in `output`** → deduped. The Workflow only
ever *reads* this tab. _Avoid_: queue, worklist.

**output (tab)**:
One row per **processed** email — the resolved handle (or blank), `match_confidence`, the full stat
set, `stats_status`, `evidence_url`, and `resolved_at`. The Workflow only ever *appends* here. It is
also the source of truth for **Resume**: the `input` formula subtracts it.

**Reverse resolution**:
Starting from an `email` and resolving back to the owner's Instagram `username` via agentic web
OSINT. Output is one `output` row per email.

**match_confidence**:
Confidence that a resolved `username` belongs to the input `email`: `high` (email + a single handle
on the same third-party page, **or** the fetched IG bio/personal-domain corroborates the email),
`medium` (corroborated identity pivot), `low` (single weak hop / guess), `none` (dead-end, blank
`username`). The `high`-by-bio case is decided **deterministically** in `persist.py`, not by the LLM.

**Stepping-stone pivot**:
When no page directly ties the email to a handle, the resolve agent first works out the owner's
identity (name, niche, location, reused usernames on other platforms, personal site) and feeds those
back as new search seeds. Bounded to ~one round for token efficiency.

**Resume**:
Re-running the Workflow with the same configuration picks up only unprocessed emails — because the
`input` formula already excludes everything in `output`. **There is no checkpoint file**; the sheet
*is* the checkpoint. Dead-ends are written (`email,,none,…`) precisely so they are not retried.

**Stats**:
The public profile numbers fetched for a resolved handle by the **direct, non-agentic**
`instagram_stats.py` (followers/following/posts, verified/business/private, bio, external_url, and
the derived avg/max views, avg likes/comments, engagement rate, reels ratio, posting cadence, last
post date, top hashtags). Sourced from Instagram's `web_profile_info` endpoint.

**stats_status**:
Outcome of the stat fetch for a row: `ok`, `private` (counts kept, view-metrics blank), `not_found`,
`blocked` (throttled / no usable session), `error`, or blank (a dead-end with no handle to fetch).

**Escalation**:
A lead-gated second resolve pass. The cheap **Sonnet** resolve agent runs first; only when it
returns `none`/`low` **and** flags `needs_escalation` (real leads but a reasoning gap, not a
dead-end) does a **warm-started Opus** agent retry with the prior findings to close the chain.

**evidence_url**:
The strongest *third-party* URL the resolver used to reach the handle (audit trail). The resolver
never fetches `instagram.com`; the profile read is Python's job.

## Relationships

- `dump` --(formula: clean/dedup/minus `output`)--> `input` --(read)--> **Workflow** --(append)--> `output`.
- `output` feeds the `input` formula, which is what makes **Resume** automatic.
- A **Resolve** agent produces a candidate handle + `match_confidence`; **Stats** + the deterministic
  bio-upgrade may raise that confidence; **Persist** writes the merged row.
- **Escalation** sits between the Sonnet resolve and Persist, firing only for promising-but-unresolved emails.

## Example dialogue

> **Dev:** "An email resolves to a handle but the stats fetch is blocked — do we write the row?"
> **Domain expert:** "Yes. Write the handle with `stats_status=blocked` and blank metrics. It still
> counts as processed, so `input` won't surface it again. A re-run won't retry it."
>
> **Dev:** "The resolver found nothing for an email. Skip it?"
> **Domain expert:** "No — write `email,,none`. A dead-end is a result. Omitting it would make the
> formula re-feed it every run."
>
> **Dev:** "How does the IG bio raise confidence to high if the agent never opens instagram.com?"
> **Domain expert:** "The stats script already fetched the profile for its numbers. `persist.py`
> checks the email against the returned bio / external_url in plain Python — no model tokens."

## Flagged ambiguities

- **Resume is the sheet, not a file.** Never reintroduce a checkpoint file; the `output` tab + the
  `input` formula are the resume mechanism.
- The always-on formula dedups by *normalized address*, so Gmail dot/+tag variants are not collapsed
  (a heavier Python clean is deliberately deferred — see the dropped `clean_emails.py`).
- `match_confidence` (email→handle) is the only confidence here; there is no separate
  "email validity" score — email cleaning lives entirely in the `input` formula.
- A single Workflow run assumes it is the only writer; two concurrent runs would double-process the
  same `input` snapshot.
