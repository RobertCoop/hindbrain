#!/usr/bin/env python3
"""PreToolUse hook (spec 8.5): edit-family scope+FTS injection; Bash deny/ask
tier before injection. Never emits permissionDecision "allow"."""
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import db, log, paths, querybuild, render, scopes, scoring, signatures, state

NAME = "pretool_gate"
SUMMARY = {}

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _exact_scoped(conn, scope_type, project, ctx):
    # scope_values repeat across many rows; match each distinct value once,
    # then fetch only the rows that hit — avoids converting ~1k rows per call
    probe = {"scope_type": scope_type, "project": ""}
    try:
        svs = [r[0] for r in conn.execute(
            "SELECT DISTINCT scope_value FROM memory WHERE scope_type = ? "
            "AND status = 'active' AND project IN (?, '')",
            (scope_type, project))]
        matched = [sv for sv in svs if sv and scopes.match(
            {**probe, "scope_value": sv}, ctx) == "exact"]
        if not matched:
            return []
        rows = conn.execute(
            "SELECT * FROM memory WHERE scope_type = ? AND status = 'active' "
            "AND project IN (?, '') AND scope_value IN "
            f"({','.join('?' * len(matched))})",
            (scope_type, project, *matched)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        d = dict(r)
        d["bm25"] = 0.0
        d["_scope_exact"] = True  # verified above; survives ctx retargeting
        out.append(d)
    return out


def _file_args(subs, cwd, project, cap=8):
    # positional args of a bash command that are real files under the
    # workspace — what makes `sed`/`cat`/`python x.py` reads visible to
    # path-scoped memories
    found, checked = [], 0
    for toks in subs:
        for t in toks[1:]:
            if t.startswith("-") or len(t) < 3 or checked >= cap:
                continue
            if not ("/" in t or "." in os.path.basename(t)):
                continue
            checked += 1
            p = t if os.path.isabs(t) else os.path.join(cwd, t)
            p = os.path.abspath(p)
            if (p == project or p.startswith(project + os.sep)) and os.path.isfile(p):
                found.append(p)
    return found


def _is_consequential(cmd, subs, cfg):
    hz = cfg["hazards"]
    heads = set(hz.get("consequential_heads") or [])
    for s in subs:
        h = signatures.head(s)
        if h and h[0] in heads:
            return True
    for p in hz.get("consequential_patterns") or []:
        try:
            if re.search(p, cmd, re.I):
                return True
        except re.error:
            continue
    return False


def _matched_head(mem, heads):
    for e in (mem.get("scope_value") or "").split("|"):
        e = e.strip()
        if e in heads:
            return e
    return next(iter(heads), "")


def _record_denied(session, agent, mem_id, sig):
    def fn(s):
        lst = s.setdefault("denied", {}).setdefault(mem_id, [])
        if sig not in lst:
            lst.append(sig)
    state.update(session, agent, fn)


def _decision(decision, reason, cfg):
    SUMMARY["deny"] = SUMMARY.get("deny", 0) + 1
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason":
            render.clip(reason, cfg["budgets"]["output_hard_cap"])}}


def _emit(conn, cfg, session, agent, inject, remind, q, now):
    parts = []
    if inject:
        parts.append(render.render_inject(inject, now))
    if remind:
        parts.append(render.render_remind(remind, now))
    for h in inject:
        db.log_access(conn, h["id"], session, agent, "injected", 1.0, q)
    for h in remind:
        db.log_access(conn, h["id"], session, agent, "reminded", 1.0, q)
    if inject or remind:
        ii = [h["id"] for h in inject]
        rr = [h["id"] for h in remind]

        def mark(s):
            for i in ii:
                if i not in s["injected"]:
                    s["injected"].append(i)
            for i in rr:
                if i not in s["reminded"]:
                    s["reminded"].append(i)
        state.update(session, agent, mark)
    SUMMARY.update({"inject": len(inject), "remind": len(remind)})
    if not parts:
        return None
    text = render.clip("\n\n".join(parts), cfg["budgets"]["output_hard_cap"])
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "additionalContext": text}}


def _edit_branch(conn, cfg, st, session, agent, cwd, project, tool, ti, now):
    fp = str(ti.get("file_path") or ti.get("notebook_path") or "")
    content = ti.get("new_string") or ti.get("content") or ""
    if not content and isinstance(ti.get("edits"), list):
        content = " ".join(str(e.get("new_string") or "")
                           for e in ti["edits"] if isinstance(e, dict))
    content = str(content)[:cfg["budgets"]["edit_scan_bytes"]]
    ctx = scoring.Ctx(session=session, agent=agent, cwd=cwd, project=project,
                      command_adjacent=False, file_path=fp, tool_name=tool)
    hits = _exact_scoped(conn, "path", project, ctx)
    q = querybuild.fts_query(os.path.basename(fp) + " " + content)
    if q:
        hits = hits + db.search(conn, q, project, k=12)
    # §5 capability table: quarantined excluded from pretool_gate entirely
    hits = [h for h in hits if h.get("authority") != "quarantined"]
    SUMMARY["hits"] = len(hits)
    taus = scoring.effective_taus(cfg, st, conn)
    inject, remind = scoring.gate(hits, st, ctx, cfg, conn, taus, now)
    return _emit(conn, cfg, session, agent, inject, remind, q, now)


