# hindbrain

The reflex-and-memory layer beneath the model's cortex. hindbrain is a Claude Code **plugin** that gives the agent persistent, retrieval-gated memory via lifecycle hooks. On the read side, before and around operations a local SQLite store is searched and results surface in three tiers: **inject** (full note placed in context), **remind** (a one-line pointer plus a fetch verb), or **deny** (a hazard-flagged memory blocks a consequential command). On the write side nothing is auto-extracted: observers watch for signals (a failure followed by a fix, a correction, an explicit "remember this"), queue candidates, and the Stop hook nudges the agent with pre-drafted `mem save` commands — the agent authors every memory, the hooks only guarantee the ask.

An offline consolidator dedupes, promotes, expires, detects contradictions, and *proposes* graduations to CLAUDE.md (it never writes CLAUDE.md itself). Everything fails open: a broken memory system degrades to "Claude Code without memory," never to a broken session.

## Requirements

- **Python ≥ 3.11** (stdlib only — no third-party packages, ever, on the hot path).
- Linux or macOS. **Windows is out of scope for v1** (`fcntl` locking and POSIX paths; the import is guarded, so a future port degrades to lock-free, but v1 is untested there).

## Install

### Path 1 — plugin (preferred)

This repository hosts its own single-plugin marketplace (`.claude-plugin/marketplace.json` with `source: "./"`), so installation is two commands:

```
claude plugin marketplace add https://github.com/RobertCoop/hindbrain
claude plugin install hindbrain@hindbrain
```

(Or interactively: `/plugin` → browse the `hindbrain` marketplace.) The exact flow tracks the live plugin documentation (see the Substrate compatibility checklist, item V1). Once installed, hooks register from the plugin's `hooks/hooks.json` automatically and data lives under `${CLAUDE_PLUGIN_DATA}`.

### Path 2 — settings.json fallback (no plugin infrastructure)

Clone this repo anywhere (example below uses `/home/you/hindbrain`) and register the same hooks in `~/.claude/settings.json` with **absolute paths**:

```json
{
  "hooks": {
    "SessionStart": [
      { "matcher": "startup|resume|clear|compact|fork",
        "hooks": [ { "type": "command", "command": "python3",
          "args": ["/home/you/hindbrain/scripts/session_start.py"], "timeout": 10 } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "python3",
          "args": ["/home/you/hindbrain/scripts/prompt_gate.py"], "timeout": 5 } ] }
    ],
    "PreToolUse": [
      { "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [ { "type": "command", "command": "python3",
          "args": ["/home/you/hindbrain/scripts/pretool_gate.py"], "timeout": 5 } ] },
      { "matcher": "Bash",
        "hooks": [ { "type": "command", "command": "python3",
          "args": ["/home/you/hindbrain/scripts/pretool_gate.py"], "timeout": 5 } ] }
    ],
    "PostToolUse": [
      { "matcher": "Bash|Edit|Write|MultiEdit|Task|WebFetch|WebSearch",
        "hooks": [ { "type": "command", "command": "python3",
          "args": ["/home/you/hindbrain/scripts/observer.py"], "async": true } ] }
    ],
    "PostToolUseFailure": [
      { "matcher": "Bash|Edit|Write|MultiEdit|Task",
        "hooks": [
          { "type": "command", "command": "python3",
            "args": ["/home/you/hindbrain/scripts/failure_gate.py"], "timeout": 5 },
          { "type": "command", "command": "python3",
            "args": ["/home/you/hindbrain/scripts/observer.py"], "async": true }
        ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "python3",
          "args": ["/home/you/hindbrain/scripts/stop_gate.py"], "timeout": 10 } ] }
    ],
    "PreCompact": [
      { "matcher": "auto|manual",
        "hooks": [ { "type": "command", "command": "python3",
          "args": ["/home/you/hindbrain/scripts/precompact_salvage.py"], "async": true } ] }
    ],
    "SessionEnd": [
      { "hooks": [ { "type": "command", "command": "python3",
          "args": ["/home/you/hindbrain/scripts/session_end.py"], "timeout": 2 } ] }
    ]
  }
}
```

Add `hindbrain/bin` to `PATH` (or symlink `bin/mem` into a directory on it) so the agent can run the `mem` CLI. Nothing in the scripts assumes a plugin install — all paths resolve through `lib/paths.py`.

