"""FTS5 query builders (spec 7.5 + AM-3). Every token is double-quoted so
hostile input can never produce an FTS-invalid MATCH string."""
import re

STOP = {
    # common English
    "the", "and", "for", "are", "but", "not", "you", "your", "was", "were",
    "been", "being", "have", "has", "had", "this", "that", "these", "those",
    "with", "from", "into", "will", "would", "can", "could", "should", "may",
    "might", "must", "when", "where", "what", "which", "who", "how", "why",
    "all", "any", "some", "each", "then", "than", "them", "they", "its",
    "just", "also", "here", "there", "only", "very", "does", "doing", "done",
    "don", "doesn", "didn", "isn", "won", "let", "lets", "yes", "okay",
    # request / shell filler
    "please", "run", "running", "use", "using", "used", "make", "making",
    "made", "file", "files", "folder", "directory", "need", "needs", "want",
    "wants", "like", "get", "gets", "got", "set", "new", "now", "try",
    "help", "show", "tell", "give", "see", "look", "check", "work", "works",
    "working", "command", "sure", "thing", "things", "way",
}

_FENCE = re.compile(r"```.*?```", re.S)
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_\-\.]{2,}")
_TRAIL_DIGITS = re.compile(r"\d+$")


def fts_query(text, cap=12):
    if not text or not isinstance(text, str):
        return None
    text = _FENCE.sub(" ", text[:65536])
    toks = [t.lower().strip(".-") for t in _TOKEN.findall(text)]
    toks = [t for t in toks if len(t) >= 3]
    toks = [t for t in dict.fromkeys(toks) if t not in STOP][:cap]
    if not toks:
        return None
    out = []
    for t in toks:
        if t not in out:
            out.append(t)
        # AM-3: identifier digit-stripping ("uuid4" -> also "uuid")
        if len(t) >= 4 and _TRAIL_DIGITS.search(t):
            stripped = _TRAIL_DIGITS.sub("", t).strip(".-")
            if stripped and stripped not in STOP and stripped not in out:
                out.append(stripped)
    return " OR ".join(f'"{t}"' for t in out)


_ERR_LINE = re.compile(r"error|exception|failed|fatal|denied|traceback|not found", re.I)


def error_query(failure_text, cap=10):
    if not failure_text or not isinstance(failure_text, str):
        return None
    all_lines = failure_text[:65536].splitlines()
    lines = [l for l in all_lines if _ERR_LINE.search(l)] or all_lines[-3:]
    t = " ".join(lines)
    t = re.sub(r"/\S+", " ", t)              # paths
    t = re.sub(r"\b[0-9a-f]{6,}\b", " ", t)  # hashes/addresses
    t = re.sub(r"\b\d+\b", " ", t)           # line numbers
    return fts_query(t, cap)
