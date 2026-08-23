---
name: hindbrain
description: Use when saving, searching, or managing persistent memory with the mem CLI — durable gotchas, decisions, preferences, and procedures that should survive this session; also when a hindbrain nudge or reminder appears in context.
---

# hindbrain — persistent memory

A local store surfaces saved notes automatically. You interact through the `mem` CLI (run via Bash).

## When to save
Save something only if it is **durable** (true next month), **non-obvious** (not derivable from the repo in one glance), **actionable**, and **scoped**. Good: tool gotchas ("pytest here needs PYTHONPATH=src"), decisions with rationale (tag the rejected alternative), user preferences, working procedures, environment facts. Never save secrets (the CLI refuses), transient state, or anything CLAUDE.md / auto memory already covers.

## Core commands
- `mem save --kind gotcha --scope command:pytest "pytest here needs PYTHONPATH=src; bare pytest fails on imports"`
  kinds: gotcha | decision | preference | fact | procedure | env
  scopes: command:<head> | path:<glob> | tool:<pattern> | project | global
- `mem search "terms"` · `mem get <id>` (fetch a reminded note) · `mem queue` / `mem drop <n>`
- `mem corroborate <id>` when you re-confirm an existing note · `mem supersede <id> "new"` when it changed · `mem refute <id>` when it proved wrong
- `--hazard` marks a note that should block a matching dangerous command (use sparingly; reserved for confirmed footguns).

## How to respond to hindbrain output
- A **reminder** lists note ids: fetch with `mem get` if plausibly relevant before proceeding.
- A **nudge** lists drafts: accept (run the draft), edit it, or `mem drop <n>` — don't ignore silently.
- A **blocked command** shows the hazard note: modify the command accordingly, or rerun identical to escalate to the user.
- Write titles like the one line you'd want to see before repeating the mistake.
