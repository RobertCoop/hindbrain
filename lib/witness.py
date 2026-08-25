"""Witness tests (spec 5.3 + AM-5). Authority derives from hook-logged evidence;
agent flags can never raise it."""
import hashlib
import json
import re
import time

from lib import querybuild

CUES = {"yes", "confirm", "correct", "trust", "keep"}

_WORD = re.compile(r"[a-z0-9_][a-z0-9_\-\.]{2,}")


def _tokens(text):
    return [t.strip(".-") for t in _WORD.findall((text or "").lower())]


def content_words(text):
    return {t for t in _tokens(text) if len(t) >= 3 and t not in querybuild.STOP}


def _hash_word(w):
    return hashlib.sha256(w.encode("utf-8")).hexdigest()


def _hash_set(words):
    return {_hash_word(w) for w in words}


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _last_user_prompts(conn, session_id, n):
    # over-fetch: synthetic wrapper turns are dropped AFTER the query, and
    # must not crowd genuine turns out of the n-turn witness window
    try:
        rows = conn.execute(
            "SELECT data FROM journal WHERE session_id=? AND event='user_prompt' "
            "ORDER BY ts DESC LIMIT ?", (session_id, n * 4)).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            d = json.loads(r[0])
        except (ValueError, TypeError, IndexError):
            continue
        if isinstance(d, dict) and not d.get("synthetic"):
            out.append(d)
            if len(out) >= n:
                break
    return out


FALLBACK_WINDOW_S = 900


def recent_project_prompts(conn, project, n=10, window_s=FALLBACK_WINDOW_S):
    # cross-session fallback for when the CLI's resolved session id has no
    # journaled user turns (resume/fork/env drift): same project, last 15 min.
    # The witness content tests still gate — this only widens WHICH of the
    # user's own recent turns count as evidence.
    if not project:
        return []
    try:
        cutoff = int(time.time()) - window_s
        rows = conn.execute(
            "SELECT data FROM journal WHERE event='user_prompt' AND ts>=? "
            "ORDER BY ts DESC LIMIT 50", (cutoff,)).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            d = json.loads(r[0])
        except (ValueError, TypeError, IndexError):
            continue
        if (isinstance(d, dict) and d.get("project") == project
                and not d.get("synthetic")):
            out.append(d)
            if len(out) >= n:
                break
    return out


def _witness_rows(conn, session_id, n, project):
    rows = _last_user_prompts(conn, session_id, n)
    if rows or not project:
        return rows, False
    return recent_project_prompts(conn, project, n=min(n, 10)), True


def _matches(body_words, hashed_body, row):
    # row data: {"text": raw} or {"hash_words": [sha256 hex, ...]} per redact_journal
    if isinstance(row.get("hash_words"), list):
        u = set(row["hash_words"])
        b = hashed_body
    else:
        u = content_words(row.get("text") or "")
        b = body_words
    if not b or not u:
        return False
    return _jaccard(b, u) >= 0.5 or b <= u


def user_witnessed(body, session_id, conn, cfg, n_prompts=20, project=None):
    b = content_words(body)
    if not b:
        return False
    rows, _ = _witness_rows(conn, session_id, n_prompts, project)
    hb = None
    for row in rows:
        if hb is None and isinstance(row.get("hash_words"), list):
            hb = _hash_set(b)
        if _matches(b, hb, row):
            return True
    return False


def observer_witnessed(candidate_row):
    if not isinstance(candidate_row, dict):
        return False
    if candidate_row.get("signal") not in ("learned_fix", "near_miss"):
        return False
    try:
        payload = json.loads(candidate_row.get("payload") or "{}")
    except (ValueError, TypeError):
        return False
    return isinstance(payload, dict) and payload.get("observer") is True


def confirm_witness_stats(conn, session_id, project=None):
    # diagnostics for mem confirm's refusal message: session turns, plus
    # recent same-project turns (any session) — always computed, so a stale
    # session binding shows up as fallback > session
    session_turns = len(_last_user_prompts(conn, session_id, 5))
    fallback_turns = len(recent_project_prompts(conn, project, n=5)) if project else 0
    return session_turns, fallback_turns


def confirm_witnessed(mem, session_id, conn, cfg, project=None):
    # AM-5: full only when a recent user turn names the memory (id or title
    # words) with a confirmation cue, or restates the body itself (5.3 test).
    # Asymmetric fallback: when the session-scoped turns produce NO MATCH (not
    # merely zero rows — a stale session binding yields wrong-but-nonempty
    # rows), recent same-project turns are also scanned. The content
    # requirement makes an accidental cross-session pass essentially
    # impossible, and the evidence is still the user's own hook-journaled
    # words. user_witnessed keeps its stricter zero-rows-only fallback.
    if _confirm_scan(mem, _last_user_prompts(conn, session_id, 5)):
        return True
    if project:
        return _confirm_scan(mem, recent_project_prompts(conn, project, n=5))
    return False


def _confirm_scan(mem, rows):
    body_words = content_words(mem.get("body") or "")
    title_words = content_words(mem.get("title") or "")
    mid = (mem.get("id") or "").lower()
    hashed_body = hashed_title = None
    for row in rows:
        if isinstance(row.get("hash_words"), list):
            u = set(row["hash_words"])
            if hashed_body is None:
                hashed_body = _hash_set(body_words)
                hashed_title = _hash_set(title_words)
            cue = bool(_hash_set(CUES) & u)
            id_ref = bool(mid) and _hash_word(mid) in u
            title_ref = (bool(title_words)
                         and len(hashed_title & u) / len(hashed_title) >= 0.6)
        else:
            text = (row.get("text") or "").lower()
            u = content_words(text)
            cue = bool(CUES & set(_tokens(text)))
            id_ref = bool(mid) and mid in text
            # >=60% of title content-words: all-subset is unreachable in
            # natural speech for prose titles
            title_ref = (bool(title_words)
                         and len(title_words & u) / len(title_words) >= 0.6)
        if (id_ref or title_ref) and cue:
            return True
        if _matches(body_words, hashed_body, row):
            return True
    return False
