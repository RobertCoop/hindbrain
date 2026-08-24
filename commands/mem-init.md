---
description: Survey this project and seed the hindbrain memory store (first-run bootstrap)
---
Initialize the hindbrain memory store for this project by following the
**mem-init** skill (load it first — it defines the survey passes, the quality
bar, and how to set kind, scope, and strength).

Focus hint from the user (may be empty; if set, weight the survey toward it):
$ARGUMENTS

Pick the survey path in this order (the skill details each):

1. **Workflow available** → run the plugin's `mem-init-scan` workflow
   (`/hindbrain:mem-init-scan`), passing the focus hint as its args. It fans
   out read-only scouts and returns a ranked proposal list; you then review,
   save the keepers, and report.
2. **No workflows, but subagents available** → spawn `mem-scout` agents (one
   per evidence source per the skill), merge their candidate lists yourself.
3. **Neither** → run `mem scout --json` for the deterministic survey, then
   follow the skill's inline passes.

Constraints, non-negotiable on every path: the survey never saves — you save,
in the main session, after your own review; verify claims before saving where
cheap; `mem search` before every save; stay near the 10–30 memory target;
finish with the report and the `mem confirm` list for me.
