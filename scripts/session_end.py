#!/usr/bin/env python3
"""SessionEnd (spec 8.11, <=2s budget): mark state ended, detach the
consolidator with HINDBRAIN_DISABLE=1. No other work."""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import log, paths, state

NAME = "session_end"


def main(evt):
    session = evt.get("session_id") or "unknown"

    def _upd(s):
        s["ended"] = True
    state.update(session, "main", _upd)

    script = os.path.join(paths.plugin_root(), "consolidator", "consolidate.py")
    spawned = 0
    if os.path.exists(script):
        try:
            subprocess.Popen(
                [sys.executable, script],
                start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "HINDBRAIN_DISABLE": "1"})
            spawned = 1
        except OSError:
            pass
    return None, {"session": session, "agent": "main", "consolidator": spawned}


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
