#!/usr/bin/env python3
"""PostToolUseFailure sync read gate (spec 8.6): surface saved notes matching
the failure text. additionalContext only — never decisions, never exit 2."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import db, log, paths, querybuild, render, scoring, state

NAME = "failure_gate"


def _failure_text(evt):
    # defensive chain per spec 8.6 / A15
    for key in ("tool_response", "error", "stderr"):
        v = evt.get(key, "")
        s = v if isinstance(v, str) else ("" if v is None else str(v))
        if s.strip():
            return s
    try:
        return json.dumps(evt, default=str)[:4000]
    except (TypeError, ValueError):
        return ""


def main(evt):
    cfg = paths.load_config()
    session = evt.get("session_id") or "unknown"
    agent = evt.get("agent_id") or "main"
    cwd = evt.get("cwd") or os.getcwd()
    project = paths.resolve_project(cwd)
    tool_name = evt.get("tool_name") or ""
    ti = evt.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}

    q = querybuild.error_query(_failure_text(evt))
    conn = db.connect()
    db.ensure_schema(conn)
    hits = db.search(conn, q, project) if q else []
    # §5 capability table: quarantined excluded from failure_gate entirely
    hits = [h for h in hits if h.get("authority") != "quarantined"]

    st = state.load(session, agent)
    ctx = scoring.Ctx(session=session, agent=agent, cwd=cwd, project=project,
                      command_adjacent=(tool_name == "Bash"),
                      command=str(ti.get("command") or ""), tool_name=tool_name)
    inject, remind = scoring.gate(hits, st, ctx, cfg, conn,
                                  scoring.struggle_adjusted(cfg, st))

    for h in inject:
        db.log_access(conn, h["id"], session, agent, "injected", query=q)
    for h in remind:
        db.log_access(conn, h["id"], session, agent, "reminded", query=q)
    if inject or remind:
        ii = [h["id"] for h in inject]
        ri = [h["id"] for h in remind]

        def _upd(s):
            s["injected"] = list(dict.fromkeys((s.get("injected") or []) + ii))
            s["reminded"] = list(dict.fromkeys((s.get("reminded") or []) + ri))
        state.update(session, agent, _upd)

    summary = {"hits": len(hits), "inject": len(inject), "remind": len(remind),
               "deny": 0, "session": session, "agent": agent}
    now = int(time.time())
    parts = []
    if inject:
        parts.append(render.render_inject(inject, now))
    if remind:
        parts.append(render.render_remind(remind, now))
    if not parts:
        return None, summary
    text = render.clip("\n\n".join(parts), cfg["budgets"]["output_hard_cap"])
    return {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure",
                                   "additionalContext": text}}, summary


if __name__ == "__main__":
    t0 = time.monotonic()
    try:
        if os.environ.get("HINDBRAIN_DISABLE") == "1":
            sys.exit(0)
        evt = json.load(sys.stdin)
        cfg = paths.load_config()
        if not cfg["general"]["enabled"]:
            sys.exit(0)
        out, summary = main(evt)
        log.metric({"hook": NAME, "dur_ms": int(1000 * (time.monotonic() - t0)),
                    **summary})
        if out:
            print(json.dumps(out))
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        try:
            log.err(e, NAME)
        finally:
            sys.exit(0)
