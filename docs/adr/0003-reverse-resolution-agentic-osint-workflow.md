# Reverse email→handle resolution as an agentic web-OSINT Workflow

The `email-to-instagram` capability resolves, for each email in an input file, the Instagram
**username** it belongs to, emitting a CSV of `email,username,match_confidence`. It is built as a
Claude **Workflow** (`.claude/workflows/email-to-instagram.js`) that fans out subagents — **not**
as a reverse `BasePlugin`. Per email it runs a multi-method sweep (web search, link-in-bio
aggregators, IG-direct handle guesses) and, when no page directly ties the email to a handle, a
bounded **stepping-stone pivot** loop: resolve the owner's identity attributes (name, niche,
reused usernames, personal site) and feed them back as new search seeds until a handle is confirmed
or the rounds run out. A synthesize/verify agent confirms the chain closes back to the email before
emitting the single best handle.

This was chosen over a reverse plugin because the work is open-ended, agentic OSINT — variable
numbers of search hops and tool calls per email that don't fit the plugin's fixed
`Discovery → Enrichment → Email → Verification` layer/provider/tier machinery, and because the
user wants "any method necessary" rather than a fixed env-gated provider chain.

**Consequence — ToS posture inherited from ADR 0002.** Closing a chain often requires WebFetching
`instagram.com` profiles to confirm a bio contains the email; this is the same Instagram-ToS
enforcement-risk class as the plugin's free path. It is accepted deliberately, the workflow logs a
one-line ToS notice at runtime, and verify agents prefer third-party corroboration (linktr.ee,
blogs, About pages) first, hitting instagram.com only to close the chain. There is no tier cap —
the workflow is inherently free-tier OSINT.

**Consequence — diverges from the framework's write-once model.** CLAUDE.md mandates "buffer →
merge → write once" for the plugin framework. This Workflow instead processes emails in batches and
**appends** to the CSV after each batch while advancing a sidecar `checkpoint.json`
(`{input, output, last_index, processed, total}`), so a kill or rate-limit leaves CSV and checkpoint
consistent and re-running with the same args resumes from `last_index + 1`. A rate-limit guard
checkpoints and stops early when a batch's finders error above a threshold. Every processed email
yields exactly one row, including dead-ends (`email,,none`), so resume never re-runs a finished
email. The divergence is acceptable because this is a separate artifact outside the plugin
framework's contract.

**Consequence — coverage is best-effort and partial.** Many emails are not publicly tied to any
handle; `match_confidence` (`high`/`medium`/`low`/`none`) encodes the directness of the resolved
chain, and `none` rows are expected and normal, not failures.