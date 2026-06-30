export const meta = {
  name: 'email-to-instagram',
  description: 'Resolve Instagram handles + public stats for emails in a Google Sheet (Sheets-native, resumable)',
  whenToUse: "Emails live in the `dump` tab of a Google Sheet; a formula builds the `input` tab (cleaned, deduped, minus already-processed). This workflow reads `input`, resolves each owner's Instagram handle via web OSINT, fetches public stats with a direct script, and appends a row to `output`. Resume is automatic — just re-run; `input` already excludes processed emails. Configure GOOGLE_SHEETS_SPREADSHEET_ID + GOOGLE_SERVICE_ACCOUNT_JSON (+ optional INSTAGRAM_SESSIONID) in .env.",
  phases: [
    { title: 'Load', detail: 'ensure tabs + read the pending email list from the input sheet' },
    { title: 'Resolve', detail: 'per email: fast Sonnet third-party OSINT sweep -> best handle + confidence' },
    { title: 'Escalate', detail: 'lead-gated Opus pass: close chains Sonnet left low/none but promising' },
    { title: 'Persist', detail: 'per batch: fetch stats + bio-upgrade confidence + append to output' },
  ],
}

// ---------------------------------------------------------------------------
// Architecture (see ADR 0004 + 0005, CONTEXT.md):
//   Google Sheets is the data substrate. This JS sandbox cannot touch the network/filesystem, so
//   agents shell out to the Python toolkit in scripts/ for ALL I/O and stats:
//     - sheets_io.py    : ensure-sheets / read-input / append-output (gspread + service account)
//     - persist.py      : stats (instagram_stats.py) + deterministic email-in-bio confidence
//                         upgrade + append — one near-zero-token Bash call per batch
//   The only meaningful token sink is the per-email resolve agent, so it is deliberately cheap:
//   Sonnet, bounded fetch budget, NEVER fetches instagram.com (Python reads the profile), with a
//   lead-gated Opus escalation only for promising-but-unresolved emails.
//   args (optional): { batch_size }. Everything else comes from .env.
// ---------------------------------------------------------------------------

log('⚠️  ToS notice: stats fetching hits the public instagram.com web_profile_info endpoint '
  + '(same Instagram-ToS risk class as the old plugin free path). The resolver itself uses only '
  + 'third-party OSINT and never fetches instagram.com.')

const BATCH_SIZE = (args && Number(args.batch_size)) || 10   // emails per persist/append unit
const MAX_PER_RUN = (args && Number(args.max_per_run)) || 400 // emails fetched per run (resume covers the rest)
const AGENT_CAP = 950                                        // per-run lifetime ceiling (hard cap 1000)
const TOKEN_FLOOR = 60000                                    // stop launching batches near the budget edge
const PY = '.venv/bin/python'

// --- Schemas ---------------------------------------------------------------

const LOAD_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['emails'],
  properties: {
    emails: { type: 'array', items: { type: 'string' }, description: 'cleaned, not-yet-processed emails from the input tab' },
  },
}

const RESOLVE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['email', 'username', 'match_confidence', 'needs_escalation', 'rate_limited'],
  properties: {
    email: { type: 'string' },
    username: { type: ['string', 'null'], description: 'best Instagram handle without @, or null' },
    match_confidence: { type: 'string', enum: ['high', 'medium', 'low', 'none'] },
    evidence_url: { type: 'string', description: 'strongest THIRD-PARTY evidence URL (not instagram.com)' },
    identity_summary: { type: 'string', description: 'who owns the email: name, niche, location, reused usernames, site' },
    candidates: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['username', 'confidence', 'basis'],
        properties: {
          username: { type: 'string' },
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
          basis: { type: 'string', description: 'one line: how this candidate was reached' },
        },
      },
    },
    needs_escalation: { type: 'boolean', description: 'true ONLY when real leads exist but could not close to high' },
    rate_limited: { type: 'boolean', description: 'true if searches/fetches were blocked or throttled' },
  },
}

const PERSIST_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['appended'],
  properties: { appended: { type: 'integer' } },
}

// --- Prompts ---------------------------------------------------------------

function loaderPrompt() {
  return `Set up the Google Sheet and read the next slice of pending emails. Run these two commands and return the email list — do NOT reason about the data.

1) ${PY} scripts/sheets_io.py ensure-sheets
2) ${PY} scripts/sheets_io.py read-input ${MAX_PER_RUN}

Command (2) prints {"emails": [...]} — up to ${MAX_PER_RUN} cleaned, deduped, not-yet-processed emails (this run's slice; resume covers the rest). Return that emails array as the structured result. If a command errors, return an empty emails array.`
}