## Data directory

Resolution order: `$HINDBRAIN_DATA` → `$CLAUDE_PLUGIN_DATA` (plugin install) → `~/.claude/hindbrain` (settings install). Layout:

```
<data>/
├── hindbrain.db        # SQLite store (WAL)
├── config.toml         # your config; created from config.default.toml on first run
├── sessions/           # per-(session,agent) state JSON
├── drafts/             # overflow bodies
├── reports/            # consolidator reports (contradictions-, promotions-<date>.md)
├── logs/
│   ├── errors.log
│   └── metrics.jsonl
└── consolidator.lock
```

## Configuration reference (`<data>/config.toml`)

| Key | Meaning |
|---|---|
| `thresholds.tau_hi` | Score at/above which a note is injected in full. **Ships as `9.9` = remind-only rollout (AM-8)**; see below. Steady-state value: `0.50`. |
| `thresholds.tau_lo` | Score at/above which a note is reminded (one-line pointer). Default `0.25`. |
| `thresholds.struggle_factor` | Both taus are multiplied by this while the struggle detector is active (`0.75` = surface more when stuck). |
| `budgets.inject_max_items` | Max injected notes per context (`3`). |
| `budgets.inject_max_chars_each` | Per-note body clip for injection (`1200`). |
| `budgets.inject_max_chars_total` | Total injection char budget per context (`4000`). |
| `budgets.remind_max_items` | Max remind lines per context (`5`). |
| `budgets.output_hard_cap` | Hard cap on any single hook output string (`9500`; platform caps at 10,000). |
| `budgets.edit_scan_bytes` | Max bytes of Edit/Write content scanned by the pretool gate (`4096`). |
| `scoring.rel_k` | Relevance saturation constant: `rel = r/(r+k)`, `r = -bm25`. |
| `scoring.rel_match_floor` | Minimum relevance for a true FTS match or exact scope hit (FTS5 IDF collapses in small stores). |
| `scoring.act_floor` | Activation is a modulator with a floor: score uses `(act_floor + (1-act_floor)*act)` so new notes can still surface. |
| `scoring.act_decay_d` | ACT-R base-level decay exponent (`0.5`). |
| `scoring.act_window` | Number of most recent access events used for activation (`30`). |
| `scoring.auth_full` / `auth_standard` / `auth_pending` / `auth_quarantined` | Authority weight multipliers (`1.00` / `0.85` / `0.65` / `0.45`). |
| `scoring.boost_scope_exact` | Score boost for an exact scope match (`1.5`). |
| `scoring.boost_project` | Score boost for a same-project (or global) note (`1.15`). |
| `scoring.prior_p0` / `prior_p1` / `prior_p2` / `prior_default` | Cold-start synthetic-access weight by originating candidate priority (`3.0` / `3.0` / `2.0` / `1.0`). |
| `scoring.fetch_weight` | Access weight for `mem get` after a remind — the escalation path toward future injection (`3.0`). |
| `scoring.deny_weight` | Access weight logged on a deny event (`3.0`). |
| `nudge.cooldown_turns` | Min turns between non-urgent Stop nudges (`3`). |
| `nudge.max_per_session` | Max non-urgent nudges per session (`3`). |
| `nudge.max_candidates_shown` | Candidates shown per nudge (`3`). |
| `nudge.strict` | When `true`, an urgent nudge blocks the Stop once per session. Default `false`. |
| `struggle.fail_threshold` | Tool failures in the window that flip struggle mode on (`3`). |
| `struggle.churn_threshold` | Edits to the same file in the window that flip struggle mode on (`4`). |
| `struggle.window_events` | Rolling event-window size (`15`). |
| `struggle.reset_success_streak` | Consecutive successes that clear struggle mode (`5`). |
| `decay.env` / `gotcha` / `decision` / `preference` / `fact` / `procedure` | Per-kind default TTL in days; `0` = never expires (`45`/`0`/`0`/`0`/`180`/`0`). Gotchas refresh their clock on use. |
| `hazards.consequential_heads` | Command heads considered consequential for the deny/ask tier. |
| `hazards.consequential_patterns` | Regexes marking consequential command shapes (force-push, `rm -rf`, …). |
| `security.redact_journal` | `true` stores SHA256 word-set hashes instead of raw user prompts in the journal (witness tests then use hashes). Default `false`. |
| `security.llm_passes` | Enables `claude -p` consolidator passes. v1.1 only; default `false`. |
| `general.enabled` | Config-level kill switch (in addition to the `HINDBRAIN_DISABLE=1` env switch). |

