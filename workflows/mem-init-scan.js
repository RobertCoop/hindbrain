export const meta = {
  name: 'mem-init-scan',
  description: 'Survey this project and propose the initial hindbrain memory set (no saves)',
  whenToUse: 'First-run memory bootstrap for an existing project: fans out read-only scouts over the evidence sources, dedupes and judges the candidates, and returns a ranked proposal list for the main session to review and save.',
  phases: [
    { title: 'Scout', detail: 'parallel read-only survey of the evidence sources' },
    { title: 'Judge', detail: 'dedupe, verify, rank; select the final 10-30 proposals' },
  ],
}

// The workflow never saves: subagents run without user interaction, and
// hindbrain's write philosophy keeps judgment-gated saves in the main
// session where the user can see them. Output is a proposal list.

const CANDIDATES_SCHEMA = {
  type: 'object',
  required: ['candidates'],
  properties: {
    candidates: {
      type: 'array',
      items: {
        type: 'object',
        required: ['title', 'body', 'kind', 'scope', 'evidence'],
        properties: {
          title: { type: 'string', description: 'the one line to see before repeating the mistake (<=80 chars)' },
          body: { type: 'string', description: 'mechanism and remedy, 20-2000 chars, declarative' },
          kind: { type: 'string', enum: ['gotcha', 'procedure', 'decision', 'env', 'fact', 'preference'] },
          scope: { type: 'string', description: 'command:<head> | path:<glob> | project | global' },
          tags: { type: 'string', description: 'synonyms, sibling commands, rejected alternatives' },
          prior: { type: 'number', description: '3.0 verified+recurring, 2.0 solid, 1.0 speculative' },
          hazard: { type: 'boolean' },
          evidence: { type: 'string', description: 'file:line or commit hash' },
          verified: { type: 'boolean' },
          rationale: { type: 'string' },
        },
      },
    },
  },
}

const focus = (typeof args === 'string' && args.trim()) ? args.trim() : null

const BAR = `A candidate must be DURABLE (true next month), NON-OBVIOUS (not derivable
from one glance at the repo), ACTIONABLE (changes a command, an edit, or a choice), and
SCOPED (attachable to a command, path, or this project). Skip project descriptions, API
surfaces, layouts, linter-enforced style, secrets (env var NAMES are fine, values never),
CLAUDE.md/auto-memory duplication, and anything transient or aspirational. Quality over
coverage — an empty list is a valid result. Read-only: never run mem save, never edit files.
${focus ? `The user asked you to weight the survey toward: ${focus}.` : ''}`

const SOURCES = [
  {
    key: 'scout-report',
    prompt: `Run \`mem scout --json\` via Bash from the project root (the mem CLI is on PATH or at
<plugin>/bin/mem; if the command is missing, reproduce its greps by hand: confession comments
HACK|WORKAROUND|FIXME|don't|must not, task-runner recipes with env vars, churn from git log).
Take the strongest hits and READ the surrounding code to confirm each is a real, current
constraint — not a stale comment — and capture the mechanism. ${BAR}`,
  },
  {
    key: 'docs-adrs',
    prompt: `Survey README, CONTRIBUTING, docs/, and any ADR/RFC directories of this project.
Extract ONLY caveat-shaped sentences ("X fails unless…", "always/never…", version constraints,
platform quirks) and decisions WITH rationale — never transcribe descriptive documentation.
Docs can be stale: where a claim is cheaply checkable (a config value, a --help flag), check it
and set verified accordingly. For every decision candidate, tag the rejected alternative. ${BAR}`,
  },
  {
    key: 'ci-taskrunners',
    prompt: `Survey this project's CI and task-runner truth: .github/workflows/*, Makefile,
justfile, package.json scripts, tox/nox/pytest configs, docker-compose. Report what commands
ACTUALLY need to succeed — env vars, flags, orderings, service dependencies — that a naive
invocation would miss. CI files are the highest-yield source: they encode what maintainers had
to do to make things pass. Scope these candidates to the command they belong to. ${BAR}`,
  },
  {
    key: 'git-history',
    prompt: `Mine this project's git history: \`git log --oneline -200\`, plus greps for
revert|workaround|hotfix|again|really fix. For the strongest signals, \`git show\` the commit to
capture what the trap actually was and whether it could recur. Also identify churn hotspots
(files fixed repeatedly) and read them for the underlying footgun. If the repo has little or no
history, return an empty list rather than padding. ${BAR}`,
  },
]