function resolvePrompt(email) {
  return `You are a fast, efficient OSINT investigator. Resolve which Instagram account belongs to this EXACT email. Be relentless but TOKEN-EFFICIENT: prefer WebSearch snippets, only WebFetch a page when a snippet is genuinely promising, cap yourself to ~6-8 tool calls, and STOP the instant you have a high-confidence match.

  EMAIL: ${email}

HARD RULE: do NOT WebFetch instagram.com / www.instagram.com / i.instagram.com. A separate step fetches the profile. Your only job is to NAME the handle from THIRD-PARTY evidence and judge confidence.

METHOD:
1. DIRECT — WebSearch the quoted email and obvious variants. Look for a page tying the email to an @handle or an instagram.com/<user> link: link-in-bio aggregators (linktr.ee, beacons.ai, carrd, linkin.bio), About/Contact pages, press kits.
2. IDENTITY PIVOT (when no page directly ties email->handle — the common case) — work out WHO owns the email: real name, brand, niche, location, personal site, and usernames reused on other platforms (start with the local-part, then GitHub / X / YouTube / TikTok / LinkedIn). Use those as new search seeds; check whether another-platform profile names their Instagram. ONE pivot round — do not spiral.

Self-verify skeptically before answering: does the chain actually return to THIS email/person? Assign match_confidence:
  - high   : email and a single handle on the same third-party page.
  - medium : corroborated identity pivot (real name + niche + a reused username line up).
  - low    : a single weak hop or an unverified guess.
  - none   : genuine dead-end, no public footprint -> username null.

Set needs_escalation=true ONLY when you found real leads (resolved identity attributes AND/OR >=1 candidate handle) but could not confidently close to high — i.e. a more capable model might disambiguate. Set false for clean high/medium hits and for true dead-ends (no leads).

Record every candidate you considered in candidates (username without @, confidence, one-line basis); put the single best in username (or null). evidence_url = the strongest THIRD-PARTY URL. rate_limited=true if searches/fetches were blocked. Return ONLY the structured result.`
}

function escalatePrompt(email, prior) {
  return `A fast pass found leads but could not confidently resolve the Instagram handle for this email. You are a more capable investigator — close the chain. Reuse the prior findings and do only a SMALL amount of extra searching (cap ~5 tool calls).

HARD RULE: do NOT WebFetch instagram.com — a separate step fetches the profile. Use third-party evidence only.

  EMAIL: ${email}
  PRIOR IDENTITY NOTES: ${(prior && prior.identity_summary) || '(none)'}
  PRIOR CANDIDATES (JSON): ${JSON.stringify((prior && prior.candidates) || [])}
  PRIOR BEST EVIDENCE: ${(prior && prior.evidence_url) || '(none)'}

Disambiguate among the candidates and/or follow ONE more pivot to confirm which handle belongs to THIS email/person. Assign match_confidence with the same definitions (high/medium/low/none). Put the single best handle in username (or null) and the strongest third-party URL in evidence_url. Set needs_escalation=false and rate_limited appropriately. Return ONLY the structured result.`
}

function persistPrompt(rows, tmpfile) {
  const json = JSON.stringify({ rows })
  return `Persist this batch of resolved emails to the output Google Sheet. Do NOT reason about the data — just write the file and run the script, then report the count.

Step 1 — use the Write tool to create the file ${tmpfile} with EXACTLY this content (one line):
${json}

Step 2 — run:
  ${PY} scripts/persist.py < ${tmpfile}

The script fetches Instagram stats, deterministically upgrades confidence, stamps resolved_at, and appends one row per email to the output sheet. It prints {"appended": N}. Return that N as the structured result. If it errors, return appended=0.`
}

// --- Phase 1: Load ---------------------------------------------------------

phase('Load')

const loaded = await agent(loaderPrompt(), {
  label: 'load:sheet', phase: 'Load', schema: LOAD_SCHEMA, model: 'haiku', effort: 'low',
})

const emails = (loaded && Array.isArray(loaded.emails)) ? loaded.emails : []
const TOTAL = emails.length
if (TOTAL === 0) {
  log('No pending emails in the input tab. Nothing to do (or everything is already processed).')
  return { total: 0, processed: 0, done: true }
}
log(`${TOTAL} pending emails to resolve (batch size ${BATCH_SIZE}).`)

