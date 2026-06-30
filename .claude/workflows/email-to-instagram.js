export const meta = {
  name: 'email-to-instagram',
  description: 'Resolve Instagram handles + public stats for users from the internal API (parallel-safe, resumable)',
  whenToUse: "Pulls users (email + name) from the internal users API, resolves each owner's Instagram handle via web OSINT (seeded with their name), fetches public stats with a direct script, and appends a row to the `output` tab of the configured Google Sheet. Parallel-safe: multiple concurrent runs claim disjoint slices via a lease-lock on the `state` tab, and resume is automatic from the stored cursor. Configure INTERNAL_API_KEY, GOOGLE_SHEETS_SPREADSHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON (+ optional INSTAGRAM_SESSIONID) in .env.",
  phases: [
    { title: 'Claim', detail: 'atomically claim the next disjoint batch of users (lease-lock + cursor)' },
    { title: 'Resolve', detail: 'per user: fast Sonnet third-party OSINT (name-seeded) -> best handle + confidence' },
    { title: 'Escalate', detail: 'lead-gated Opus pass: close chains Sonnet left low/none but promising' },
    { title: 'Persist', detail: 'per batch: fetch stats + bio-upgrade confidence + append to output' },
  ],
}

// ---------------------------------------------------------------------------
// Architecture (see ADR 0001 substrate + 0002 token-efficient core + 0003 parallel-safe claim,
// and CONTEXT.md):
//   Input is the internal users API; output is the Google Sheet `output` tab. This JS sandbox
//   cannot touch the network/filesystem, so agents shell out to the Python toolkit in scripts/:
//     - claim.py        : parallel-safe atomic claim (state-tab lease-lock -> cursor -> API page)
//     - persist.py      : stats (instagram_stats.py) + deterministic email-in-bio confidence
//                         upgrade + append-output — one near-zero-token Bash call per batch
//   The only meaningful token sink is the per-user resolve agent: Sonnet, bounded fetch budget,
//   NEVER fetches instagram.com (Python reads the profile), seeded with the user's name, with a
//   lead-gated Opus escalation only for promising-but-unresolved users.
//   args (optional): { batch_size }. Everything else comes from .env.
// ---------------------------------------------------------------------------

log('⚠️  ToS notice: stats fetching hits the public instagram.com web_profile_info endpoint '
  + '(same Instagram-ToS risk class as the old plugin free path). The resolver itself uses only '
  + 'third-party OSINT and never fetches instagram.com.')

// args may arrive as an object or a JSON string (or be absent) — coerce defensively.
let ARGS = args
if (typeof ARGS === 'string') { try { ARGS = JSON.parse(ARGS) } catch (e) { ARGS = {} } }
if (!ARGS || typeof ARGS !== 'object') ARGS = {}

const BATCH_SIZE = Number(ARGS.batch_size) > 0 ? Number(ARGS.batch_size) : 50  // users per batch (default 50)
const AGENT_CAP = 950                                        // per-run lifetime ceiling (hard cap 1000)
const TOKEN_FLOOR = 60000                                    // stop launching batches near the budget edge
const PY = '.venv/bin/python'

log(`Config: batch_size=${BATCH_SIZE} (args received as ${typeof args})`)

// --- Schemas ---------------------------------------------------------------

const CLAIM_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['users', 'exhausted', 'claim_id', 'claim_row', 'reclaimed'],
  properties: {
    users: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['email'],
        properties: {
          email: { type: 'string' },
          name: { type: 'string', description: 'owner name (first + last), may be empty' },
        },
      },
    },
    exhausted: { type: 'boolean', description: 'true when new work ran out (API returned < BATCH_SIZE)' },
    claim_id: { type: ['string', 'null'], description: 'ledger id of this claim (null if no users)' },
    claim_row: { type: ['integer', 'null'], description: 'ledger row to mark done after persist' },
    reclaimed: { type: 'boolean', description: 'true if this batch was recovered from a dead claimer' },
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
    identity_summary: { type: 'string', description: 'who the user is: name, niche, location, reused usernames, site' },
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

function claimPrompt() {
  return `Claim the next batch of users to process. This is parallel-safe — run the command exactly and return its output; do NOT reason about the data.

  ${PY} scripts/claim.py ${BATCH_SIZE}

It prints {"users": [{"email","name"}, ...], "exhausted": bool}. Return that as the structured result. If it errors, return {"users": [], "exhausted": true}.`
}

