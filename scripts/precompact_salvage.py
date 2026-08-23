#!/usr/bin/env python3
"""PreCompact salvage (spec 8.10): journal open candidate ids + trigger.
Nothing else — PreCompact carries no additionalContext, never blocks."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import db, log, paths

NAME = "precompact_salvage"


def main(evt):
    session = evt.get("session_id") or "unknown"
    agent = evt.get("agent_id") or "main"
    conn = db.connect()
    db.ensure_schema(conn)
    rows = conn.execute(
        "SELECT id FROM candidate WHERE status='open' AND session_id=?",
        (session,)).fetchall()
    open_ids = [r["id"] for r in rows]
    db.journal(conn, session, agent, "precompact",
               {"open_candidates": open_ids,
                "trigger": str(evt.get("trigger") or "")})
    return None, {"open": len(open_ids), "session": session, "agent": agent}


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