def _bash_branch(conn, cfg, st, session, agent, cwd, project, ti, now):
    cmd = str(ti.get("command") or "")
    subs = signatures.subcommands(cmd)
    heads = {signatures.head_str(s) for s in subs}
    ctx = scoring.Ctx(session=session, agent=agent, cwd=cwd, project=project,
                      command_adjacent=True, command=cmd, tool_name="Bash")
    scoped = _exact_scoped(conn, "command", project, ctx)
    consequential = _is_consequential(cmd, subs, cfg)
    denied_map = st.get("denied") or {}
    deny_w = cfg["scoring"]["deny_weight"]

    # ---- deny/ask tier (before injection) ----
    # identical retry after ANY prior deny (hazard or escalation path) -> ask
    for m in scoped:
        if m.get("authority") != "full":
            continue
        sig = "Bash:" + _matched_head(m, heads)
        if sig in (denied_map.get(m["id"]) or []):
            db.log_access(conn, m["id"], session, agent, "denied", deny_w, cmd[:200])
            reason = ("Command rerun unchanged after a prior block. "
                      + render.render_ask(m))
            return _decision("ask", reason, cfg)
    for m in scoped:
        if not m.get("hazard") or m.get("authority") != "full":
            continue
        sig = "Bash:" + _matched_head(m, heads)
        if consequential:
            if m.get("hazard_mode") == "ask":
                db.log_access(conn, m["id"], session, agent, "denied", deny_w,
                              cmd[:200])
                return _decision("ask", render.render_ask(m), cfg)
            _record_denied(session, agent, m["id"], sig)
            db.log_access(conn, m["id"], session, agent, "denied", deny_w, cmd[:200])
            return _decision("deny", render.render_deny(m, now), cfg)

    # ---- reminded-but-unfetched escalation ----
    if consequential:
        reminded = set(st.get("reminded") or [])
        fetched = set(st.get("fetched") or [])
        for m in scoped:
            mid = m["id"]
            if (mid in reminded and mid not in fetched
                    and m.get("authority") == "full" and mid not in denied_map):
                sig = "Bash:" + _matched_head(m, heads)
                _record_denied(session, agent, mid, sig)
                db.log_access(conn, mid, session, agent, "denied", deny_w, cmd[:200])
                return _decision("deny", render.render_deny(m, now), cfg)

    # ---- injection tier ----
    # file args make `sed`/`cat`/`python x.py` reads visible to path scopes;
    # path hits never join the deny tier (deny is command-scope-only in v1)
    files = _file_args(subs, cwd, project)
    path_hits = []
    for fp in files:
        fctx = scoring.Ctx(session=session, agent=agent, cwd=cwd,
                           project=project, file_path=fp, tool_name="Bash")
        path_hits.extend(_exact_scoped(conn, "path", project, fctx))
    if files:
        ctx.file_path = files[0]
    toks = []
    for s in subs:
        toks.extend(signatures.head(s))
        toks.extend(t.lstrip("-") for t in s if t.startswith("-") and len(t) > 1)
    toks.extend(os.path.basename(f) for f in files)
    q = querybuild.fts_query(" ".join(toks))
    hits = scoped + path_hits + (db.search(conn, q, project, k=12) if q else [])
    SUMMARY["hits"] = len(hits)
    taus = scoring.effective_taus(cfg, st, conn)
    inject, remind = scoring.gate(hits, st, ctx, cfg, conn, taus, now)
    return _emit(conn, cfg, session, agent, inject, remind, q, now)


def main(evt, cfg):
    session = evt.get("session_id") or "unknown"
    agent = evt.get("agent_id") or "main"
    tool = evt.get("tool_name") or ""
    ti = evt.get("tool_input")
    if not isinstance(ti, dict):
        ti = {}
    cwd = evt.get("cwd") or os.getcwd()
    project = paths.resolve_project(cwd)
    now = int(time.time())
    SUMMARY.update({"session": session, "agent": agent, "tool": tool,
                    "hits": 0, "inject": 0, "remind": 0, "deny": 0})

    if tool == "Read" and not cfg["general"].get("read_gate", True):
        return None

    conn = db.connect()
    db.ensure_schema(conn)
    st = state.load(session, agent)
    if tool in EDIT_TOOLS or tool == "Read":
        # Read reuses the edit branch: file_path + no content, and
        # command_adjacent=False, so pending-authority notes may remind here
        return _edit_branch(conn, cfg, st, session, agent, cwd, project, tool,
                            ti, now)
    if tool == "Bash":
        return _bash_branch(conn, cfg, st, session, agent, cwd, project, ti, now)
    return None


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
