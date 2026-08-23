#!/usr/bin/env python3
"""Stop write gate (spec 8.8 + AM-2): nudge the agent with pre-drafted saves
for open candidates. Urgency bypasses cooldown/cap; strict mode (config, off
by default) may block once per session on urgent candidates only."""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import db, log, paths, render, state

NAME = "stop_gate"

_PRIO = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _unbound_sweep(conn, session, project, now):
    # AM-2: only rows for this project; claim so concurrent sessions skip
    out = []
    try:
        rows = conn.execute(
            "SELECT * FROM candidate WHERE status='open' AND "
            "session_id='unbound' AND ts > ?", (now - 7200,)).fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        c = dict(r)
        try:
            payload = json.loads(c.get("payload") or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or payload.get("project") != project:
            continue
        claimed = payload.get("claimed_by")
        if claimed and claimed != session:
            continue
        if not claimed:
            payload["claimed_by"] = session
            try:
                conn.execute("UPDATE candidate SET payload=? WHERE id=?",
                             (json.dumps(payload), c["id"]))
                conn.commit()
            except sqlite3.OperationalError:
                continue
            c["payload"] = json.dumps(payload)
        out.append(c)
    return out


def main(evt):
    if evt.get("stop_hook_active"):  # platform loop guard
        return None, {"skipped": "stop_hook_active"}
    cfg = paths.load_config()
    session = evt.get("session_id") or "unknown"
    cwd = evt.get("cwd") or os.getcwd()
    project = paths.gitroot(cwd)
    now = int(time.time())
    conn = db.connect()
    db.ensure_schema(conn)
    st = state.load(session, "main")

    # all agents' candidates surface at the main-thread Stop (§12.1; a
    # subagent's learnings must not die with its context)
    rows = conn.execute(
        "SELECT * FROM candidate WHERE status='open' AND session_id=?",
        (session,)).fetchall()
    cands = [dict(r) for r in rows]
    if session != "unbound":
        cands.extend(_unbound_sweep(conn, session, project, now))

    summary = {"cands": len(cands), "session": session, "agent": "main"}
    if not cands:
        return None, summary

    ncfg = cfg["nudge"]
    urgent = any(c.get("priority") in ("P0", "P1") for c in cands)
    turn = int(st.get("turn") or 0)
    if not urgent:
        if (turn - int(st.get("last_nudge_turn", -10))) < ncfg["cooldown_turns"]:
            return None, summary
        if int(st.get("nudges") or 0) >= ncfg["max_per_session"]:
            return None, summary

    shown = sorted(cands, key=lambda c: (_PRIO.get(c.get("priority"), 9),
                                         c.get("ts") or 0))
    shown = shown[:ncfg["max_candidates_shown"]]
    mems = {}
    for c in shown:
        nm = c.get("near_match")
        if nm and nm not in mems:
            m = db.get_memory(conn, nm)
            if m:
                mems[nm] = m
    text = render.clip(render.render_nudge(shown, mems),
                       cfg["budgets"]["output_hard_cap"])
    block = bool(ncfg.get("strict")) and urgent and not st.get("strict_blocked")

    def _upd(s):
        s["last_nudge_turn"] = turn
        s["nudges"] = int(s.get("nudges") or 0) + 1
        if block:
            s["strict_blocked"] = True  # strict blocks at most once per session
    state.update(session, "main", _upd)
    db.journal(conn, session, "main", "nudge",
               {"shown": [c["id"] for c in shown], "urgent": urgent})
    summary.update({"nudge": 1, "shown": len(shown)})
    if block:
        return {"decision": "block", "reason": text}, summary
    return {"hookSpecificOutput": {"hookEventName": "Stop",
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
