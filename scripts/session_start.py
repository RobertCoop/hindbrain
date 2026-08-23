#!/usr/bin/env python3
"""SessionStart hook (spec 8.3): env contract, state bootstrap, carryover,
profile injection, consolidator kick."""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import db, log, paths, render, scoring, state

try:
    import fcntl
except ImportError:
    fcntl = None

NAME = "session_start"
SUMMARY = {}

_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
PROFILE_CAP = 1500
PROFILE_BODY_CLIP = 200


def _write_env_contract(session):
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file:
        return
    try:
        with open(env_file, "a", encoding="utf-8") as f:
            f.write(f"HINDBRAIN_DB={paths.db_path()}\n")
            f.write(f"HINDBRAIN_DATA={paths.data_dir()}\n")
            f.write(f"HINDBRAIN_SESSION={session}\n")
    except OSError:
        pass


def _reset_or_load(conn, session, source):
    if source == "compact":
        st = state.load(session, "main")
        if st.get("injected") or st.get("reminded"):
            db.journal(conn, session, "main", "compact_reset",
                       {"injected": st.get("injected") or [],
                        "reminded": st.get("reminded") or []})
        st["injected"] = []
        st["reminded"] = []
        state.save(session, "main", st)
        return st
    if source in ("startup", "clear", "fork"):  # fork = fresh state (F2)
        return state.reset(session, "main")
    st = state.load(session, "main")  # resume (and unknown sources: non-destructive)
    state.save(session, "main", st)
    return st


def _session_ended(owner):
    if owner == "unbound":
        return False  # unbound rows belong to stop_gate's sweep, not carryover
    try:
        with open(state.state_path(owner, "main"), "r", encoding="utf-8") as f:
            return bool(json.load(f).get("ended"))
    except (OSError, ValueError):
        return True  # no state file -> owner is gone; safe to carry


def _carry_candidates(conn, session, project):
    try:
        rows = conn.execute(
            "SELECT * FROM candidate WHERE status='open' AND session_id != ? "
            "ORDER BY ts DESC LIMIT 50", (session,)).fetchall()
    except Exception:
        return []
    carried = []
    for r in rows:
        c = dict(r)
        try:
            payload = json.loads(c.get("payload") or "{}")
        except (ValueError, TypeError):
            payload = {}
        if not isinstance(payload, dict) or payload.get("project") != project:
            continue
        # never steal from a session that is still live (its own Stop gate
        # owns these); unbound rows are excluded likewise
        if not _session_ended(c.get("session_id") or ""):
            continue
        carried.append(c)
    if carried:
        try:
            conn.executemany("UPDATE candidate SET status='carried' WHERE id=?",
                             [(c["id"],) for c in carried])
            conn.commit()
        except Exception:
            pass
    carried.sort(key=lambda c: (_PRIORITY_ORDER.get(c.get("priority"), 4),
                                -(c.get("ts") or 0)))
    return carried


def _top_memories(conn, cfg, project, now):
    try:
        rows = conn.execute(
            "SELECT * FROM memory WHERE status='active' AND project IN (?, '') "
            "ORDER BY access_count DESC, created_at DESC LIMIT 30",
            (project,)).fetchall()
        pinned_rows = conn.execute(
            "SELECT * FROM memory WHERE status='active' AND pinned=1 "
            "AND project IN (?, '')", (project,)).fetchall()
        n = conn.execute(
            "SELECT COUNT(*) FROM memory WHERE status='active' AND project IN (?, '')",
            (project,)).fetchone()[0]
    except Exception:
        return [], 0
    mems = {}
    for r in list(pinned_rows) + list(rows):
        d = dict(r)
        mems.setdefault(d["id"], d)
    window = cfg["scoring"]["act_window"]
    for m in mems.values():
        try:
            events = db.activation_events(conn, m["id"], window)
        except Exception:
            events = []
        m["_act"] = scoring.activation(events, now, cfg)
    ranked = sorted(mems.values(), key=lambda m: m["_act"], reverse=True)
    pinned = [m for m in ranked if m.get("pinned")]
    top = [m for m in ranked if not m.get("pinned")][:5]
    return pinned + top, n