function resolvePrompt(email, name) {
  const who = name && name.trim()
    ? `EMAIL: ${email}\n  NAME: ${name}   (the account owner's real name — your strongest seed)`
    : `EMAIL: ${email}   (no name available)`
  return `You are a fast, efficient OSINT investigator. Resolve which Instagram account belongs to this person. Be relentless but TOKEN-EFFICIENT: prefer WebSearch snippets, only WebFetch a page when a snippet is genuinely promising, cap yourself to ~6-8 tool calls, and STOP the instant you have a high-confidence match.

  ${who}

HARD RULE: do NOT WebFetch instagram.com / www.instagram.com / i.instagram.com. A separate step fetches the profile. Your only job is to NAME the handle from THIRD-PARTY evidence and judge confidence.

METHOD:
1. NAME-FIRST (when a name is given) — search "<name> instagram", "<name> <any niche/brand you discover>", and the name alongside the email's local-part or domain. The name is usually the fastest path to the handle.
2. DIRECT — WebSearch the quoted email and obvious variants for a page tying it to an @handle or instagram.com/<user> link: link-in-bio aggregators (linktr.ee, beacons.ai, carrd, linkin.bio), About/Contact pages, press kits.
3. IDENTITY PIVOT — resolve niche, location, personal site, and usernames reused on other platforms (start with the local-part, then GitHub / X / YouTube / TikTok / LinkedIn); check whether another-platform profile names their Instagram. ONE pivot round — do not spiral.

Self-verify skeptically before answering: does the chain actually return to THIS person/email? Assign match_confidence:
  - high   : email or full name + a single handle on the same third-party page.
  - medium : corroborated identity pivot (name + niche + a reused username line up).
  - low    : a single weak hop or an unverified guess.
  - none   : genuine dead-end, no public footprint -> username null.

Set needs_escalation=true ONLY when you found real leads (resolved identity attributes AND/OR >=1 candidate handle) but could not confidently close to high — i.e. a more capable model might disambiguate. Set false for clean high/medium hits and for true dead-ends (no leads).

Record every candidate you considered in candidates (username without @, confidence, one-line basis); put the single best in username (or null). evidence_url = the strongest THIRD-PARTY URL. rate_limited=true if searches/fetches were blocked. Return ONLY the structured result.`
}

function escalatePrompt(email, name, prior) {
  return `A fast pass found leads but could not confidently resolve the Instagram handle for this person. You are a more capable investigator — close the chain. Reuse the prior findings and do only a SMALL amount of extra searching (cap ~5 tool calls).

HARD RULE: do NOT WebFetch instagram.com — a separate step fetches the profile. Use third-party evidence only.

  EMAIL: ${email}
  NAME: ${(name && name.trim()) || '(none)'}
  PRIOR IDENTITY NOTES: ${(prior && prior.identity_summary) || '(none)'}
  PRIOR CANDIDATES (JSON): ${JSON.stringify((prior && prior.candidates) || [])}
  PRIOR BEST EVIDENCE: ${(prior && prior.evidence_url) || '(none)'}

Disambiguate among the candidates and/or follow ONE more pivot to confirm which handle belongs to THIS person. Assign match_confidence with the same definitions (high/medium/low/none). Put the single best handle in username (or null) and the strongest third-party URL in evidence_url. Set needs_escalation=false and rate_limited appropriately. Return ONLY the structured result.`
}

function persistPrompt(rows, claimRow, tmpfile) {
  const json = JSON.stringify({ rows, claim_row: claimRow })
  return `Persist this batch of resolved users to the output Google Sheet. Do NOT reason about the data — just write the file and run the script, then report the count.

Step 1 — use the Write tool to create the file ${tmpfile} with EXACTLY this content (one line):
${json}

Step 2 — run:
  ${PY} scripts/persist.py < ${tmpfile}

The script dedups against already-written emails, fetches Instagram stats, deterministically upgrades confidence, stamps resolved_at, appends one row per new user to the output sheet, and marks the claim done. It prints {"appended": N}. Return that N as the structured result. If it errors, return appended=0.`
}

// --- Main loop: Claim -> Resolve -> Escalate -> Persist, batch by batch ------

function deadRow(email, rateLimited) {
  return { email, username: null, match_confidence: 'none', evidence_url: '', rate_limited: !!rateLimited }
}

let agentsUsed = 0
let processed = 0
let stopReason = null
let round = 0
const pendingPersists = []   // persists run in the background so the next claim+resolve doesn't wait on the write