### The remind-only default and flipping `tau_hi`

hindbrain ships with `tau_hi = 9.9`, which means **nothing is ever injected** — relevant notes only appear as one-line reminders the agent can fetch with `mem get`. This is deliberate: the inject tier must be earned. Watch `mem stats` (or the consolidator's promotions report): once **acceptance** (fetched-after-remind ÷ reminded, over 30 days) exceeds **0.15**, edit `<data>/config.toml` and set `tau_hi = 0.50`. The consolidator prints this exact suggestion into its report when the threshold is met.

## The `mem` CLI

Runs inside the agent's Bash tool (or your terminal). DB resolution: `--db` → `$HINDBRAIN_DB` → `$CLAUDE_PLUGIN_DATA/hindbrain.db` → `~/.claude/hindbrain/hindbrain.db`. Exit codes: 0 ok, 1 user error, 2 refused (secrets/policy).

```
mem save   --kind K --scope TYPE:VALUE [--tags a,b] [--hazard [--hazard-mode deny|ask]]
           [--channel external] [--from-candidate N] [--prior 0.5..3.0] "BODY"
mem get ID            # full record; logs a 'fetched' access (escalation toward inject)
mem search "TERMS" [--all-projects] [-k 8]
mem list [--kind K] [--project] [--pinned]
mem queue             # open candidates for the current session
mem drop N            # discard candidate N
mem corroborate ID    # +1 corroboration; at >=2 pending -> standard
mem confirm ID        # -> authority full (only with a witnessed user confirmation)
mem supersede ID "BODY"   # new record replaces old (append-only; old kept as lineage)
mem refute ID ["note"]    # mark wrong; excluded from surfacing
mem pin ID | mem unpin ID
mem audit ID          # lineage: source, supersession chain, corroborations, accesses
mem stats             # counts by kind/authority/status; acceptance metrics
mem scout [--json] [--path DIR]   # read-only bootstrap survey of a project (see mem-init)
mem anchor [--path DIR]           # write the .hindbrain workspace anchor (see Multi-repo workspaces)
```

Authority is granted from **hook-witnessed evidence**, never from agent flags: a claim matching a logged user turn gets `full`-track treatment, observer-witnessed fixes get `standard`, plain agent notes start `pending`, and web/tool-derived content is `quarantined` until corroborated across sessions. `--prior` sets only cold-start activation strength, never authority.

### First-run bootstrap: `/hindbrain:mem-init`

Starting hindbrain against an existing project? Run `/hindbrain:mem-init` (optionally with a focus hint, e.g. `/hindbrain:mem-init the deploy pipeline`). It walks the agent through the **mem-init** skill: survey CI/task-runner truth, confession comments, git history, decision records, and doc caveats; filter through the durable/non-obvious/actionable/scoped bar; verify claims before saving; and seed 10–30 well-scoped memories with deliberate kind, scope, and `--prior` strength. It ends with a `mem confirm` shortlist for you — user confirmation is what grants `full` authority and arms the deny tier for any seeded hazards.

Three survey backends, picked automatically by capability: the **`mem-init-scan` plugin workflow** (`/hindbrain:mem-init-scan` — parallel read-only scouts, a judge pass, and a ranked proposal list, with the raw survey kept out of the main context; requires dynamic workflows to be enabled), the **`mem-scout` subagent** the plugin ships (one read-only scout per evidence source), or the fully inline path starting from **`mem scout --json`** — a deterministic, dependency-free survey of confession comments, task-runner/CI recipes, git churn, and environment shape. On every path the survey only *proposes*: saves happen in the main session, and seeded memories start at `pending` authority regardless of backend.

### Multi-repo workspaces and project binding

A memory's `project` is the **workspace**. The durable way to declare one is the anchor file: run `mem anchor` (or touch `.hindbrain`) at the workspace root — resolution walks up from any cwd to the **nearest** directory containing `.hindbrain` and uses it, permanently and explicitly. A parent dir holding several repos is fully supported: anchor the parent once and every nested repo binds to it; `cd`-ing around never forks project identity. Scope path globs workspace-relative (`repoA/src/**`).

Full resolution precedence: `HINDBRAIN_PROJECT` env → nearest `.hindbrain` anchor → freshest session handshake whose workspace contains cwd (written at SessionStart; stale after 7 days) → git root of cwd. The anchor file's presence is what matters; its content is reserved. `mem scout` aggregates git mining across immediate child repos when the workspace root isn't itself a repo.

## Consolidator

`python3 consolidator/consolidate.py` runs the offline maintenance passes (GC, expiry, dedup, contradiction report, reconsolidation check, promotions, CLAUDE.md graduation proposals, metrics rollup). It is a flock singleton and is normally kicked automatically by SessionStart/SessionEnd when the last run is older than 24 h; running it by hand is always safe and idempotent. Reports land in `<data>/reports/`.

## Troubleshooting

- **Something misbehaves mid-session:** set `HINDBRAIN_DISABLE=1` in the environment — every hook checks it first and exits silently. Independent of the platform's own hook toggles. (`general.enabled = false` in config.toml is the persistent equivalent.)
- **Errors:** hooks never break a session; failures are swallowed and logged to `<data>/logs/errors.log`. Attach that file to bug reports. Per-invocation timing lives in `<data>/logs/metrics.jsonl`.
- **`python3` not found by hooks** (pyenv shims, Homebrew paths in GUI-launched sessions): the hooks invoke `python3` via exec form and depend on the platform's PATH. If your environment resolves the wrong (or no) interpreter, a `HINDBRAIN_PYTHON` escape hatch is the planned fix (checklist item O5): point it at an absolute interpreter path and switch the `hooks.json` `command` entries to a small `sh -c '"${HINDBRAIN_PYTHON:-python3}" "$@"' --` shim. Until verified live, editing `hooks.json`/settings to an absolute interpreter path works today.
- **No memories ever injected:** that is the shipped default (remind-only, `tau_hi = 9.9`). See the flip instructions above.

## Uninstall

1. Remove the plugin through the plugin manager, or delete the hindbrain hook blocks from `~/.claude/settings.json` (fallback install).
2. Your memories are just files: the data directory (`$CLAUDE_PLUGIN_DATA` or `~/.claude/hindbrain`) contains the SQLite store, config, logs, and reports. Delete it to purge everything, or keep it — reinstalling picks it right back up.

## Substrate compatibility

The Claude Code platform moves monthly; this build targets the hooks reference as of Aug 2026. The spec's live-verification checklist below is **unverified pending a live test** — run it on day one and monthly, and record results here with the Claude Code version tested.

| V | Verify | Where it lands | Result (CC version / date / verified-adjusted) |
|---|---|---|---|
| V1 | Current plugin install/marketplace flow and manifest fields | §3.2, `plugin.json`, README | *unverified* |
| V2 | `$CLAUDE_ENV_FILE` exact write format and persistence semantics | `scripts/session_start.py` | *unverified* |
| V3 | `UserPromptSubmit` stdin carries the prompt under `prompt` | `scripts/prompt_gate.py` | *unverified* |
| V4 | `PostToolUseFailure` stdin error-field names | `scripts/failure_gate.py` defensive chain order | *unverified* |
| V5 | `Stop` stdin `stop_hook_active` field name/behavior | `scripts/stop_gate.py` guard | *unverified* |
| V6 | Whether `--settings '{"disableAllHooks":true}'` disables *plugin* hooks (v1.1 LLM-pass recursion guard; `HINDBRAIN_DISABLE` covers v1 regardless) | consolidator v1.1 | *unverified* |
| V7 | `PreToolUse`/`PostToolUseFailure` `additionalContext` delivery point (next to tool result) unchanged | gate timing claims | *unverified* |
| V8 | `SessionStart` `source` values (`startup/resume/clear/compact/fork`) | `hooks/hooks.json` matcher, `session_start.py` | *unverified* |
| V9 | `async: true` hooks cannot return context (observer must never rely on output) | `scripts/observer.py` | *unverified* |
| V10 | `${CLAUDE_PLUGIN_DATA}` availability + creation semantics | `lib/paths.py` | *unverified* |
| V11 | Tool hooks fire inside subagents with `agent_id`/`agent_type` | per-context state files | *unverified* |
| V12 | Deny `permissionDecisionReason` is shown to Claude (not user-only) | deny-tier design | *unverified* |
