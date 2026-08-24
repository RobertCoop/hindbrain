---
name: mem-init
description: Use when initializing or seeding the hindbrain memory store for a project for the first time — surveying an existing codebase and its documentation to create the initial set of durable memories via the mem CLI. Triggers on requests like "initialize memory for this project", "seed the memory bank", "bootstrap hindbrain here", or the /hindbrain:mem-init command.
---

# mem-init — seed the memory store from an existing project

You are bootstrapping hindbrain's store for a project it has never seen. Normally
memories are earned — a failure is fixed, a correction lands, the user says
"remember" — and that evidence trail is what makes the store trustworthy. A
bootstrap has no evidence trail, so it must substitute **your verification** for
lived experience, and it must be **sparing**: the store's value is precision.
Twenty memories that fire at the right moment beat two hundred that teach the
gates to be ignored. Aim for **10–30 memories** in a typical project; going past
40 needs an unusually large or trap-dense codebase.

The one-line test for every candidate: **would this note, surfaced at the right
moment a month from now, change what the agent does?** If it only describes,
skip it. If it's obvious from a glance at the repo, skip it. If CLAUDE.md or the
auto-memory files already say it, skip it — never duplicate those layers, and
never write to them.

## Procedure

Work in five passes. Do not save anything until pass 3.

### Pass 1 — Survey (read, don't save)

Three ways to run the survey, by decreasing leverage — use the best one your
environment offers. On every path the survey is **read-only**: nothing saves
until pass 3, in your own context, after your own review.

- **Plugin workflow** (best): run the `mem-init-scan` workflow
  (`/hindbrain:mem-init-scan`, or the Workflow tool with the plugin-namespaced
  name), passing any user focus hint as args. It fans out four read-only
  scouts over the sources below, dedupes, judges, and returns 10–30 ranked
  proposals plus a cut summary — your context never sees the raw survey. Then
  skip to pass 2 treating the proposals as your candidate list (the judge
  already applied the bar once; you still verify and dedup before saving).
- **Subagents** (no workflows): spawn read-only `mem-scout` agents (the
  plugin ships the agent definition), one per source group below; merge their
  candidate lists yourself.
- **Inline** (neither): start with `mem scout --json` — the deterministic
  survey that greps confession comments, task-runner/CI recipes, git churn
  and suspicious commits, and environment shape for you — then read only its
  hits plus the judgment-heavy sources (docs/ADRs) yourself.

The sources, in rough order of yield:

1. **Task-runner and CI truth** — Makefile/justfile/package.json scripts,
   tox.ini/noxfile, `.github/workflows/*`: the flags, env vars, and orderings a
   command *actually* needs (`PYTHONPATH=…`, `--no-sandbox`, a required
   pre-step). CI is the highest-value source here: it encodes what maintainers
   had to do to make things pass.
2. **Confession comments** — grep the codebase for
   `HACK|WORKAROUND|XXX|FIXME|NOTE:|careful|don't|do not|must not|gotcha|footgun`
   (case-insensitive). A comment warning the next human is a gotcha wanting to
   be a memory. (`mem scout` finds these mechanically.)
3. **Git history** — `git log --oneline -60`, plus
   `git log --grep 'revert\|fix.*again\|workaround' -i --oneline`. Reverts,
   repeated fixes to one file, and "make X work on Y" commits are footguns with
   receipts. `git log --follow` on suspicious files if needed. (`mem scout`
   surfaces suspicious commits and churn hotspots; you supply the reading.)
4. **Decision records** — ADRs, design docs, RFC directories, or decision
   paragraphs in docs: choices **with rationale**, especially where an obvious
   alternative was rejected.
5. **Environment shape** — docker-compose/devcontainer (ports, service names,
   volumes), `.env.example` (names only — values are secrets), pinned tool
   versions, required system deps.
6. **README / CONTRIBUTING / docs — caveats only.** Documentation is mostly
   *derivable*: the agent can read it any time, so do not transcribe it.
   Extract only the caveat-shaped sentences — "note that…", "X will fail
   unless…", "always/never…", version constraints, platform quirks — and
   decision rationale. Skip API references, tutorials, feature descriptions,
   and anything aspirational (docs describing what *should* be true are a
   contradiction risk, not a memory).

### Pass 2 — Filter

