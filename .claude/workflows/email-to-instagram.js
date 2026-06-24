export const meta = {
  name: 'email-to-instagram',
  description: 'Reverse-resolve Instagram usernames from a list of emails via agentic web OSINT (resumable, checkpointed)',
  whenToUse: 'Given a file of emails, find each owner\'s Instagram handle. Output CSV: email,username,match_confidence. Re-run with the same args to resume.',
  phases: [
    { title: 'Load', detail: 'read email file + checkpoint, compute resume point' },
    { title: 'Resolve', detail: 'per email: multi-method OSINT sweep + stepping-stone pivot, then skeptical verify' },
    { title: 'Write', detail: 'append each batch to CSV, advance checkpoint (kill/rate-limit safe)' },
  ],
}

// ---------------------------------------------------------------------------
// Args: a string path to the email file, OR { input, output }.
//   input  - newline-delimited emails OR a CSV with an email column.
//   output - CSV path (default email_to_instagram.csv in CWD).
// Output : CSV `email,username,match_confidence`, one row per processed email
//          (dead-ends included as `email,,none`). Sidecar checkpoint json next
//          to the output enables user-driven resume. See ADR 0003 + CONTEXT.md.
// ---------------------------------------------------------------------------

const inputPath = typeof args === 'string' ? args : (args && args.input)
const outputPath = (args && typeof args === 'object' && args.output) || 'email_to_instagram.csv'
if (!inputPath) {
  throw new Error('email-to-instagram: provide an input file path as args (string) or { input, output }')
}
const checkpointPath = outputPath.replace(/\.csv$/i, '') + '.checkpoint.json'

const BATCH_SIZE = 25          // emails per batch (checkpoint granularity); kept well under the agent cap
const AGENT_CAP = 950          // per-run lifetime ceiling (hard cap is 1000); resume covers the rest
const TOKEN_FLOOR = 60000      // stop launching batches when the turn's token budget is nearly spent

log('⚠️  ToS notice: this workflow performs web OSINT and may WebFetch instagram.com to confirm a match — '
  + 'same Instagram-ToS risk class as ADR 0002. Third-party sources are preferred; instagram.com is hit only to close a chain.')

// --- Schemas ---------------------------------------------------------------

const LOAD_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['emails', 'last_index'],
  properties: {
    emails: { type: 'array', items: { type: 'string' } },
    last_index: { type: 'integer', description: 'index of last already-processed email from the checkpoint, or -1' },
  },
}

const FIND_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['email', 'candidates', 'rate_limited'],
  properties: {
    email: { type: 'string' },
    identity_summary: { type: 'string', description: 'who the email owner is: name, niche, location, reused usernames, site' },
    candidates: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['username', 'confidence', 'evidence_urls', 'basis'],
        properties: {
          username: { type: 'string', description: 'Instagram handle without the @' },
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
          evidence_urls: { type: 'array', items: { type: 'string' } },
          basis: { type: 'string', description: 'one line: how this candidate was reached' },
        },
      },
    },
    rate_limited: { type: 'boolean', description: 'true if searches/fetches were blocked or throttled' },
  },
}

const ROW_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['email', 'username', 'match_confidence'],
  properties: {
    email: { type: 'string' },
    username: { type: ['string', 'null'], description: 'verified handle without @, or null if none' },
    match_confidence: { type: 'string', enum: ['high', 'medium', 'low', 'none'] },
  },
}

const WRITE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['appended'],
  properties: { appended: { type: 'integer' } },
}

// --- Prompts ---------------------------------------------------------------

