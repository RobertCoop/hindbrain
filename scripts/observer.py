#!/usr/bin/env python3
"""Async observer (spec 8.7 + AM-7): journals failures/taint/Task summaries/edit
churn, detects fail->fix pairs into learned_fix candidates, maintains struggle
window. Never emits output (async hooks cannot return context, V9)."""
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import db, ids, log, paths, querybuild, signatures, state, witness
import drafting

NAME = "observer"

# Trivial failures never journaled (spec 8.7 write-time ignore list)
TRIVIAL = re.compile(
    r"exit code 130|status 130|SIGINT|KeyboardInterrupt|timed? ?out|TimeoutError|"
    r"rate.?limit|too many requests|\b429\b|network is unreachable|ENETUNREACH|"
    r"command not found", re.I)

DISCOVERY = re.compile(
    r"\b(turns? out|discovered|root cause|the fix (was|is)|gotcha|"
    r"note for (the )?future|works only|must (use|set|run)|requires? setting)\b",
    re.I)

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _failure_text(evt):
    for key in ("tool_response", "error", "stderr"):
        v = evt.get(key, "")
        s = v if isinstance(v, str) else ("" if v is None else str(v))
        if s.strip():
            return s
    try:
        return json.dumps(evt, default=str)[:4000]
    except (TypeError, ValueError):
        return ""


def _record_struggle(session, agent, cfg, kind, file=None):
    sc = cfg["struggle"]

    def _upd(st):
        s = st.get("struggle") or {}
        ev = {"t": kind}
        if file:
            ev["file"] = file
        events = ((s.get("events") or []) + [ev])[-int(sc["window_events"]):]
        fails = sum(1 for e in events if e.get("t") == "fail")
        churn = {}
        for e in events:
            if e.get("t") == "edit" and e.get("file"):
                churn[e["file"]] = churn.get(e["file"], 0) + 1
        streak = 0
        for e in reversed(events):
            if e.get("t") != "ok":
                break
            streak += 1
        active = bool(s.get("active"))
        if fails >= sc["fail_threshold"] or (
                churn and max(churn.values()) >= sc["churn_threshold"]):
            active = True
        if streak >= sc["reset_success_streak"]:
            active = False
        st["struggle"] = {"fails": fails, "churn": churn, "streak": streak,
                         "active": active, "events": events}
    state.update(session, agent, _upd)


def _near_match(conn, project, text):
    # dedup-before-enqueue: fts near-match against existing memories
    q = querybuild.fts_query(text)
    if not q:
        return None
    a = witness.content_words(text)
    if not a:
        return None
    for h in db.search(conn, q, project, k=3):
        b = witness.content_words(
            f"{h.get('title') or ''} {h.get('body') or ''}")
        if b and len(a & b) / len(a | b) >= 0.5:
            return h["id"]
    return None


def _enqueue(conn, session, agent, priority, signal, payload, draft_cmd,
             project, dedup_text):
    payload = dict(payload)
    payload.setdefault("project", project)  # AM-2 unbound sweep scoping
    near = _near_match(conn, project, dedup_text)
    cid = ids.ulid()
    if draft_cmd and draft_cmd.startswith("mem save"):
        # links §9.3 step 4: candidate -> saved, prior from priority
        draft_cmd = f"{draft_cmd} --from-candidate {cid}"
    try:
        conn.execute(
            "INSERT INTO candidate(id, session_id, agent_id, ts, priority, "
            "signal, payload, draft_cmd, near_match) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, session, agent, int(time.time()), priority, signal,
             json.dumps(payload), draft_cmd, near))
        conn.commit()
        return True
    except sqlite3.OperationalError:
        return False


def _task_summary(resp):
    # AM-7: extract text fields when the response parses as JSON; fallback str
    parsed = None
    if isinstance(resp, str):
        try:
            parsed = json.loads(resp)
        except ValueError:
            parsed = None
    elif isinstance(resp, (dict, list)):
        parsed = resp
    texts = []

    def walk(o, depth=0):
        if depth > 4 or len(texts) >= 20:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str) and k in (
                        "text", "content", "summary", "result", "message"):
                    texts.append(v)
                else:
                    walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o:
                walk(v, depth + 1)
        elif isinstance(o, str):
            texts.append(o)

    if parsed is not None:
        walk(parsed)
    if texts:
        return " ".join(texts)[:2000]
    return str(resp)[:2000]


def _snippet_at(text, pos, span=300):
    start = max(text.rfind("\n", 0, pos), text.rfind(". ", 0, pos)) + 1
    return text[start:start + span].strip()