Run every candidate through the four-part bar: **durable** (true next month),
**non-obvious** (not derivable from the repo in one glance — the file's own
content doesn't count as non-obvious), **actionable** (changes a command, an
edit, or a choice), **scoped** (attachable to a command, path, or this
project). Drop everything else. Explicitly drop:

- restatements of CLAUDE.md, MEMORY.md, or lint/formatter config (the tools enforce those)
- project descriptions, directory layouts, API surfaces — derivable
- secrets or credential values in any form (the CLI refuses them anyway)
- transient state: open bugs, current branch work, TODO lists
- style preferences with no evidence the user holds them

### Pass 3 — Verify, then save

Docs and comments can be stale — **verify cheap claims before saving them**
(≤ ~30s each): run the command with `--help` or a dry-run, check the config
file actually sets what the doc claims, confirm the version pin. A claim you
verified is saved plainly; a plausible claim you could not verify is either
skipped or saved with lower strength and a body that attributes it
("per CONTRIBUTING.md: …").

**Dedup before every save**: `mem search "<key terms>"` first; if a near-match
exists, `mem corroborate <id>` instead of saving a sibling.

Then save with deliberate **type, scope, and strength**:

**Kind** — `gotcha` (trap + the way around it; the highest-value kind),
`procedure` (multi-step how-to that works), `decision` (choice + rationale),
`env` (machine/tooling state; gets a TTL automatically), `fact` (durable
project/domain fact), `preference` (only with evidence the user actually holds
it — rare in a bootstrap).

**Scope** — narrowest scope that is actually true:
`command:<head>` (e.g. `command:pytest`, `command:git.push`) for anything
keyed to running a tool; `path:<glob>` for anything keyed to editing files;
`project` for facts/decisions; `global` only for truths about the *user's
machine or tooling* that hold across projects. When one lesson spans sibling
commands, use the `|` form (`command:pytest|make`).

**Strength** — three independent levers:

- `--prior` (0.5–3.0): cold-start activation. `3.0` = verified, likely to
  recur soon (the CI-required env var, the destructive-command trap);
  `2.0` = solid but occasional; `1.0` (default) = plausible-but-unverified
  doc extractions. Prior washes out as real usage accumulates, so err low.
- `mem pin <id>`: always in the session-start profile. Budget **≤ 3 pins**,
  reserved for things needed in essentially every session.
- `--hazard` (with `--hazard-mode deny|ask`): only for **confirmed**
  destructive footguns tied to a consequential command (a force-push that
  broke the remote, a migration that ate data). Note: hazards only actually
  block at `full` authority, which a bootstrap save cannot grant itself —
  flag these for user confirmation in your report (below).

**Authority is not yours to set.** Bootstrap saves are agent-authored with no
witnessed evidence, so they land at `pending` — retrievable and remind-eligible,
but not command-adjacent-injectable until corroborated (`mem corroborate` in a
later session when a note proves out, or the user runs `mem confirm <id>`).
Do not try to raise it with flags; the witness tests will ignore you. The only
honest downgrade: content quoted from *fetched web pages* gets
`--channel external` (quarantined).

**Write titles as the injection surface**: the one line you'd want to see just
before repeating the mistake — mechanism and remedy, not topic. Tag with
synonyms and sibling invocations (`pytest` → `tests,test,make`), and for every
`decision`, **tag the rejected alternative** — the violating code will contain
the *other* word (`uuid` on a "use ULIDs" decision).

### Pass 4 — Review

`mem list --project` and reread everything you saved as if you were the gate:
titles that would read as noise at the wrong moment get tightened or refuted
(`mem refute <id>`); near-duplicates get consolidated via `mem supersede`.
Check total count against the 10–30 target — if you're over, cut from the
bottom of the value ranking, not the top.

### Pass 5 — Report

End with a short report to the user:

1. Count saved, by kind, plus anything pinned.
2. **The confirm list**: the 3–8 highest-stakes notes (all hazards, plus any
   note whose wrongness would be costly), each as a ready-to-run
   `mem confirm <id>` line — user confirmation grants `full` authority and is
   what arms the deny tier.
3. What you deliberately did *not* save (one line: e.g. "skipped API docs and
   layout — derivable"), so the user can redirect if they wanted more.
4. Remind the user the store self-tunes from here: notes that prove out get
   corroborated and escalate; wrong ones can be `mem refute`'d; nothing else
   is needed day-to-day.