function findPrompt(email) {
  return `You are a thorough, intelligent OSINT investigator. Resolve which Instagram account belongs to this exact email address:

  EMAIL: ${email}

Use ANY method. Make as many WebSearch and WebFetch calls as you need — be relentless but efficient.

METHOD (adapt freely):
1. DIRECT — WebSearch the quoted email "${email}" and obvious variants. Look for any page where the email appears next to an Instagram @handle or an instagram.com/<user> link: link-in-bio aggregators (linktr.ee, beacons.ai, carrd, linkin.bio), personal / About / Contact pages, press kits, media pages, or the IG bio itself.
2. IDENTITY PIVOT (use this whenever no page directly ties the email to a handle — the common case) — first work out WHO owns the email: real name, brand, niche/topic, location, employer, personal website/domain, and usernames reused on OTHER platforms (start with the email's local-part, then GitHub, X/Twitter, YouTube, TikTok, LinkedIn). Then use those as NEW search seeds and follow the chain:
   - search "<name> <niche> <city> instagram"
   - try the reused username directly: WebFetch instagram.com/<username>
   - follow the social links on their personal site
   - check whether an X / LinkedIn / YouTube / TikTok profile names their Instagram handle
   Each finding is a stepping stone to the next. Iterate up to ~3 pivot rounds; stop early the moment you have a confirmed high-confidence match.

For every candidate handle you find, record: username (no @), your confidence (high/medium/low), the evidence URLs, and a one-line basis describing how you reached it. Prefer third-party evidence; you MAY WebFetch instagram.com/<handle> to check whether the bio shows this email, but use Instagram sparingly.

If your searches/fetches start getting blocked or throttled, set rate_limited=true so the run can pause and resume later.
If you genuinely find nothing after pivoting, return an empty candidates array. Return ONLY the structured result.`
}

function verifyPrompt(email, found) {
  return `You are a skeptical verifier. An OSINT pass proposed candidate Instagram handles for this email; confirm which (if any) ACTUALLY belongs to it, then emit the single best result.

  EMAIL: ${email}
  IDENTITY NOTES: ${found.identity_summary || '(none)'}
  CANDIDATES (JSON): ${JSON.stringify(found.candidates)}

Verify by WebFetching the strongest evidence URLs — third-party first (linktr.ee / personal site / press / other-platform bios) — and only if needed to close the chain, WebFetch instagram.com/<handle> to check the bio or links. Reject any candidate whose chain does not genuinely return to THIS email / person.

Assign match_confidence:
  - high   : the email and a single handle appear on the same page, OR the IG bio shows this exact email.
  - medium : reached via a corroborated identity pivot (e.g. matched real name + niche + a username reused across platforms).
  - low    : a single weak hop or an unverified guess.
If nothing verifies, return username=null and match_confidence="none".
If the person plausibly runs multiple accounts, pick the highest-reach / most relevant ONE (one row only).
Return ONLY { email, username, match_confidence }.`
}

function writerPrompt(rows, newLastIndex) {
  const checkpointJson = JSON.stringify({
    input: inputPath, output: outputPath, last_index: newLastIndex, processed: newLastIndex + 1, total: TOTAL,
  })
  return `Append rows to a CSV and update a checkpoint file, using the Read / Write / Bash tools. Be precise — this run depends on it for resume safety.

CSV FILE: ${outputPath}
  - If it does NOT exist yet, create it and write this exact header as the first line:
      email,username,match_confidence
  - Then APPEND one line per row below. Do NOT rewrite or reorder existing lines (read the current file and write it back with the new lines appended, or use a Bash append).
  - CSV-escape every field: if a value contains a comma, double-quote, or newline, wrap it in double quotes and double any internal double-quotes. A null/empty username must be written as an empty field (nothing between the commas).
  ROWS (JSON): ${JSON.stringify(rows)}

CHECKPOINT FILE: ${checkpointPath}
  - Overwrite it (create if missing) with EXACTLY this JSON content:
      ${checkpointJson}

Return how many rows you appended to the CSV.`
}

// --- Phase 1: Load ---------------------------------------------------------

phase('Load')

const loaded = await agent(
  `You are the loader for a reverse email->Instagram workflow.

1. Read the email list file at: ${inputPath}
   - It is either newline-delimited email addresses OR a CSV that has an email column. Use the Read tool (and Bash if it helps).
   - Extract every syntactically valid email address, lowercase them, and de-duplicate while PRESERVING first-seen order.
2. Read the checkpoint file at: ${checkpointPath} if it exists (Read tool).
   - It is JSON like {"input":...,"output":...,"last_index":N,"processed":P,"total":T}.
   - Return its last_index. If the file is missing or unreadable, return last_index = -1.

Return the full ordered unique email list and last_index. Return ONLY the structured result.`,
  { phase: 'Load', schema: LOAD_SCHEMA }
)