phase('Scout')
const scouted = await parallel(SOURCES.map(s => () =>
  agent(s.prompt, { label: `scout:${s.key}`, phase: 'Scout', schema: CANDIDATES_SCHEMA })
    .then(r => ({ source: s.key, candidates: (r && r.candidates) || [] }))))

const all = []
for (const s of scouted.filter(Boolean)) {
  for (const c of s.candidates) all.push({ ...c, source: s.source })
}
log(`${all.length} raw candidates from ${scouted.filter(Boolean).length}/4 scouts`)

// cheap script-side dedup before the judge: normalized title+scope key
const seen = new Map()
for (const c of all) {
  const key = `${(c.scope || '').toLowerCase()}|${(c.title || '').toLowerCase().replace(/[^a-z0-9 ]/g, '').split(/\s+/).sort().join(' ')}`
  const prev = seen.get(key)
  if (!prev || (c.verified && !prev.verified)) seen.set(key, c)
}
const merged = [...seen.values()]
log(`${merged.length} after script-side dedup`)

if (merged.length === 0) return { proposals: [], note: 'scouts found nothing that clears the bar' }

phase('Judge')
const judged = await agent(`You are the JUDGE for a hindbrain memory bootstrap. Below are raw
memory candidates from four read-only scouts of this project. Produce the FINAL proposal list:

1. Merge true duplicates and near-duplicates (keep the better-evidenced copy; union the tags).
2. Apply the bar strictly: durable, non-obvious, actionable, scoped. Cut restatements of
   CLAUDE.md or lint config, derivable facts, and anything transient. Check the project's
   CLAUDE.md (if present) and cut candidates it already covers.
3. Spot-verify the highest-stakes claims you can check cheaply and read-only (a config value,
   a --help flag, the file the evidence cites still saying what the candidate claims); update
   verified/prior accordingly. Downgrade prior to 1.0 for anything you could not verify.
4. For each survivor: tighten the title to the line you'd want to see before repeating the
   mistake; ensure scope is the NARROWEST true scope; ensure decisions tag the rejected
   alternative; set hazard=true only for confirmed destructive footguns on consequential
   commands.
5. Rank by expected value and keep AT MOST 30 (target 10-30; fewer is fine). Also list what
   you cut and why, in one line per theme (not per item).

Never run mem save; never edit files. Return via the schema.

RAW CANDIDATES:
${JSON.stringify(merged, null, 1)}`,
  {
    label: 'judge', phase: 'Judge', effort: 'high',
    schema: {
      type: 'object',
      required: ['proposals', 'cut_summary'],
      properties: {
        proposals: CANDIDATES_SCHEMA.properties.candidates,
        cut_summary: { type: 'array', items: { type: 'string' } },
      },
    },
  })

const proposals = (judged && judged.proposals) || merged.slice(0, 30)
log(`${proposals.length} final proposals`)
return {
  proposals,
  cut_summary: (judged && judged.cut_summary) || [],
  next_steps: 'Review each proposal, then save the keepers with: mem save --kind <kind> --scope <scope> --tags <tags> --prior <prior> [--hazard] "<body>" (add --title for a custom title). Dedup with mem search before each save. Finish by showing the user a mem confirm shortlist for the highest-stakes notes — user confirmation grants full authority and arms any hazards.',
}
