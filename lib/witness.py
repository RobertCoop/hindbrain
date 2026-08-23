"""Witness tests (spec 5.3 + AM-5). Authority derives from hook-logged evidence;
agent flags can never raise it."""
import hashlib
import json
import re

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
    try:
        rows = conn.execute(
            "SELECT data FROM journal WHERE session_id=? AND event='user_prompt' "
            "ORDER BY ts DESC LIMIT ?", (session_id, n)).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            d = json.loads(r[0])
        except (ValueError, TypeError, IndexError):
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


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


def user_witnessed(body, session_id, conn, cfg, n_prompts=20):
    b = content_words(body)
    if not b:
        return False
    hb = None
    for row in _last_user_prompts(conn, session_id, n_prompts):
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


def confirm_witnessed(mem, session_id, conn, cfg):
    # AM-5: full only when a recent user turn names the memory (id or title words)
    # with a confirmation cue, or restates the body itself (5.3 test).
    body_words = content_words(mem.get("body") or "")
    title_words = content_words(mem.get("title") or "")
    mid = (mem.get("id") or "").lower()
    hashed_body = hashed_title = None
    for row in _last_user_prompts(conn, session_id, 5):
        if isinstance(row.get("hash_words"), list):
            u = set(row["hash_words"])
            if hashed_body is None:
                hashed_body = _hash_set(body_words)
                hashed_title = _hash_set(title_words)
            cue = bool(_hash_set(CUES) & u)
            id_ref = bool(mid) and _hash_word(mid) in u
            title_ref = bool(title_words) and hashed_title <= u
        else:
            text = (row.get("text") or "").lower()
            u = content_words(text)
            cue = bool(CUES & set(_tokens(text)))
            id_ref = bool(mid) and mid in text
            title_ref = bool(title_words) and title_words <= u
        if (id_ref or title_ref) and cue:
            return True
        if _matches(body_words, hashed_body, row):
            return True
    return False