const emails = (loaded && loaded.emails) || []
const TOTAL = emails.length
const lastIndex = loaded ? loaded.last_index : -1
const startIndex = lastIndex + 1

if (TOTAL === 0) {
  log(`No emails found in ${inputPath}. Nothing to do.`)
  return { output: outputPath, checkpoint: checkpointPath, total: 0, processed: 0, done: true }
}
if (startIndex >= TOTAL) {
  log(`Already complete: ${TOTAL}/${TOTAL} processed (last_index=${lastIndex}). CSV: ${outputPath}`)
  return { output: outputPath, checkpoint: checkpointPath, total: TOTAL, processed: TOTAL, resumed_from: startIndex, done: true }
}
log(`${TOTAL} emails; resuming at index ${startIndex} (already processed: ${startIndex}).`)

// --- Phase 2/3: Resolve in batches, append + checkpoint after each ----------

phase('Resolve')

let agentsUsed = 1            // the loader
let processed = startIndex
let stopReason = null

const indices = []
for (let i = startIndex; i < TOTAL; i++) indices.push(i)

for (let b = 0; b < indices.length; b += BATCH_SIZE) {
  const batchIdx = indices.slice(b, b + BATCH_SIZE)
  const projected = batchIdx.length * 2 + 1   // find + verify per email, + 1 writer
  if (agentsUsed + projected > AGENT_CAP) { stopReason = 'agent-cap'; break }
  if (budget.total && budget.remaining() < TOKEN_FLOOR) { stopReason = 'token-budget'; break }

  const batchEmails = batchIdx.map(i => emails[i])

  // find -> verify, each email an independent chain (no barrier between stages)
  const results = await pipeline(
    batchEmails,
    (email) => agent(findPrompt(email), { label: `find:${email}`, phase: 'Resolve', schema: FIND_SCHEMA }),
    (found, email) => {
      if (!found || !found.candidates || found.candidates.length === 0) {
        return { email, username: null, match_confidence: 'none', rate_limited: !!(found && found.rate_limited) }
      }
      return agent(verifyPrompt(email, found), { label: `verify:${email}`, phase: 'Resolve', schema: ROW_SCHEMA })
        .then((v) => v
          ? { email: v.email || email, username: v.username ?? null, match_confidence: v.match_confidence || 'none', rate_limited: !!found.rate_limited }
          : { email, username: null, match_confidence: 'none', rate_limited: !!found.rate_limited })
    }
  )

  agentsUsed += batchIdx.length * 2

  // a dropped item (stage threw) becomes a rate-limited dead-end so it can be retried on resume...
  // ...except we DO advance past it; treat as 'none' but count toward the rate-limit signal.
  const norm = results.map((r, j) => r || { email: batchEmails[j], username: null, match_confidence: 'none', rate_limited: true })
  const rlCount = norm.filter((r) => r.rate_limited).length
  const rows = norm.map((r) => ({ email: r.email, username: r.username ?? null, match_confidence: r.match_confidence || 'none' }))

  const newLastIndex = batchIdx[batchIdx.length - 1]
  await agent(writerPrompt(rows, newLastIndex), { label: `write@${newLastIndex}`, phase: 'Write', schema: WRITE_SCHEMA })
  agentsUsed += 1
  processed = newLastIndex + 1

  const found = rows.filter((r) => r.username).length
  log(`Batch done: ${processed}/${TOTAL} processed (${found} resolved this batch).`)

  if (rlCount > batchIdx.length / 2) { stopReason = 'rate-limited'; break }
}

if (stopReason) {
  log(`Stopped early (${stopReason}) at ${processed}/${TOTAL}. Re-run with the SAME args to resume from index ${processed}.`)
} else {
  log(`Complete: ${processed}/${TOTAL} emails processed. CSV: ${outputPath}`)
}

return {
  output: outputPath,
  checkpoint: checkpointPath,
  total: TOTAL,
  processed,
  resumed_from: startIndex,
  stopped: stopReason,
  done: !stopReason && processed >= TOTAL,
}
