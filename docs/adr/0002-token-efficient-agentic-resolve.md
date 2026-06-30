# Token-efficient agentic resolve, with deterministic verification and lead-gated escalation

Reverse resolution (email → Instagram handle) is the only meaningful token sink in the pipeline:
loading, stat-fetching, and writing are deterministic Python invoked by near-zero-token agents. The
agentic core is therefore designed to minimize tokens without giving up accuracy.

## Decision

1. **One combined resolve agent per email** (find *and* self-verify), not a finder followed by a
   separate skeptical verifier. A second verifier re-fetches the same pages, roughly doubling
   page-ingestion tokens.
2. **The LLM never fetches `instagram.com`.** The resolve agent uses only third-party OSINT (search
   snippets, link-in-bio pages, cross-platform bios) to *name* the handle. The expensive profile
   read happens once in Python (`instagram_stats.py`), which is run anyway for stats.
3. **Deterministic verification.** `persist.py` cross-checks the email against the fetched
   `biography` / `external_url` (full email, distinctive local-part, or a personal — non-freemail —
   domain link) and upgrades `match_confidence` to `high` in plain Python — zero model tokens.
4. **Cheap tiers for mechanical agents** (Haiku/low effort for the load and persist agents, which
   only run Bash); **Sonnet** for resolve.
5. **Lead-gated Opus escalation.** Only when Sonnet returns `none`/`low` **and** flags
   `needs_escalation` (real leads but a reasoning/disambiguation gap, not a dead-end) does a
   warm-started Opus agent retry — handed Sonnet's identity notes/candidates/evidence and a small
   extra fetch budget, not a fresh sweep. Dead-ends and confident hits never escalate.

## Consequences

- **Large token savings**: no second verifier, no in-agent profile fetches, bounded dead-end spend,
  and the most capable (priciest) model runs on only a small, promising subset.
- **Verification is more reliable, not less**: substring/domain matching against the real bio is
  deterministic and reproducible, where an LLM "does the bio mention this email?" pass is both
  costly and fallible.
- **Accuracy trade-off**: dropping the dedicated skeptical verifier leans on (a) the resolve agent's
  own self-verification and (b) the deterministic bio-upgrade. A handle reached purely by a weak
  pivot with no bio corroboration stays `low`/`medium` and is never silently promoted.
- **ToS posture** (inherited): stat-fetching hits the public `web_profile_info` endpoint — an
  Instagram-ToS enforcement-risk class — and the Workflow logs a one-line notice at start. The
  resolver itself touches only third-party sources.
- **Escalation depends on honest self-reporting** of `needs_escalation`; if Sonnet under-flags, some
  resolvable emails stay `low`. Tunable by loosening the gate later.