// --- Phase 2/3/4: Resolve -> Escalate -> Persist, per batch -----------------

phase('Resolve')

let agentsUsed = 1     // the loader
let processed = 0
let stopReason = null

function deadRow(email, rateLimited) {
  return { email, username: null, match_confidence: 'none', evidence_url: '', rate_limited: !!rateLimited }
}

for (let b = 0; b < TOTAL; b += BATCH_SIZE) {
  const batchEmails = emails.slice(b, b + BATCH_SIZE)
  const projected = batchEmails.length * 2 + 1   // resolve (+ maybe escalate) per email, + 1 persist
  if (agentsUsed + projected > AGENT_CAP) { stopReason = 'agent-cap'; break }
  if (budget.total && budget.remaining() < TOKEN_FLOOR) { stopReason = 'token-budget'; break }

  // resolve -> conditional escalate, each email an independent chain (no barrier between stages)
  let escalations = 0
  const results = await pipeline(
    batchEmails,
    (email) => agent(resolvePrompt(email), {
      label: `resolve:${email}`, phase: 'Resolve', schema: RESOLVE_SCHEMA, model: 'sonnet', effort: 'medium',
    }),
    (found, email) => {
      if (!found) return deadRow(email, true)            // agent skipped/died -> retry on resume
      const base = {
        email: found.email || email,
        username: found.username ?? null,
        match_confidence: found.match_confidence || 'none',
        evidence_url: found.evidence_url || '',
        rate_limited: !!found.rate_limited,
      }
      const leadGated = (base.match_confidence === 'none' || base.match_confidence === 'low') && found.needs_escalation
      if (!leadGated) return base
      escalations += 1
      return agent(escalatePrompt(email, found), {
        label: `escalate:${email}`, phase: 'Escalate', schema: RESOLVE_SCHEMA, model: 'opus', effort: 'high',
      }).then((up) => {
        if (!up) return base    // escalation died -> keep Sonnet's result
        return {
          email: up.email || email,
          username: up.username ?? null,
          match_confidence: up.match_confidence || base.match_confidence,
          evidence_url: up.evidence_url || base.evidence_url,
          rate_limited: !!up.rate_limited || base.rate_limited,
        }
      })
    },
  )

  agentsUsed += batchEmails.length + escalations   // resolve agents + the escalations that fired

  const rows = results.map((r, j) => r || deadRow(batchEmails[j], true))
  const rlCount = rows.filter((r) => r.rate_limited).length

  // persist: stats + bio-upgrade + append (one cheap, near-zero-token Bash agent)
  const persistRows = rows.map((r) => ({
    email: r.email, username: r.username, match_confidence: r.match_confidence, evidence_url: r.evidence_url,
  }))
  const lastIdx = Math.min(b + BATCH_SIZE, TOTAL) - 1
  const wrote = await agent(persistPrompt(persistRows, `/tmp/persist_batch_${lastIdx}.json`), {
    label: `persist@${lastIdx}`, phase: 'Persist', schema: PERSIST_SCHEMA, model: 'haiku', effort: 'low',
  })
  agentsUsed += 1
  processed += batchEmails.length

  const resolvedCount = rows.filter((r) => r.username).length
  log(`Batch done: ${processed}/${TOTAL} processed, ${resolvedCount} resolved this batch, ${(wrote && wrote.appended) || 0} rows appended.`)

  if (rlCount > batchEmails.length / 2) { stopReason = 'rate-limited'; break }
}

// TOTAL is only this run's slice (<= MAX_PER_RUN); a full slice means more remain in the sheet.
const moreLikelyPending = TOTAL >= MAX_PER_RUN
if (stopReason) {
  log(`Stopped early (${stopReason}) at ${processed}/${TOTAL} of this run's slice. Re-run with the `
    + 'SAME args to resume — the input tab already excludes everything written to output.')
} else if (moreLikelyPending) {
  log(`Slice complete: ${processed} emails processed this run. More remain in the input tab — `
    + `re-run to process the next ~${MAX_PER_RUN}.`)
} else {
  log(`Complete: ${processed}/${TOTAL} emails processed; the input tab is now drained.`)
}

return {
  total: TOTAL,
  processed,
  stopped: stopReason,
  done: !stopReason && !moreLikelyPending,
}
