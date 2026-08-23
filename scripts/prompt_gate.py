#!/usr/bin/env python3
"""UserPromptSubmit hook (spec 8.4 + AM-1): turn bookkeeping, witness journal,
P0/correction detectors, FTS retrieval through the tier gate."""
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import db, ids, log, paths, querybuild, render, scoring, state, witness

import drafting

NAME = "prompt_gate"
SUMMARY = {}

PROMPT_CAP = 20 * 1024

P0_RE = re.compile(r"\b(remember|for future reference|from now on|going forward)\b",
                   re.I)
CORRECTION_RE = re.compile(
    r"^\s*(no[,.\s]|nope\b|not that\b|wrong\b|stop\b|don'?t\b|do not\b)"
    r"|\bi told you\b|^\s*actually\b", re.I)
# AM-1 second clause: always|never within 4 tokens of a clause-start directive verb
_DIRECTIVE = {"use", "prefer", "do", "don't", "dont", "avoid", "ask", "write", "run"}
_LEAD_SKIP = {"you", "we", "please", "should", "must", "and", "also", "just",
              "always", "never"}
_SENT_SPLIT = re.compile(r"[.!?;\n]+")
_TOK = re.compile(r"[a-z']+")


def _always_never_clause(prompt):
    for clause in _SENT_SPLIT.split(prompt):
        toks = _TOK.findall(clause.lower())
        an = [i for i, t in enumerate(toks) if t in ("always", "never")]
        if not an:
            continue
        j = 0
        while j < len(toks) and toks[j] in _LEAD_SKIP:
            j += 1
        if j < len(toks) and toks[j] in _DIRECTIVE and any(abs(i - j) <= 4 for i in an):
            return clause.strip()
    return None


def _p0_sentence(prompt):
    if P0_RE.search(prompt):
        for s in _SENT_SPLIT.split(prompt):
            if P0_RE.search(s):
                return s.strip()[:300]
        return prompt.strip()[:300]
    return _always_never_clause(prompt)


def _hash_words(words):
    import hashlib  # lazy: only redact_journal mode pays for _hashlib
    return [hashlib.sha256(w.encode("utf-8")).hexdigest() for w in sorted(words)]


def _journal_event(conn, cfg, session, project, turn, event, text):
    if cfg["security"]["redact_journal"]:
        words = witness.content_words(text)
        # AM-5: content_words drops stopwords ("yes"), but confirm_witnessed's
        # hashed branch checks cue-word hashes — keep any cue token from the
        # raw text so both journal modes behave identically.
        words |= witness.CUES & set(witness._tokens(text))
        data = {"hash_words": _hash_words(words),
                "project": project, "turn": turn}
    else:
        data = {"text": text, "project": project, "turn": turn}
    db.journal(conn, session, "main", event, data)


def _repeat_correction(conn, project, text, now):
    # 30-day repeat against prior journaled correction detections -> P1
    words = witness.content_words(text)
    if not words:
        return False
    hashed = None
    try:
        rows = conn.execute(
            "SELECT data FROM journal WHERE event='correction' AND ts >= ? "
            "ORDER BY ts DESC LIMIT 200", (now - 30 * 86400,)).fetchall()
    except Exception:
        return False
    for r in rows:
        try:
            d = json.loads(r["data"])
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict) or d.get("project") != project:
            continue
        if isinstance(d.get("hash_words"), list):
            if hashed is None:
                hashed = set(_hash_words(words))
            u, b = set(d["hash_words"]), hashed
        else:
            u, b = witness.content_words(d.get("text") or ""), words
        if u and b and len(b & u) / len(b | u) >= 0.5:
            return True
    return False


def _near_match(conn, project, body):
    q = querybuild.fts_query(body)
    if not q:
        return None
    bw = witness.content_words(body)
    for h in db.search(conn, q, project, k=3):
        hw = witness.content_words((h.get("title") or "") + " " + (h.get("body") or ""))
        if bw and hw and len(bw & hw) / len(bw | hw) >= 0.6:
            return h["id"]
    return None


def _enqueue(conn, session, agent, priority, signal, payload, draft_cmd,
             near_match=None):
    cid = ids.ulid()
    if draft_cmd and draft_cmd.startswith("mem save"):
        # links §9.3 step 4: candidate -> saved, prior from priority
        draft_cmd = f"{draft_cmd} --from-candidate {cid}"
    try:
        conn.execute(
            "INSERT INTO candidate(id, session_id, agent_id, ts, priority, signal, "
            "payload, draft_cmd, near_match) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, session, agent, int(time.time()), priority, signal,
             json.dumps(payload), draft_cmd, near_match))
        conn.commit()
    except sqlite3.OperationalError:
        pass


def _refresh_struggle(st, cfg):
    sg = st.get("struggle") or {}
    scfg = cfg["struggle"]
    events = [e for e in (sg.get("events") or []) if isinstance(e, dict)]
    events = events[-scfg["window_events"]:]
    sg["events"] = events
    fails = sum(1 for e in events if e.get("t") == "fail")
    churn = {}
    for e in events:
        if e.get("t") == "edit" and e.get("file"):
            churn[e["file"]] = churn.get(e["file"], 0) + 1
    sg["fails"] = fails
    sg["churn"] = churn
    if sg.get("streak", 0) >= scfg["reset_success_streak"]:
        sg["active"] = False
    else:
        sg["active"] = (fails >= scfg["fail_threshold"]
                        or any(c >= scfg["churn_threshold"] for c in churn.values()))
    st["struggle"] = sg


def main(evt, cfg):
    session = evt.get("session_id") or "unknown"
    agent = "main"
    prompt = str(evt.get("prompt") or "")[:PROMPT_CAP]
    cwd = evt.get("cwd") or os.getcwd()
    project = paths.gitroot(cwd)
    now = int(time.time())

    def bump(s):
        s["turn"] += 1
        _refresh_struggle(s, cfg)
    st = state.update(session, agent, bump)
    turn = st["turn"]

    conn = db.connect()
    db.ensure_schema(conn)
    _journal_event(conn, cfg, session, project, turn, "user_prompt", prompt)

    parts = []
    p0 = _p0_sentence(prompt) if prompt else None
    if p0:
        d = drafting.draft_remember(p0)
        nm = _near_match(conn, project, d["body"])
        _enqueue(conn, session, agent, "P0", "remember_request",
                 {"text": p0, "project": project}, d["cmd"], nm)
        parts.append(render.render_capability())
    elif CORRECTION_RE.search(prompt or ""):
        corr = prompt.strip()[:300]
        pri = "P1" if _repeat_correction(conn, project, corr, now) else "P2"
        _journal_event(conn, cfg, session, project, turn, "correction", corr)
        d = drafting.draft_remember(corr)
        _enqueue(conn, session, agent, pri, "correction",
                 {"text": corr, "project": project}, d["cmd"])

    inject, remind, hits, q = [], [], [], None
    if len(prompt) >= 8:  # latency guard
        q = querybuild.fts_query(prompt)
        if q:
            hits = db.search(conn, q, project, k=12)
            if hits:
                taus = scoring.struggle_adjusted(cfg, st)
                ctx = scoring.Ctx(session=session, agent=agent, cwd=cwd,
                                  project=project, command_adjacent=False)
                inject, remind = scoring.gate(hits, st, ctx, cfg, conn, taus, now)
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

    SUMMARY.update({"session": session, "agent": agent, "hits": len(hits),
                    "inject": len(inject), "remind": len(remind), "deny": 0})
    if not parts:
        return None
    text = render.clip("\n\n".join(parts), cfg["budgets"]["output_hard_cap"])
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
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