def _handle_failure(conn, evt, cfg, session, agent):
    ft = _failure_text(evt)
    if TRIVIAL.search(ft):
        return
    tool = evt.get("tool_name") or ""
    ti = evt.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    cmd = str(ti.get("command") or "")
    subs = signatures.subcommands(cmd)
    db.journal(conn, session, agent, "tool_fail", {
        "tool": tool,
        "sig": signatures.failure_signature(tool, ti, ft),
        "cmd_head": signatures.head_str(subs[0]) if subs else "",
        "cmd": cmd[:500],
        "ts": int(time.time())})
    _record_struggle(session, agent, cfg, "fail")


def _learned_fix(conn, cfg, session, agent, project, ti, cmd, summary):
    subs = signatures.subcommands(cmd)
    if not subs:
        return
    ok_sig = signatures.failure_signature("Bash", ti, "")
    head = ok_sig.split(":", 2)[1]
    cutoff = int(time.time()) - 1800
    try:
        rows = conn.execute(
            "SELECT data FROM journal WHERE session_id=? AND event='tool_fail' "
            "AND ts>=? ORDER BY ts DESC LIMIT 50", (session, cutoff)).fetchall()
    except sqlite3.OperationalError:
        return
    fail = None
    for r in rows:
        try:
            d = json.loads(r["data"])
        except (ValueError, TypeError):
            continue
        fsig = d.get("sig") or ""
        # Task/Edit failures never seed learned_fix (AM-7): Bash sigs only
        if fsig.startswith("Bash:") and signatures.similar(fsig, ok_sig):
            fail = d
            break
    if not fail or not fail.get("cmd") or fail["cmd"] == cmd:
        return
    # one learned_fix per command head per session
    try:
        open_rows = conn.execute(
            "SELECT payload FROM candidate WHERE session_id=? AND "
            "signal='learned_fix' AND status='open'", (session,)).fetchall()
    except sqlite3.OperationalError:
        open_rows = []
    for r in open_rows:
        try:
            if json.loads(r["payload"]).get("head") == head:
                return
        except (ValueError, TypeError, AttributeError):
            continue
    fail_cmd = fail["cmd"]
    fail_toks = set(fail_cmd.split())
    delta = " ".join(t for t in cmd.split() if t not in fail_toks)[:200]
    draft = drafting.draft_gotcha(fail_cmd, cmd)
    if _enqueue(conn, session, agent, "P2", "learned_fix",
                {"fail_cmd": fail_cmd[:500], "ok_cmd": cmd[:500],
                 "delta": delta, "head": head, "observer": True},
                draft.get("cmd"), project,
                draft.get("body") or f"{fail_cmd} {cmd}"):
        summary["candidates"] += 1


def main(evt):
    cfg = paths.load_config()
    session = evt.get("session_id") or "unknown"
    agent = evt.get("agent_id") or "main"
    cwd = evt.get("cwd") or os.getcwd()
    project = paths.gitroot(cwd)
    hook = evt.get("hook_event_name") or ""
    tool = evt.get("tool_name") or ""
    ti = evt.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    conn = db.connect()
    db.ensure_schema(conn)
    summary = {"event": hook, "tool": tool, "session": session, "agent": agent,
               "candidates": 0}

    if hook == "PostToolUseFailure":
        _handle_failure(conn, evt, cfg, session, agent)
        return None, summary

    if tool in ("WebFetch", "WebSearch") or tool.startswith("mcp__"):
        turn = int(state.load(session, agent).get("turn") or 0)
        db.journal(conn, session, agent, "taint", {"turn": turn})

        def _taint(s):
            tt = s.get("tainted_turns") or []
            if turn not in tt:
                tt.append(turn)
            s["tainted_turns"] = tt
        state.update(session, agent, _taint)
        return None, summary

    if tool == "Task":
        text = _task_summary(evt.get("tool_response"))
        db.journal(conn, session, agent, "tool_ok",
                   {"tool": "Task", "summary": text})
        m = DISCOVERY.search(text or "")
        if m:
            snippet = _snippet_at(text, m.start())
            draft = drafting.draft_remember(snippet) if snippet else {}
            if snippet and _enqueue(
                    conn, session, agent, "P3", "discovery",
                    {"text": snippet, "observer": True},
                    draft.get("cmd"), project, snippet):
                summary["candidates"] += 1
        return None, summary

    if tool == "Bash":
        cmd = str(ti.get("command") or "")
        subs = signatures.subcommands(cmd)
        db.journal(conn, session, agent, "tool_ok", {
            "tool": "Bash",
            "cmd_head": signatures.head_str(subs[0]) if subs else ""})
        _record_struggle(session, agent, cfg, "ok")
        _learned_fix(conn, cfg, session, agent, project, ti, cmd, summary)
        return None, summary

    if tool in EDIT_TOOLS:
        fp = str(ti.get("file_path") or "")
        db.journal(conn, session, agent, "tool_ok", {"file": fp})
        _record_struggle(session, agent, cfg, "edit", file=fp)
        return None, summary

    return None, summary


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