def _profile(conn, cfg, project, carried, now):
    parts = []
    show, n = _top_memories(conn, cfg, project, now)
    if show:
        for m in show:
            body = m.get("body") or ""
            if len(body) > PROFILE_BODY_CLIP:
                m["body"] = body[:PROFILE_BODY_CLIP] + "…"
        parts.append(render.render_inject(show, now))
    if n:
        scopes3 = []
        for m in show:
            sv = m.get("scope_value") or ""
            s = f"{m.get('scope_type', '')}:{sv}" if sv else (m.get("scope_type") or "")
            if s and s not in scopes3:
                scopes3.append(s)
            if len(scopes3) == 3:
                break
        active = f" (most active: {', '.join(scopes3)})" if scopes3 else ""
        parts.append(f"Local memory: {n} notes for this project{active}. "
                     "`mem search <terms>` available.")
    if carried:
        lines = ["Carried-over unsaved candidate drafts from previous sessions:"]
        for c in carried[:2]:
            draft = c.get("draft_cmd") or ""
            if not draft:
                try:
                    draft = json.loads(c.get("payload") or "{}").get("text") or ""
                except (ValueError, TypeError, AttributeError):
                    draft = ""
            lines.append(f"  {render.clip(str(draft), 200)}")
        parts.append("\n".join(lines))
    return render.clip("\n".join(parts), PROFILE_CAP), [m["id"] for m in show]


def _maybe_kick_consolidator(conn):
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='last_consolidation'").fetchone()
    except Exception:
        row = None
    last = 0
    if row:
        try:
            last = int(float(row[0]))
        except (TypeError, ValueError):
            last = 0
    if time.time() - last < 24 * 3600:
        return False
    script = os.path.join(paths.plugin_root(), "consolidator", "consolidate.py")
    if not os.path.exists(script):
        return False
    if fcntl is not None:
        # skip when the consolidator singleton already holds the lock
        try:
            fh = open(os.path.join(paths.data_dir(), "consolidator.lock"), "a+")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                fh.close()
                return False
            fh.close()
        except OSError:
            pass
    subprocess.Popen([sys.executable, script], start_new_session=True,
                     env={**os.environ, "HINDBRAIN_DISABLE": "1"},
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    return True


def main(evt, cfg):
    session = evt.get("session_id") or "unknown"
    cwd = evt.get("cwd") or os.getcwd()
    project = paths.gitroot(cwd)
    source = evt.get("source") or "startup"
    now = int(time.time())

    conn = db.connect()
    db.ensure_schema(conn)
    _write_env_contract(session)
    _reset_or_load(conn, session, source)
    carried = _carry_candidates(conn, session, project)
    text, shown_ids = _profile(conn, cfg, project, carried, now)
    if shown_ids:
        def mark(s):
            for i in shown_ids:
                if i not in s["injected"]:
                    s["injected"].append(i)
        state.update(session, "main", mark)
    kicked = _maybe_kick_consolidator(conn)
    SUMMARY.update({"session": session, "agent": "main", "source": source,
                    "profile_chars": len(text), "carried": len(carried),
                    "consolidator_kicked": kicked})
    if not text:
        return None
    return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                   "additionalContext": text}}


if __name__ == "__main__":
    t0 = time.monotonic()
    try:
        if os.environ.get("HINDBRAIN_DISABLE") == "1":
            sys.exit(0)
        evt = json.load(sys.stdin)
        cfg = paths.load_config()
        if not cfg["general"]["enabled"]:
            sys.exit(0)
        out = main(evt, cfg)
        log.metric({"hook": NAME,
                    "dur_ms": int(1000 * (time.monotonic() - t0)), **SUMMARY})
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
