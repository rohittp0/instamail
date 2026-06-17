# Output CSV doubles as the resume journal

**Context.** The tool must autoresume — a rerun skips emails already processed.
We needed somewhere to record what's done.

**Decision.** Use the output CSV itself as the source of truth rather than a
separate sidecar state/journal file. Rows are streamed and flushed per-email only
once *all* of that email's selected plugins complete, so any email with a row is
fully processed. On startup we read the existing CSV's `email` column and skip
those emails, appending new rows in input order. Resume requires the existing
header to exactly match the current selection's expected header; a mismatch is
fatal.

**Why.** One artifact instead of two — no risk of the CSV and a sidecar drifting
out of sync, nothing extra to clean up, and "the file you're building is the
record of progress" is easy to reason about. The header-match guard keeps
"processed" unambiguous: resume continues the *same* run, and a changed plugin set
must use a new output file.

**Consequences.** Resume granularity is the whole email (per-plugin partial
progress within one email is not resumable — an interrupted email reruns all its
plugins). Changing the plugin selection cannot extend an existing CSV in place;
it requires a fresh output file. Per-row flushing trades a little write overhead
for crash-safety.