while (true) {
  const projected = BATCH_SIZE * 2 + 2   // claim + resolve(+escalate) per user + persist
  if (agentsUsed + projected > AGENT_CAP) { stopReason = 'agent-cap'; break }
  if (budget.total && budget.remaining() < TOKEN_FLOOR) { stopReason = 'token-budget'; break }

  round += 1

  // --- Claim (parallel-safe): atomically grab the next disjoint slice of users ---
  const claim = await agent(claimPrompt(), {
    label: `claim#${round}`, phase: 'Claim', schema: CLAIM_SCHEMA, model: 'haiku', effort: 'low',
  })
  agentsUsed += 1
  const users = (claim && Array.isArray(claim.users)) ? claim.users.filter((u) => u && u.email) : []
  const claimRow = claim ? claim.claim_row : null
  const reclaimed = !!(claim && claim.reclaimed)
  // Empty means no recoverable orphan AND no new work -> truly done. (We do NOT stop on `exhausted`
  // alone: once new work runs out, later claims may still reclaim a dead run's orphaned batch.)
  if (users.length === 0) break

  // --- Resolve (Sonnet): the ONLY per-batch barrier. Once "fetching" is done we can claim the next
  // batch; escalation + persist for this batch run in the background (below) and overlap it. ---
  const found = await parallel(users.map((u) => () => agent(resolvePrompt(u.email, u.name), {
    label: `resolve:${u.email}`, phase: 'Resolve', schema: RESOLVE_SCHEMA, model: 'sonnet', effort: 'medium',
  })))
  agentsUsed += users.length

  const baseRows = found.map((f, j) => (f ? {
    email: f.email || users[j].email,
    username: f.username ?? null,
    match_confidence: f.match_confidence || 'none',
    evidence_url: f.evidence_url || '',
    rate_limited: !!f.rate_limited,
  } : deadRow(users[j].email, true)))

  // Lead-gated escalation targets, decided from Sonnet output before backgrounding.
  const escTargets = []
  found.forEach((f, j) => {
    const b = baseRows[j]
    if (f && (b.match_confidence === 'none' || b.match_confidence === 'low') && f.needs_escalation) {
      escTargets.push({ idx: j, u: users[j], found: f })
    }
  })
  agentsUsed += escTargets.length + 1   // escalations + the persist agent (fired below)

  const rlCount = baseRows.filter((r) => r.rate_limited).length   // rate-limit signal from Sonnet (sync)

  // --- Background: Escalate (Opus) -> Persist for THIS batch. Not awaited here, so the next batch
  // claims+resolves while these run. Escalation must complete before persist (it can change the row),
  // so they're chained inside one background task. ---
  const thisRound = round
  const claimRowN = claimRow
  const tag = reclaimed ? ' (recovered orphan)' : ''
  processed += users.length

  const p = (async () => {
    const escResults = await parallel(escTargets.map((t) => () =>
      agent(escalatePrompt(t.u.email, t.u.name, t.found), {
        label: `escalate:${t.u.email}`, phase: 'Escalate', schema: RESOLVE_SCHEMA, model: 'opus', effort: 'high',
      }).then((up) => ({ idx: t.idx, up }))))
    for (const er of escResults) {
      if (er && er.up) {
        const b = baseRows[er.idx]
        baseRows[er.idx] = {
          email: er.up.email || b.email,
          username: er.up.username ?? null,
          match_confidence: er.up.match_confidence || b.match_confidence,
          evidence_url: er.up.evidence_url || b.evidence_url,
          rate_limited: !!er.up.rate_limited || b.rate_limited,
        }
      }
    }
    const persistRows = baseRows.map((r) => ({
      email: r.email, username: r.username, match_confidence: r.match_confidence, evidence_url: r.evidence_url,
    }))
    const resolvedCount = persistRows.filter((r) => r.username).length
    const wrote = await agent(persistPrompt(persistRows, claimRowN, `/tmp/persist_${thisRound}.json`), {
      label: `persist#${thisRound}`, phase: 'Persist', schema: PERSIST_SCHEMA, model: 'haiku', effort: 'low',
    })
    log(`Batch ${thisRound}${tag}: ${resolvedCount} resolved, ${escTargets.length} escalated, ${(wrote && wrote.appended) || 0} rows appended (${processed} processed so far).`)
  })().catch((e) => log(`Batch ${thisRound} escalate/persist failed (${e}); its claim will be reclaimed from the ledger.`))
  pendingPersists.push(p)

  if (rlCount > users.length / 2) { stopReason = 'rate-limited'; break }
}

// Drain outstanding background escalate/persist tasks before returning, so we don't exit mid-write.
if (pendingPersists.length) {
  log(`Waiting for ${pendingPersists.length} in-flight escalate/write task(s) to finish…`)
  await Promise.all(pendingPersists)
}

if (stopReason) {
  log(`Stopped early (${stopReason}) after ${processed} users this run. Re-run to resume from the `
    + 'stored cursor — concurrent re-runs are safe (each claims a disjoint slice). Orphaned batches '
    + 'from a crashed run are reclaimed automatically once their lease expires.')
} else {
  log(`Complete: ${processed} users processed this run; no new work and no recoverable orphans remain.`)
}

return {
  processed,
  stopped: stopReason,
  done: !stopReason,
}
